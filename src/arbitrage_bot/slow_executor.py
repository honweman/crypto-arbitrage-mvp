from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .risk import (
    RiskMarketContext,
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)
from .slow_execution import SlowExecutionPlan, build_slow_execution_plan
from .trade_log import write_trade_event


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"Auto Buy/Sell exchange is not configured: {key}")


async def build_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    submitted_base: float = 0.0,
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
    live: bool,
    replace_existing: bool,
    previous_mid_price: float | None = None,
    last_cancel_at: float | None = None,
) -> tuple[dict[str, Any], float]:
    plan = await build_plan(cfg, manager, submitted_base=submitted_base)
    next_submitted_base = submitted_base
    payload: dict[str, Any] = {
        "type": "slow_execution",
        "mode": "live" if live else "dry_run",
        "status": plan.status,
        "plan": plan.to_dict(),
        "tracks_submitted_base": True,
    }
    risk_orders = []
    if plan.order is not None:
        risk_orders.append(
            RiskOrder(
                strategy="slow_execution",
                exchange=plan.exchange,
                symbol=plan.symbol,
                side=plan.order.side,
                amount=plan.order.amount,
                price=plan.order.price,
                quote_notional=plan.order.quote_notional,
                distance_bps=0.0,
            )
        )
    exchange_cfg = _find_exchange(cfg, cfg.slow_execution.exchange)
    existing_open_order_count: int | None = None
    open_order_error: str | None = None
    should_cancel_existing = replace_existing or cfg.slow_execution.cancel_existing_orders
    if live and (
        cfg.risk.max_open_orders > 0
        or cfg.risk.max_cancels_per_cycle > 0
        or cfg.risk.min_seconds_between_cancels > 0
    ):
        try:
            existing_open_order_count = len(
                await manager.fetch_open_orders(
                    exchange_cfg,
                    symbol=cfg.slow_execution.symbol,
                )
            )
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
        best_bid=plan.best_bid,
        best_ask=plan.best_ask,
        mid_price=plan.mid_price,
        bid_depth_quote=plan.bid_depth_quote,
        ask_depth_quote=plan.ask_depth_quote,
        max_level_gap_bps=plan.max_level_gap_bps,
        order_book_timestamp_ms=plan.order_book_timestamp_ms,
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
    payload["risk"] = risk.to_dict()

    if live and plan.order is not None:
        if not risk.approved:
            payload["status"] = "blocked_by_risk"
            return payload, next_submitted_base
        payload["execution"] = await place_plan(
            cfg,
            manager,
            plan,
            replace_existing=replace_existing,
        )
        payload["status"] = "placed"
        next_submitted_base = plan.order.submitted_base_after
    elif plan.order is not None:
        next_submitted_base = plan.order.submitted_base_after

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
    previous_mid_price: float | None = None
    last_cancel_at: float | None = None
    try:
        while True:
            started = time.monotonic()
            payload, submitted_base = await run_cycle(
                cfg,
                manager,
                submitted_base=submitted_base,
                live=live,
                replace_existing=replace_existing,
                previous_mid_price=previous_mid_price,
                last_cancel_at=last_cancel_at,
            )
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            write_trade_event(cfg.trade_log, payload)
            sys.stdout.flush()
            plan_payload = payload.get("plan", {})
            if isinstance(plan_payload, dict):
                mid_price = plan_payload.get("mid_price")
                if isinstance(mid_price, (int, float)):
                    previous_mid_price = float(mid_price)
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
        description="Auto Buy/Sell midpoint executor with guarded live order placement"
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep submitting configured Auto Buy/Sell slices until total_base is submitted.",
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
