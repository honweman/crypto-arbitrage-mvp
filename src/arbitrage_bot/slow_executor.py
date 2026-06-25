from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .order_validation import summarize_order_validations
from .risk import (
    RiskMarketContext,
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)
from .slow_execution import SlowExecutionPlan, build_slow_execution_plan
from .strategy_timeline import write_strategy_timeline_from_payload
from .trade_log import write_trade_event


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"Auto Buy/Sell exchange is not configured: {key}")


def _quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0].upper()


def _quote_to_common_rate(cfg: BotConfig, symbol: str) -> float | None:
    quote = _quote_currency(symbol)
    if not quote:
        return None
    if quote == cfg.common_quote_currency.upper():
        return 1.0
    if quote in cfg.quote_rates:
        return float(cfg.quote_rates[quote])
    return None


def _quote_conversion(cfg: BotConfig, symbol: str) -> dict[str, Any]:
    quote = _quote_currency(symbol)
    rate = _quote_to_common_rate(cfg, symbol)
    return {
        "quote_currency": quote,
        "common_quote_currency": cfg.common_quote_currency,
        "quote_to_common_rate": rate,
        "available": rate is not None,
    }


def _raw_client_order_id(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    for value in (
        raw.get("clientOrderId"),
        raw.get("clientOrderID"),
        raw.get("client_order_id"),
        raw.get("clientOid"),
        raw.get("client_oid"),
        info.get("clientOrderId"),
        info.get("clientOrderID"),
        info.get("client_order_id"),
        info.get("clientOid"),
        info.get("client_oid"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _raw_order_id(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("id") or raw.get("order") or "").strip()


def _raw_order_side(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    return str(raw.get("side") or info.get("side") or "").strip().lower()


def _symbol_matches(left: str, right: str) -> bool:
    return str(left or "").upper() == str(right or "").upper()


def _opposite_side(left: str, right: str) -> bool:
    return {str(left).lower(), str(right).lower()} == {"buy", "sell"}


def _market_maker_self_trade_guard(
    cfg: BotConfig,
    plan: SlowExecutionPlan,
    *,
    live: bool,
    open_orders: list[Any] | None,
    open_order_error: str | None,
    market_maker_paused: bool,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution
    maker_cfg = cfg.market_maker
    if not live or not exec_cfg.block_conflicting_market_maker or plan.order is None:
        return {
            "enabled": bool(exec_cfg.block_conflicting_market_maker),
            "blocked": False,
            "reasons": [],
            "conflicting_open_orders": [],
        }

    same_config_target = (
        maker_cfg.exchange == exec_cfg.exchange
        and _symbol_matches(maker_cfg.symbol, exec_cfg.symbol)
    )
    reasons: list[str] = []
    if (
        same_config_target
        and maker_cfg.enabled
        and maker_cfg.live_enabled
        and not market_maker_paused
    ):
        reasons.append(
            "self-trade guard: market maker is live on "
            f"{exec_cfg.exchange} {exec_cfg.symbol}; pause or disable MM before "
            "starting Auto Buy/Sell"
        )

    prefix = str(maker_cfg.client_order_prefix or "").strip()
    conflicting_open_orders: list[dict[str, Any]] = []
    if prefix:
        if open_order_error:
            reasons.append(
                "self-trade guard: could not verify existing market maker open "
                f"orders for {exec_cfg.exchange} {exec_cfg.symbol}: {open_order_error}"
            )
        for raw in open_orders or []:
            client_order_id = _raw_client_order_id(raw)
            if not client_order_id.startswith(prefix):
                continue
            side = _raw_order_side(raw)
            if side and not _opposite_side(plan.order.side, side):
                continue
            conflicting_open_orders.append(
                {
                    "id": _raw_order_id(raw),
                    "client_order_id": client_order_id,
                    "side": side or "unknown",
                }
            )
        if conflicting_open_orders:
            reasons.append(
                "self-trade guard: found "
                f"{len(conflicting_open_orders)} open market maker order(s) "
                f"with prefix {prefix} on {exec_cfg.exchange} {exec_cfg.symbol}"
            )

    return {
        "enabled": True,
        "blocked": bool(reasons),
        "reasons": reasons,
        "conflicting_open_orders": conflicting_open_orders,
        "market_maker_paused": market_maker_paused,
        "market_maker_exchange": maker_cfg.exchange,
        "market_maker_symbol": maker_cfg.symbol,
        "market_maker_prefix": prefix,
    }


def _apply_self_trade_guard(
    risk: dict[str, Any],
    guard: dict[str, Any],
) -> dict[str, Any]:
    if not guard.get("blocked"):
        risk["self_trade_guard"] = guard
        return risk
    reasons = [str(reason) for reason in guard.get("reasons", []) if reason]
    risk["approved"] = False
    risk["level"] = "blocked"
    risk["reasons"] = [
        *list(risk.get("reasons", [])),
        *[reason for reason in reasons if reason not in risk.get("reasons", [])],
    ]
    risk["self_trade_guard"] = guard
    return risk


async def build_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    submitted_base: float = 0.0,
    submitted_quote: float = 0.0,
    start_price_triggered: bool = False,
) -> SlowExecutionPlan:
    exec_cfg = cfg.slow_execution
    if not exec_cfg.enabled:
        raise ValueError("slow_execution.enabled is false")
    if not exec_cfg.exchange:
        raise ValueError("slow_execution.exchange is required")
    if not exec_cfg.symbol:
        raise ValueError("slow_execution.symbol is required")

    exchange_cfg = _find_exchange(cfg, exec_cfg.exchange)
    book = await manager.fetch_order_book(
        exchange_cfg,
        exec_cfg.symbol,
        max(cfg.order_book_depth, 2),
    )
    if book is None:
        raise ValueError(f"no order book for {exec_cfg.exchange} {exec_cfg.symbol}")
    return build_slow_execution_plan(
        book,
        exec_cfg,
        submitted_base=submitted_base,
        submitted_quote=submitted_quote,
        start_price_triggered=start_price_triggered,
    )


async def place_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: SlowExecutionPlan,
    *,
    replace_existing: bool,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution
    exchange_cfg = _find_exchange(cfg, exec_cfg.exchange)
    canceled: list[dict[str, Any]] = []
    if replace_existing or exec_cfg.cancel_existing_orders:
        canceled = await manager.cancel_open_orders(exchange_cfg, symbol=exec_cfg.symbol)

    if plan.order is None:
        return {
            "canceled_count": len(canceled),
            "placed_count": 0,
            "placed_order_ids": [],
        }

    client_order_id = (
        f"{exec_cfg.client_order_prefix}-{int(time.time() * 1000)}"
        if exec_cfg.client_order_prefix
        else None
    )
    raw = await manager.create_limit_order(
        exchange_cfg,
        symbol=exec_cfg.symbol,
        side=plan.order.side,
        amount=plan.order.amount,
        price=plan.order.price,
        post_only=exec_cfg.post_only,
        client_order_id=client_order_id,
    )
    return {
        "canceled_count": len(canceled),
        "placed_count": 1,
        "placed_order_ids": [raw.get("id")] if isinstance(raw, dict) else [],
    }


async def validate_plan_order(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: SlowExecutionPlan,
) -> dict[str, Any]:
    if plan.order is None:
        return summarize_order_validations([])

    exec_cfg = cfg.slow_execution
    exchange_cfg = _find_exchange(cfg, exec_cfg.exchange)
    try:
        row = await manager.prepare_limit_order(
            exchange_cfg,
            symbol=exec_cfg.symbol,
            side=plan.order.side,
            amount=plan.order.amount,
            price=plan.order.price,
        )
    except Exception as exc:  # noqa: BLE001
        row = {
            "exchange": exchange_cfg.key,
            "symbol": exec_cfg.symbol,
            "side": plan.order.side,
            "status": "error",
            "requested_amount": plan.order.amount,
            "requested_price": plan.order.price,
            "amount": None,
            "price": None,
            "cost": plan.order.quote_notional,
            "limits": {},
            "precision": {},
            "errors": [f"{exc.__class__.__name__}: {exc}"],
            "warnings": [],
        }
    return summarize_order_validations([row])


def _block_for_validation(
    payload: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    risk["approved"] = False
    risk["level"] = "blocked"
    risk["reasons"] = [
        *list(risk.get("reasons", [])),
        *[f"order validation: {error}" for error in validation.get("errors", [])],
    ]
    risk["warnings"] = [
        *list(risk.get("warnings", [])),
        *validation.get("warnings", []),
    ]
    payload["risk"] = risk
    payload["status"] = "blocked_by_risk"
    return payload


async def cancel_order_ids(
    cfg: BotConfig,
    manager: ExchangeManager,
    order_ids: list[str],
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution
    exchange_cfg = _find_exchange(cfg, exec_cfg.exchange)
    canceled = []
    errors = []
    for order_id in order_ids:
        try:
            canceled.append(
                await manager.cancel_order(
                    exchange_cfg,
                    symbol=exec_cfg.symbol,
                    order_id=order_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"order_id": order_id, "error": str(exc)})
    return {
        "type": "slow_execution_cancel",
        "order_ids": order_ids,
        "canceled_count": len(canceled),
        "errors": errors,
    }


async def run_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    submitted_base: float,
    submitted_quote: float = 0.0,
    live: bool,
    replace_existing: bool,
    previous_mid_price: float | None = None,
    last_cancel_at: float | None = None,
    start_price_triggered: bool = False,
    market_maker_paused: bool = False,
) -> tuple[dict[str, Any], float]:
    plan = await build_plan(
        cfg,
        manager,
        submitted_base=submitted_base,
        submitted_quote=submitted_quote,
        start_price_triggered=start_price_triggered,
    )
    next_submitted_base = submitted_base
    next_submitted_quote = submitted_quote
    payload: dict[str, Any] = {
        "type": "slow_execution",
        "mode": "live" if live else "dry_run",
        "status": plan.status,
        "plan": plan.to_dict(),
        "tracks_submitted_base": True,
        "tracks_submitted_quote": True,
        "start_price_triggered": start_price_triggered
        or plan.status != "waiting_for_start_price",
        "next_submitted_quote": next_submitted_quote,
    }
    conversion = _quote_conversion(cfg, plan.symbol)
    payload["quote_conversion"] = conversion
    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    risk_orders = []
    if plan.order is not None:
        risk_orders.append(
            RiskOrder(
                strategy="slow_execution",
                exchange=plan.exchange,
                symbol=plan.symbol,
                side=plan.order.side,
                amount=plan.order.amount,
                price=plan.order.price * quote_rate_for_risk,
                quote_notional=plan.order.quote_notional * quote_rate_for_risk,
                distance_bps=0.0,
            )
        )
    exchange_cfg = _find_exchange(cfg, cfg.slow_execution.exchange)
    existing_open_orders: list[Any] | None = None
    existing_open_order_count: int | None = None
    open_order_error: str | None = None
    should_cancel_existing = replace_existing or cfg.slow_execution.cancel_existing_orders
    needs_open_orders = live and (
        cfg.slow_execution.block_conflicting_market_maker
        or cfg.risk.max_open_orders > 0
        or cfg.risk.max_cancels_per_cycle > 0
        or cfg.risk.min_seconds_between_cancels > 0
    )
    if needs_open_orders:
        try:
            existing_open_orders = await manager.fetch_open_orders(
                exchange_cfg,
                symbol=cfg.slow_execution.symbol,
            )
            existing_open_order_count = len(existing_open_orders)
        except Exception as exc:  # noqa: BLE001
            open_order_error = str(exc)
    expected_cancel_count = (
        existing_open_order_count
        if should_cancel_existing and existing_open_order_count is not None
        else 0
    )
    if live and plan.order is not None and cfg.slow_execution.order_ttl_seconds > 0:
        expected_cancel_count += 1
    market = RiskMarketContext(
        exchange=plan.exchange,
        symbol=plan.symbol,
        best_bid=plan.best_bid * quote_rate_for_risk,
        best_ask=plan.best_ask * quote_rate_for_risk,
        mid_price=plan.mid_price * quote_rate_for_risk,
        bid_depth_quote=plan.bid_depth_quote * quote_rate_for_risk,
        ask_depth_quote=plan.ask_depth_quote * quote_rate_for_risk,
        max_level_gap_bps=plan.max_level_gap_bps,
        order_book_timestamp_ms=plan.order_book_timestamp_ms,
        order_book_received_at=plan.order_book_received_at,
    )
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="slow_execution",
        live=live,
        existing_spread_bps=plan.existing_spread_bps,
        plan_observed_at=plan.observed_at,
        market=market,
        previous_mid_price=previous_mid_price,
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=existing_open_order_count,
        expected_cancel_count=expected_cancel_count,
        last_cancel_at=last_cancel_at,
        open_order_error=open_order_error,
        post_only=cfg.slow_execution.post_only,
    )
    guard = _market_maker_self_trade_guard(
        cfg,
        plan,
        live=live,
        open_orders=existing_open_orders,
        open_order_error=open_order_error,
        market_maker_paused=market_maker_paused,
    )
    payload["risk"] = _apply_self_trade_guard(risk.to_dict(), guard)
    payload["risk"]["currency"] = cfg.common_quote_currency
    payload["risk"]["quote_conversion"] = conversion
    if quote_rate is None:
        payload["risk"]["approved"] = False
        payload["risk"]["level"] = "blocked"
        payload["risk"]["reasons"] = [
            *list(payload["risk"].get("reasons", [])),
            (
                f"missing quote rate for {conversion['quote_currency']} -> "
                f"{cfg.common_quote_currency}"
            ),
        ]

    if live and plan.order is not None:
        if not payload["risk"]["approved"]:
            payload["status"] = "blocked_by_risk"
            return payload, next_submitted_base
        validation = await validate_plan_order(cfg, manager, plan)
        payload["order_validation"] = validation
        if validation["status"] != "ok":
            return _block_for_validation(payload, validation), next_submitted_base
        payload["execution"] = await place_plan(
            cfg,
            manager,
            plan,
            replace_existing=replace_existing,
        )
        payload["status"] = "placed"
        next_submitted_base = plan.order.submitted_base_after
        next_submitted_quote = plan.order.submitted_quote_after
    elif plan.order is not None:
        next_submitted_base = plan.order.submitted_base_after
        next_submitted_quote = plan.order.submitted_quote_after

    payload["next_submitted_quote"] = next_submitted_quote
    return payload, next_submitted_base


async def run_loop(
    cfg: BotConfig,
    *,
    live: bool,
    loop: bool,
    interval_seconds: float | None,
    replace_existing: bool,
) -> None:
    interval = (
        cfg.slow_execution.interval_seconds
        if interval_seconds is None
        else interval_seconds
    )
    interval = max(1.0, interval)
    manager = ExchangeManager()
    submitted_base = 0.0
    submitted_quote = 0.0
    start_price_triggered = cfg.slow_execution.start_price <= 0
    previous_mid_price: float | None = None
    last_cancel_at: float | None = None
    try:
        while True:
            started = time.monotonic()
            payload, submitted_base = await run_cycle(
                cfg,
                manager,
                submitted_base=submitted_base,
                submitted_quote=submitted_quote,
                live=live,
                replace_existing=replace_existing,
                previous_mid_price=previous_mid_price,
                last_cancel_at=last_cancel_at,
                start_price_triggered=start_price_triggered,
            )
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            write_trade_event(cfg.trade_log, payload)
            write_strategy_timeline_from_payload(
                cfg.strategy_timeline,
                payload,
                source="auto_buy_sell_loop",
            )
            sys.stdout.flush()
            plan_payload = payload.get("plan", {})
            if cfg.slow_execution.start_price > 0 and payload.get("status") != "waiting_for_start_price":
                start_price_triggered = True
            if isinstance(plan_payload, dict):
                mid_price = plan_payload.get("mid_price")
                if isinstance(mid_price, (int, float)):
                    previous_mid_price = float(mid_price)
                order_payload = plan_payload.get("order")
                if isinstance(order_payload, dict):
                    submitted_quote_after = order_payload.get("submitted_quote_after")
                    if isinstance(submitted_quote_after, (int, float)):
                        submitted_quote = float(submitted_quote_after)
            execution = payload.get("execution", {})
            if (
                isinstance(execution, dict)
                and int(execution.get("canceled_count", 0) or 0) > 0
            ):
                last_cancel_at = time.time()

            ttl = cfg.slow_execution.order_ttl_seconds
            execution = payload.get("execution", {})
            placed_order_ids = [
                order_id
                for order_id in execution.get("placed_order_ids", [])
                if isinstance(order_id, str)
            ]
            if live and ttl > 0 and placed_order_ids:
                await asyncio.sleep(ttl)
                cancel_payload = await cancel_order_ids(
                    cfg,
                    manager,
                    placed_order_ids,
                )
                print(json.dumps(cancel_payload, ensure_ascii=True, sort_keys=True))
                write_trade_event(cfg.trade_log, cancel_payload)
                write_strategy_timeline_from_payload(
                    cfg.strategy_timeline,
                    cancel_payload,
                    source="auto_buy_sell_loop",
                )
                sys.stdout.flush()
                if cancel_payload["canceled_count"] > 0:
                    last_cancel_at = time.time()

            if not loop or payload["status"] in {
                "complete",
                "below_min_order_quote",
                "stopped_by_price",
            }:
                return

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto Buy/Sell top-of-book executor with guarded live order placement"
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Keep submitting configured Auto Buy/Sell slices until total_base "
            "or total_quote is submitted."
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help=(
            "Override Auto Buy/Sell interval_seconds "
            "(config key slow_execution.interval_seconds). "
            "Minimum effective interval is 1 second."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Place real exchange orders. Default is dry-run plan output only.",
    )
    parser.add_argument(
        "--confirm-live-orders",
        action="store_true",
        help="Required together with --live to acknowledge real order placement.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Cancel open orders on the configured symbol before placing each slice.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)

    if args.live and not args.confirm_live_orders:
        raise SystemExit("--live requires --confirm-live-orders")

    try:
        asyncio.run(
            run_loop(
                cfg,
                live=args.live,
                loop=args.loop,
                interval_seconds=args.interval_seconds,
                replace_existing=args.replace_existing,
            )
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
