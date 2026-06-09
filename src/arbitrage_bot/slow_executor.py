from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .slow_execution import SlowExecutionPlan, build_slow_execution_plan


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"slow execution exchange is not configured: {key}")


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


async def run_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    submitted_base: float,
    live: bool,
    replace_existing: bool,
) -> tuple[dict[str, Any], float]:
    plan = await build_plan(cfg, manager, submitted_base=submitted_base)
    next_submitted_base = (
        plan.order.submitted_base_after if plan.order is not None else submitted_base
    )
    payload: dict[str, Any] = {
        "type": "slow_execution",
        "mode": "live" if live else "dry_run",
        "status": plan.status,
        "plan": plan.to_dict(),
        "submitted_based": True,
    }

    if live and plan.order is not None:
        payload["execution"] = await place_plan(
            cfg,
            manager,
            plan,
            replace_existing=replace_existing,
        )
        payload["status"] = "placed"

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
    try:
        while True:
            started = time.monotonic()
            payload, submitted_base = await run_cycle(
                cfg,
                manager,
                submitted_base=submitted_base,
                live=live,
                replace_existing=replace_existing,
            )
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            sys.stdout.flush()

            if not loop or payload["status"] in {"complete", "below_min_order_quote"}:
                return

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Slow midpoint buy/sell executor with guarded live order placement"
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep submitting configured slices until total_base is submitted.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Override slow_execution.interval_seconds. Minimum effective interval is 1 second.",
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
