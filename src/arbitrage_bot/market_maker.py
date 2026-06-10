from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import (
    ExchangeManager,
    limit_order_capability_errors,
    limit_order_features,
)
from .market_making import MarketMakerPlan, build_symmetric_market_maker_plan
from .order_validation import summarize_order_validations
from .risk import (
    RiskMarketContext,
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)
from .trade_log import write_trade_event


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"market maker exchange is not configured: {key}")


def quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0].upper()


def quote_to_common_rate(cfg: BotConfig, symbol: str) -> float | None:
    quote = quote_currency(symbol)
    if not quote:
        return None
    if quote == cfg.common_quote_currency.upper():
        return 1.0
    if quote in cfg.quote_rates:
        return float(cfg.quote_rates[quote])
    return None


def market_maker_quote_conversion(cfg: BotConfig, symbol: str) -> dict[str, Any]:
    quote = quote_currency(symbol)
    rate = quote_to_common_rate(cfg, symbol)
    return {
        "quote_currency": quote,
        "common_quote_currency": cfg.common_quote_currency,
        "quote_to_common_rate": rate,
        "available": rate is not None,
    }


def _scaled_market_context(
    plan: MarketMakerPlan,
    *,
    quote_rate: float,
) -> RiskMarketContext:
    return RiskMarketContext(
        exchange=plan.exchange,
        symbol=plan.symbol,
        best_bid=plan.best_bid * quote_rate,
        best_ask=plan.best_ask * quote_rate,
        mid_price=plan.mid_price * quote_rate,
        bid_depth_quote=plan.bid_depth_quote * quote_rate,
        ask_depth_quote=plan.ask_depth_quote * quote_rate,
        max_level_gap_bps=plan.max_level_gap_bps,
        order_book_timestamp_ms=plan.order_book_timestamp_ms,
    )


def _plan_reprice_bps(
    previous_plan: dict[str, Any] | None,
    current_plan: MarketMakerPlan,
) -> float | None:
    if not previous_plan:
        return None
    if previous_plan.get("exchange") != current_plan.exchange:
        return None
    if previous_plan.get("symbol") != current_plan.symbol:
        return None
    previous_orders = previous_plan.get("orders")
    if not isinstance(previous_orders, list):
        return None
    if len(previous_orders) != len(current_plan.orders):
        return None

    previous_by_key = {
        (item.get("side"), item.get("level")): item
        for item in previous_orders
        if isinstance(item, dict)
    }
    max_change_bps = 0.0
    for order in current_plan.orders:
        previous_order = previous_by_key.get((order.side, order.level))
        if not isinstance(previous_order, dict):
            return None
        previous_price = previous_order.get("price")
        previous_quote = previous_order.get("quote_notional")
        if not isinstance(previous_price, (int, float)):
            return None
        if not isinstance(previous_quote, (int, float)):
            return None
        if abs(float(previous_quote) - order.quote_notional) > 1e-12:
            return None
        price_change_bps = (
            abs(order.price - float(previous_price))
            / current_plan.mid_price
            * 10_000
        )
        max_change_bps = max(max_change_bps, price_change_bps)
    return max_change_bps


async def build_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    order_book: OrderBookSnapshot | None = None,
) -> MarketMakerPlan:
    book = await load_plan_order_book(cfg, manager, order_book=order_book)
    return build_symmetric_market_maker_plan(book, cfg.market_maker)


async def load_plan_order_book(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    order_book: OrderBookSnapshot | None = None,
) -> OrderBookSnapshot:
    maker_cfg = cfg.market_maker
    if not maker_cfg.enabled:
        raise ValueError("market_maker.enabled is false")
    if not maker_cfg.exchange:
        raise ValueError("market_maker.exchange is required")
    if not maker_cfg.symbol:
        raise ValueError("market_maker.symbol is required")

    if order_book is not None:
        if (
            order_book.exchange != maker_cfg.exchange
            or order_book.symbol != maker_cfg.symbol
        ):
            raise ValueError(
                "cached order book does not match market maker exchange/symbol"
            )
        return order_book

    exchange_cfg = _find_exchange(cfg, maker_cfg.exchange)
    book = await manager.fetch_order_book(
        exchange_cfg,
        maker_cfg.symbol,
        max(cfg.order_book_depth, maker_cfg.levels),
    )
    if book is None:
        raise ValueError(
            f"no order book for {maker_cfg.exchange} {maker_cfg.symbol}"
        )
    return book


