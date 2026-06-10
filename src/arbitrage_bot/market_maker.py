from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .market_making import MarketMakerPlan, build_symmetric_market_maker_plan
from .risk import RiskOrder, evaluate_order_batch
from .trade_log import write_trade_event


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"market maker exchange is not configured: {key}")


async def build_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
) -> MarketMakerPlan:
    maker_cfg = cfg.market_maker
    if not maker_cfg.enabled:
        raise ValueError("market_maker.enabled is false")
    if not maker_cfg.exchange:
        raise ValueError("market_maker.exchange is required")
    if not maker_cfg.symbol:
        raise ValueError("market_maker.symbol is required")

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
    return build_symmetric_market_maker_plan(book, maker_cfg)


async def place_plan(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: MarketMakerPlan,
    *,
    replace_existing: bool,
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    exchange_cfg = _find_exchange(cfg, maker_cfg.exchange)
    canceled: list[dict[str, Any]] = []
    if replace_existing or maker_cfg.cancel_existing_orders:
        canceled = await manager.cancel_open_orders(exchange_cfg, symbol=maker_cfg.symbol)

    placed = []
    timestamp_ms = int(time.time() * 1000)
    for index, order in enumerate(plan.orders, start=1):
        client_order_id = (
            f"{maker_cfg.client_order_prefix}-{timestamp_ms}-{index}"
            if maker_cfg.client_order_prefix
            else None
        )
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
        "placed_count": len(placed),
        "placed_order_ids": [item.get("id") for item in placed if isinstance(item, dict)],
    }


async def run_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    live: bool,
    replace_existing: bool,
) -> dict[str, Any]:
    plan = await build_plan(cfg, manager)
    payload: dict[str, Any] = {
        "type": "market_maker",
        "mode": "live" if live else "dry_run",
        "status": "planned",
        "plan": plan.to_dict(),
    }
    risk_orders = [
        RiskOrder(
            strategy="market_maker",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price,
            quote_notional=order.quote_notional,
            distance_bps=order.distance_bps,
        )
        for order in plan.orders
    ]
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="market_maker",
        live=live,
        existing_spread_bps=plan.existing_spread_bps,
        plan_observed_at=plan.observed_at,
        post_only=cfg.market_maker.post_only,
    )
    payload["risk"] = risk.to_dict()

    if live:
        if not risk.approved:
            payload["status"] = "blocked_by_risk"
            return payload
        payload["execution"] = await place_plan(
            cfg,
            manager,
            plan,
            replace_existing=replace_existing,
        )
        payload["status"] = "placed"

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
    try:
        while True:
            started = time.monotonic()
            payload = await run_cycle(
                cfg,
                manager,
                live=live,
                replace_existing=replace_existing,
            )
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            write_trade_event(cfg.trade_log, payload)
            sys.stdout.flush()

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