def order_book_market_data(book: OrderBookSnapshot) -> dict[str, Any]:
    return {
        "source": book.source,
        "exchange": book.exchange,
        "symbol": book.symbol,
        "timestamp_ms": book.timestamp_ms,
        "received_at": book.received_at,
        "age_seconds": max(0.0, time.time() - book.received_at),
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
    }


async def place_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: MarketMakerPlan,
    *,
    replace_existing: bool,
    replace_order_ids: list[str] | None = None,
    prepared_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    exchange_cfg = _find_exchange(cfg, maker_cfg.exchange)
    canceled: list[dict[str, Any]] = []
    cancel_errors: list[dict[str, str]] = []
    if replace_order_ids:
        cancel_payload = await cancel_order_ids(cfg, manager, replace_order_ids)
        canceled = cancel_payload["canceled"]
        cancel_errors = cancel_payload["errors"]
    elif replace_existing or maker_cfg.cancel_existing_orders:
        canceled = await manager.cancel_open_orders(exchange_cfg, symbol=maker_cfg.symbol)

    placed = []
    timestamp_ms = int(time.time() * 1000)
    prepared_orders = prepared_orders or []
    client_order_ids = [
        (
            f"{maker_cfg.client_order_prefix}-{timestamp_ms}-{index}"
            if maker_cfg.client_order_prefix
            else None
        )
        for index in range(1, len(plan.orders) + 1)
    ]
    batch_creator = getattr(manager, "create_prepared_limit_orders", None)
    if (
        batch_creator is not None
        and len(plan.orders) > 1
        and len(prepared_orders) == len(plan.orders)
    ):
        try:
            placed = await batch_creator(
                exchange_cfg,
                symbol=maker_cfg.symbol,
                sides=[order.side for order in plan.orders],
                prepared_orders=prepared_orders,
                post_only=maker_cfg.post_only,
                client_order_ids=client_order_ids,
            )
            return {
                "canceled_count": len(canceled),
                "cancel_errors": cancel_errors,
                "placed_count": len(placed),
                "placed_order_ids": [
                    item.get("id") for item in placed if isinstance(item, dict)
                ],
                "used_batch_create": True,
            }
        except NotImplementedError:
            pass
        except Exception as exc:  # noqa: BLE001
            return {
                "canceled_count": len(canceled),
                "cancel_errors": cancel_errors,
                "placed_count": 0,
                "placed_order_ids": [],
                "create_errors": [{"scope": "batch", "error": str(exc)}],
                "used_batch_create": True,
            }

    prepared_creator = getattr(manager, "create_prepared_limit_order", None)
    for index, order in enumerate(plan.orders, start=1):
        client_order_id = client_order_ids[index - 1]
        prepared = (
            prepared_orders[index - 1]
            if index <= len(prepared_orders)
            else None
        )
        if prepared is not None and prepared_creator is not None:
            raw = await prepared_creator(
                exchange_cfg,
                symbol=maker_cfg.symbol,
                side=order.side,
                prepared=prepared,
                post_only=maker_cfg.post_only,
                client_order_id=client_order_id,
            )
        else:
            raw = await manager.create_limit_order(
                exchange_cfg,
                symbol=maker_cfg.symbol,
                side=order.side,
                amount=order.amount,
                price=order.price,
                post_only=maker_cfg.post_only,
                client_order_id=client_order_id,
            )
        placed.append(raw)

    return {
        "canceled_count": len(canceled),
        "cancel_errors": cancel_errors,
        "placed_count": len(placed),
        "placed_order_ids": [item.get("id") for item in placed if isinstance(item, dict)],
        "used_batch_create": False,
    }


async def cancel_order_ids(
    cfg: BotConfig,
    manager: ExchangeManager,
    order_ids: list[str],
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    exchange_cfg = _find_exchange(cfg, maker_cfg.exchange)
    canceled = []
    errors = []
    batch_canceler = getattr(manager, "cancel_orders", None)
    if batch_canceler is not None and len(order_ids) > 1:
        try:
            canceled = await batch_canceler(
                exchange_cfg,
                symbol=maker_cfg.symbol,
                order_ids=order_ids,
            )
            return {
                "type": "market_maker_cancel",
                "order_ids": order_ids,
                "canceled": canceled,
                "canceled_count": len(canceled),
                "errors": [],
                "used_batch_cancel": True,
            }
        except Exception as exc:  # noqa: BLE001
            errors.append({"order_id": "batch", "error": str(exc)})

    for order_id in order_ids:
        try:
            canceled.append(
                await manager.cancel_order(
                    exchange_cfg,
                    symbol=maker_cfg.symbol,
                    order_id=order_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"order_id": order_id, "error": str(exc)})
    return {
        "type": "market_maker_cancel",
        "order_ids": order_ids,
        "canceled": canceled,
        "canceled_count": len(canceled),
        "errors": errors,
        "used_batch_cancel": False,
    }


async def validate_plan_orders(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: MarketMakerPlan,
) -> dict[str, Any]:
    exchange_cfg = _find_exchange(cfg, cfg.market_maker.exchange)
    rows = []
    batch_preparer = getattr(manager, "prepare_limit_orders", None)
    if batch_preparer is not None:
        try:
            rows = await batch_preparer(
                exchange_cfg,
                symbol=cfg.market_maker.symbol,
                orders=[order.to_dict() for order in plan.orders],
            )
        except Exception as exc:  # noqa: BLE001
            rows = [
                _validation_error_row(cfg, exchange_cfg, order, exc)
                for order in plan.orders
            ]
    else:
        for order in plan.orders:
            try:
                rows.append(
                    await manager.prepare_limit_order(
                        exchange_cfg,
                        symbol=cfg.market_maker.symbol,
                        side=order.side,
                        amount=order.amount,
                        price=order.price,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                rows.append(_validation_error_row(cfg, exchange_cfg, order, exc))
    summary = summarize_order_validations(rows)
    capability_errors = limit_order_capability_errors(
        exchange_cfg,
        post_only=cfg.market_maker.post_only,
    )
    capability_warnings = []
    features = limit_order_features(exchange_cfg)
    if cfg.market_maker.client_order_prefix and not features.client_order_id:
        capability_warnings.append(
            f"{exchange_cfg.key} does not support client order ids; "
            "MM orders can only be tracked in memory until restart"
        )
    if capability_errors:
        summary["status"] = "error"
        summary["errors"] = [*summary.get("errors", []), *capability_errors]
        summary["error_count"] = len(summary["errors"])
    if capability_warnings:
        summary["warnings"] = [
            *summary.get("warnings", []),
            *capability_warnings,
        ]
        summary["warning_count"] = len(summary["warnings"])
    summary["exchange_features"] = features.to_dict()
    return summary


def _validation_error_row(
    cfg: BotConfig,
    exchange_cfg: ExchangeConfig,
    order: Any,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "exchange": exchange_cfg.key,
        "symbol": cfg.market_maker.symbol,
        "side": order.side,
        "status": "error",
        "requested_amount": order.amount,
        "requested_price": order.price,
        "amount": None,
        "price": None,
        "cost": order.quote_notional,
        "limits": {},
        "precision": {},
        "errors": [f"{exc.__class__.__name__}: {exc}"],
        "warnings": [],
    }


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


async def run_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    live: bool,
    replace_existing: bool,
    replace_order_ids: list[str] | None = None,
    previous_plan: dict[str, Any] | None = None,
    previous_mid_price: float | None = None,
    last_cancel_at: float | None = None,
    order_book: OrderBookSnapshot | None = None,
) -> dict[str, Any]:
    book = await load_plan_order_book(cfg, manager, order_book=order_book)
    plan = build_symmetric_market_maker_plan(book, cfg.market_maker)
    payload: dict[str, Any] = {
        "type": "market_maker",
        "mode": "live" if live else "dry_run",
        "status": "planned",
        "plan": plan.to_dict(),
        "market_data": order_book_market_data(book),
    }
    conversion = market_maker_quote_conversion(cfg, plan.symbol)
    payload["quote_conversion"] = conversion
    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    risk_orders = [
        RiskOrder(
            strategy="market_maker",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price * quote_rate_for_risk,
            quote_notional=order.quote_notional * quote_rate_for_risk,
            distance_bps=order.distance_bps,
        )
        for order in plan.orders
    ]
    exchange_cfg = _find_exchange(cfg, cfg.market_maker.exchange)
    existing_open_order_count: int | None = None
    open_order_error: str | None = None
    replace_order_ids = [order_id for order_id in (replace_order_ids or []) if order_id]
    should_cancel_existing = (
        replace_existing or cfg.market_maker.cancel_existing_orders
    )
    should_cancel_tracked = bool(replace_order_ids)
    if live and (
        cfg.risk.max_open_orders > 0
        or cfg.risk.max_cancels_per_cycle > 0
        or cfg.risk.min_seconds_between_cancels > 0
    ):
        try:
            existing_open_order_count = len(
                await manager.fetch_open_orders(
                    exchange_cfg,
                    symbol=cfg.market_maker.symbol,
                )
            )
        except Exception as exc:  # noqa: BLE001
            open_order_error = str(exc)
    expected_cancel_count = (
        existing_open_order_count
        if should_cancel_existing and existing_open_order_count is not None
        else len(replace_order_ids)
    )
    market = _scaled_market_context(plan, quote_rate=quote_rate_for_risk)
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="market_maker",
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
        post_only=cfg.market_maker.post_only,
    )
    payload["risk"] = risk.to_dict()
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

    if live:
        if not payload["risk"]["approved"]:
            payload["status"] = "blocked_by_risk"
            return payload
        reprice_bps = _plan_reprice_bps(previous_plan, plan)
        payload["reprice_bps"] = reprice_bps
        if (
            cfg.market_maker.reprice_threshold_bps > 0
            and reprice_bps is not None
            and reprice_bps < cfg.market_maker.reprice_threshold_bps
            and replace_order_ids
        ):
            payload["status"] = "unchanged"
            payload["execution"] = {
                "canceled_count": 0,
                "cancel_errors": [],
                "placed_count": 0,
                "placed_order_ids": [],
                "reason": (
                    f"reprice {reprice_bps:.4f} bps is below threshold "
                    f"{cfg.market_maker.reprice_threshold_bps:.4f} bps"
                ),
            }
            return payload
        validation = await validate_plan_orders(cfg, manager, plan)
        payload["order_validation"] = validation
        if validation["status"] != "ok":
            return _block_for_validation(payload, validation)
        execution = await place_plan(
            cfg,
            manager,
            plan,
            replace_existing=replace_existing,
            replace_order_ids=replace_order_ids if should_cancel_tracked else None,
            prepared_orders=validation.get("orders"),
        )
        payload["execution"] = execution
        payload["status"] = "execution_error" if execution.get("create_errors") else "placed"

    return payload


async def run_loop(
    cfg: BotConfig,
    *,
    live: bool,
    loop: bool,
    poll_seconds: float | None,
    replace_existing: bool,
) -> None:
    interval = (
        cfg.market_maker.poll_seconds
        if poll_seconds is None
        else poll_seconds
    )
    interval = max(1.0, interval)
    manager = ExchangeManager()
    previous_mid_price: float | None = None
    previous_plan: dict[str, Any] | None = None
    last_cancel_at: float | None = None
    try:
        while True:
            started = time.monotonic()
            payload = await run_cycle(
                cfg,
                manager,
                live=live,
                replace_existing=replace_existing,
                previous_plan=previous_plan,
                previous_mid_price=previous_mid_price,
                last_cancel_at=last_cancel_at,
            )
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            write_trade_event(cfg.trade_log, payload)
            sys.stdout.flush()
            plan_payload = payload.get("plan", {})
            if isinstance(plan_payload, dict):
                previous_plan = plan_payload
                mid_price = plan_payload.get("mid_price")
                if isinstance(mid_price, (int, float)):
                    previous_mid_price = float(mid_price)
            execution = payload.get("execution", {})
            if (
                isinstance(execution, dict)
                and int(execution.get("canceled_count", 0) or 0) > 0
            ):
                last_cancel_at = time.time()

            if not loop:
                return

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Market maker order-plan generator and guarded live placer"
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously. Default is one dry-run/live cycle.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override market_maker.poll_seconds. Minimum effective interval is 1 second.",
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
        help="Cancel open orders on the configured symbol before placing the new ladder.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)

    if args.live and not args.confirm_live_orders:
        raise SystemExit("--live requires --confirm-live-orders")
    if (
        args.live
        and args.loop
        and not args.replace_existing
        and not cfg.market_maker.cancel_existing_orders
    ):
        raise SystemExit(
            "continuous --live requires --replace-existing or "
            "market_maker.cancel_existing_orders=true"
        )

    try:
        asyncio.run(
            run_loop(
                cfg,
                live=args.live,
                loop=args.loop,
                poll_seconds=args.poll_seconds,
                replace_existing=args.replace_existing,
            )
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
