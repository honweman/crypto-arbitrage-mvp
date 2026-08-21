from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from ..state import MonitorState

from ...config import BotConfig
from ...exchanges import ExchangeManager
from ...spot_grid_executor import (
    TrackedGridOrder,
    cancel_order_ids as cancel_spot_grid_order_ids,
    fills_from_sync,
    load_plan_order_book as load_spot_grid_order_book,
    load_runtime_state as load_spot_grid_runtime_state,
    run_cycle as run_spot_grid_cycle,
    runtime_state_from_tracked,
    save_runtime_state as save_spot_grid_runtime_state,
    sync_tracked_grid_orders,
    tracked_orders_from_state,
    tracked_orders_from_sync,
)
from ...strategy_timeline import (
    write_strategy_timeline_from_payload,
)
from ...trade_log import write_trade_event
from ...web_config import (
    spot_grid_config_to_dict,
)
from ..core import (
    _all_account_exchanges,
    _risk_account_enabled,
    _risk_strategy_enabled,
)


def _stat_int(stats: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(stats.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _stat_float_or_none(stats: dict[str, Any], key: str) -> float | None:
    value = stats.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spot_grid_runtime_path(cfg: BotConfig) -> str:
    return cfg.spot_grid.runtime_path or "data/spot_grid_runtime.json"


def _tracked_spot_grid_order_ids(
    tracked_orders: list[TrackedGridOrder],
) -> list[str]:
    return [order.order_id for order in tracked_orders if order.order_id]


def _tracked_spot_grid_orders_for_ids(
    tracked_orders: list[TrackedGridOrder],
    order_ids: list[str],
) -> list[TrackedGridOrder]:
    order_id_set = {str(order_id) for order_id in order_ids if order_id}
    if not order_id_set:
        return []
    return [order for order in tracked_orders if order.order_id in order_id_set]


def _spot_grid_gate_status(
    cfg: BotConfig,
    *,
    strategy_paused: bool,
    program_running: bool,
) -> tuple[bool, str, str]:
    grid_cfg = cfg.spot_grid
    if not grid_cfg.enabled:
        return False, "disabled", "spot_grid.enabled is false"
    if not grid_cfg.exchange:
        return False, "config_error", "spot_grid.exchange is required"
    if not grid_cfg.symbol:
        return False, "config_error", "spot_grid.symbol is required"
    if not grid_cfg.live_enabled:
        return False, "dry_run", "spot_grid.live_enabled is false"
    if not program_running:
        return False, "program_paused", "program is paused"
    if strategy_paused:
        return False, "paused", "spot_grid strategy is paused"
    if not cfg.risk.enabled or not cfg.risk.trading_enabled:
        return False, "blocked_by_risk", "risk trading is disabled"
    if not cfg.risk.allow_live_trading:
        return False, "blocked_by_risk", "risk.allow_live_trading is false"
    if not _risk_strategy_enabled(cfg, "spot_grid"):
        return False, "blocked_by_risk", "spot_grid strategy is disabled"
    if grid_cfg.exchange and not _risk_account_enabled(cfg, grid_cfg.exchange):
        return False, "blocked_by_risk", f"{grid_cfg.exchange} account is disabled"
    return True, "live", "live"


async def _spot_grid_open_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    current_ids: list[str],
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    fallback_ids = sorted({order_id for order_id in current_ids if order_id})
    if not grid_cfg.exchange or not grid_cfg.symbol:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": None,
        }
    exchange = next(
        (item for item in _all_account_exchanges(cfg) if item.key == grid_cfg.exchange),
        None,
    )
    if exchange is None:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"spot grid exchange is not configured: {grid_cfg.exchange}",
        }
    try:
        open_orders = await manager.fetch_open_orders(exchange, symbol=grid_cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    order_ids: set[str] = set()
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        order_id = _raw_order_id(order)
        if order_id:
            order_ids.add(order_id)
    return {
        "source": "exchange",
        "order_ids": sorted(order_ids),
        "open_orders": [order for order in open_orders if isinstance(order, dict)],
        "open_order_count": len(open_orders),
        "error": None,
    }


async def _spot_grid_closed_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
) -> tuple[list[dict[str, Any]], str | None]:
    grid_cfg = cfg.spot_grid
    if not grid_cfg.exchange or not grid_cfg.symbol:
        return [], None
    exchange = next(
        (item for item in _all_account_exchanges(cfg) if item.key == grid_cfg.exchange),
        None,
    )
    if exchange is None:
        return [], f"spot grid exchange is not configured: {grid_cfg.exchange}"
    try:
        closed_orders = await manager.fetch_closed_orders(
            exchange,
            symbol=grid_cfg.symbol,
            limit=100,
        )
    except Exception as exc:  # noqa: BLE001
        return [], f"{exc.__class__.__name__}: {exc}"
    return [order for order in closed_orders if isinstance(order, dict)], None


async def _cancel_tracked_spot_grid_orders(
    cfg: BotConfig,
    manager: ExchangeManager,
    tracked_orders: list[TrackedGridOrder],
) -> tuple[dict[str, Any] | None, list[TrackedGridOrder]]:
    order_ids = _tracked_spot_grid_order_ids(tracked_orders)
    if not order_ids:
        payload = {
            "type": "spot_grid_cancel",
            "strategy": "spot_grid",
            "mode": "live",
            "status": "cancel_skipped",
            "exchange": cfg.spot_grid.exchange,
            "symbol": cfg.spot_grid.symbol,
            "order_ids": [],
            "canceled_count": 0,
            "errors": [
                {
                    "order_id": "tracked_orders",
                    "error": "tracked grid orders have no exchange order ids",
                }
            ],
            "remaining_open_order_ids": [],
        }
        return payload, tracked_orders

    payload = await cancel_spot_grid_order_ids(cfg, manager, order_ids)
    snapshot = await _spot_grid_open_order_snapshot(cfg, manager, order_ids)
    remaining_ids = [
        order_id
        for order_id in snapshot.get("order_ids", [])
        if order_id in set(order_ids)
    ]
    remaining_orders = (
        tracked_orders
        if snapshot.get("error")
        else _tracked_spot_grid_orders_for_ids(tracked_orders, remaining_ids)
    )
    if snapshot.get("error") or remaining_orders:
        payload["status"] = "cancel_retry"
        payload["remaining_open_order_ids"] = remaining_ids or order_ids
        payload["open_order_sync_error"] = snapshot.get("error")
        payload["reason"] = (
            "tracked grid orders must be fully canceled before rebuilding"
        )
    else:
        payload["remaining_open_order_ids"] = []
    return payload, remaining_orders


def _spot_grid_stats(
    *,
    placed_count: int,
    canceled_count: int,
    cycle_count: int,
    last_cancel_at: float | None,
    previous_mid_price: float | None,
) -> dict[str, Any]:
    return {
        "placed_count": placed_count,
        "canceled_count": canceled_count,
        "cycle_count": cycle_count,
        "last_cancel_at": last_cancel_at,
        "previous_mid_price": previous_mid_price,
    }


def _raw_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("order") or "")


def _raw_client_order_id(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    candidates = (
        raw.get("clientOrderId"),
        raw.get("client_order_id"),
        raw.get("clientOid"),
        info.get("clientOrderId"),
        info.get("client_order_id"),
        info.get("clientOrderID"),
        info.get("clientOid"),
        info.get("client_oid"),
        info.get("orderLinkId"),
        info.get("order_link_id"),
        info.get("externalOid"),
        info.get("identifier"),
    )
    for value in candidates:
        if value:
            return str(value)
    return ""


MARKET_MAKER_FILL_SYNC_SECONDS = 30.0
MARKET_MAKER_FILL_SYNC_LIMIT = 200


async def spot_grid_task_loop(
    cfg: BotConfig,
    state: MonitorState,
) -> None:
    manager = ExchangeManager()
    stored_state = load_spot_grid_runtime_state(_spot_grid_runtime_path(cfg))
    tracked_orders = tracked_orders_from_state(stored_state)
    open_order_exchange = str(stored_state.get("exchange") or "")
    open_order_symbol = str(stored_state.get("symbol") or "")
    stored_stats = (
        stored_state.get("stats") if isinstance(stored_state.get("stats"), dict) else {}
    )
    placed_count = _stat_int(stored_stats, "placed_count")
    canceled_count = _stat_int(stored_stats, "canceled_count")
    cycle_count = _stat_int(stored_stats, "cycle_count")
    last_cancel_at = _stat_float_or_none(stored_stats, "last_cancel_at")
    previous_mid_price = _stat_float_or_none(stored_stats, "previous_mid_price")
    runtime: dict[str, Any] = {
        "status": "starting",
        "mode": "dry_run",
        "open_order_ids": _tracked_spot_grid_order_ids(tracked_orders),
        "open_order_exchange": open_order_exchange,
        "open_order_symbol": open_order_symbol,
        "open_order_count": len(tracked_orders),
        "placed_count": placed_count,
        "canceled_count": canceled_count,
        "cycle_count": cycle_count,
        "runtime_path": _spot_grid_runtime_path(cfg),
        "last_error": None,
        "updated_at": time.time(),
    }
    try:
        await state.set_spot_grid_runtime(runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            grid_cfg = runtime_cfg.spot_grid
            interval = max(1.0, runtime_cfg.poll_seconds)
            started = time.monotonic()
            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            live_allowed, status, reason = _spot_grid_gate_status(
                runtime_cfg,
                strategy_paused=strategy_pauses.get("spot_grid", False),
                program_running=program_running,
            )
            try:
                current_tracking_key = (grid_cfg.exchange, grid_cfg.symbol)
                previous_tracking_key = (open_order_exchange, open_order_symbol)
                if (
                    tracked_orders
                    and previous_tracking_key != ("", "")
                    and previous_tracking_key != current_tracking_key
                ):
                    cancel_cfg = replace(
                        runtime_cfg,
                        spot_grid=replace(
                            runtime_cfg.spot_grid,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                    )
                    (
                        cancel_payload,
                        remaining_orders,
                    ) = await _cancel_tracked_spot_grid_orders(
                        cancel_cfg,
                        manager,
                        tracked_orders,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                        last_cancel_at = time.time()
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="spot_grid_task",
                    )
                    tracked_orders = remaining_orders
                    if tracked_orders:
                        runtime = {
                            **runtime,
                            "status": "cancel_retry",
                            "mode": "live" if live_allowed else "dry_run",
                            "reason": (
                                "previous spot grid orders must be canceled "
                                "before switching exchange or symbol"
                            ),
                            "config": spot_grid_config_to_dict(grid_cfg),
                            "open_order_ids": _tracked_spot_grid_order_ids(
                                tracked_orders
                            ),
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(tracked_orders),
                            "last_execution": cancel_payload,
                            "last_error": cancel_payload.get("open_order_sync_error"),
                            "updated_at": time.time(),
                        }
                        await state.set_spot_grid_runtime(runtime)
                        save_spot_grid_runtime_state(
                            _spot_grid_runtime_path(cancel_cfg),
                            runtime_state_from_tracked(
                                tracked_orders,
                                exchange=open_order_exchange,
                                symbol=open_order_symbol,
                                stats=_spot_grid_stats(
                                    placed_count=placed_count,
                                    canceled_count=canceled_count,
                                    cycle_count=cycle_count,
                                    last_cancel_at=last_cancel_at,
                                    previous_mid_price=previous_mid_price,
                                ),
                            ),
                        )
                        sleep_for = max(
                            0.0,
                            interval - (time.monotonic() - started),
                        )
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    open_order_exchange = ""
                    open_order_symbol = ""
                    previous_mid_price = None

                open_order_snapshot: dict[str, Any] = {
                    "source": "memory",
                    "order_ids": _tracked_spot_grid_order_ids(tracked_orders),
                    "open_orders": [],
                    "open_order_count": len(tracked_orders),
                    "error": None,
                }
                order_sync: dict[str, Any] | None = None
                closed_order_error: str | None = None
                open_tracked_orders = list(tracked_orders)
                confirmed_fills = []
                missing_unconfirmed: list[TrackedGridOrder] = []
                if live_allowed or tracked_orders:
                    open_order_snapshot = await _spot_grid_open_order_snapshot(
                        runtime_cfg,
                        manager,
                        _tracked_spot_grid_order_ids(tracked_orders),
                    )
                    if open_order_snapshot.get("error"):
                        runtime = {
                            **runtime,
                            "status": "open_order_sync_error",
                            "mode": "live" if live_allowed else "dry_run",
                            "reason": "could not confirm current spot grid orders",
                            "config": spot_grid_config_to_dict(grid_cfg),
                            "open_order_ids": _tracked_spot_grid_order_ids(
                                tracked_orders
                            ),
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(tracked_orders),
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync_error": open_order_snapshot.get("error"),
                            "last_error": open_order_snapshot.get("error"),
                            "updated_at": time.time(),
                        }
                        await state.set_spot_grid_runtime(runtime)
                        sleep_for = max(
                            0.0,
                            interval - (time.monotonic() - started),
                        )
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    (
                        closed_orders,
                        closed_order_error,
                    ) = await _spot_grid_closed_order_snapshot(runtime_cfg, manager)
                    if closed_order_error:
                        order_sync = {
                            "tracked_before_count": len(tracked_orders),
                            "exchange_open_count": open_order_snapshot.get(
                                "open_order_count",
                                0,
                            ),
                            "closed_order_error": closed_order_error,
                        }
                    else:
                        order_sync = sync_tracked_grid_orders(
                            tracked_orders,
                            open_order_snapshot.get("open_orders", []),
                            closed_orders,
                        )
                        open_tracked_orders = tracked_orders_from_sync(
                            order_sync,
                            "open_tracked_orders",
                        )
                        confirmed_fills = fills_from_sync(order_sync)
                        missing_unconfirmed = tracked_orders_from_sync(
                            order_sync,
                            "missing_unconfirmed",
                        )
                        tracked_orders = open_tracked_orders
                        if tracked_orders:
                            open_order_exchange = grid_cfg.exchange
                            open_order_symbol = grid_cfg.symbol

                if not live_allowed:
                    cancel_payload = None
                    if tracked_orders:
                        cancel_cfg = runtime_cfg
                        if open_order_exchange and open_order_symbol:
                            cancel_cfg = replace(
                                runtime_cfg,
                                spot_grid=replace(
                                    runtime_cfg.spot_grid,
                                    exchange=open_order_exchange,
                                    symbol=open_order_symbol,
                                ),
                            )
                        (
                            cancel_payload,
                            tracked_orders,
                        ) = await _cancel_tracked_spot_grid_orders(
                            cancel_cfg,
                            manager,
                            tracked_orders,
                        )
                        canceled_count += int(
                            cancel_payload.get("canceled_count", 0) or 0
                        )
                        if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                            last_cancel_at = time.time()
                        write_trade_event(cancel_cfg.trade_log, cancel_payload)
                        write_strategy_timeline_from_payload(
                            cancel_cfg.strategy_timeline,
                            cancel_payload,
                            source="spot_grid_task",
                        )
                        if not tracked_orders:
                            open_order_exchange = ""
                            open_order_symbol = ""
                    runtime = {
                        "status": (
                            "cancel_retry"
                            if tracked_orders and cancel_payload
                            else status
                        ),
                        "mode": "paused" if status == "paused" else "dry_run",
                        "reason": (
                            cancel_payload.get("reason")
                            if tracked_orders and cancel_payload
                            else reason
                        ),
                        "config": spot_grid_config_to_dict(grid_cfg),
                        "open_order_ids": _tracked_spot_grid_order_ids(tracked_orders),
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(tracked_orders),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": order_sync,
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "runtime_path": _spot_grid_runtime_path(runtime_cfg),
                        "last_execution": cancel_payload,
                        "last_error": None,
                        "updated_at": time.time(),
                    }
                    await state.set_spot_grid_runtime(runtime)
                    save_spot_grid_runtime_state(
                        _spot_grid_runtime_path(runtime_cfg),
                        runtime_state_from_tracked(
                            tracked_orders,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                            stats=_spot_grid_stats(
                                placed_count=placed_count,
                                canceled_count=canceled_count,
                                cycle_count=cycle_count,
                                last_cancel_at=last_cancel_at,
                                previous_mid_price=previous_mid_price,
                            ),
                        ),
                    )
                else:
                    if closed_order_error:
                        runtime = {
                            **runtime,
                            "status": "closed_order_sync_error",
                            "mode": "live",
                            "reason": "could not confirm recent spot grid fills",
                            "config": spot_grid_config_to_dict(grid_cfg),
                            "open_order_ids": _tracked_spot_grid_order_ids(
                                tracked_orders
                            ),
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(tracked_orders),
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync": order_sync,
                            "last_error": closed_order_error,
                            "updated_at": time.time(),
                        }
                        await state.set_spot_grid_runtime(runtime)
                        sleep_for = max(
                            0.0,
                            interval - (time.monotonic() - started),
                        )
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    cycle_count += 1
                    order_book = await load_spot_grid_order_book(runtime_cfg, manager)
                    preview_payload = await run_spot_grid_cycle(
                        runtime_cfg,
                        manager,
                        live=False,
                        previous_mid_price=previous_mid_price,
                        last_cancel_at=last_cancel_at,
                        order_book=order_book,
                    )
                    plan_payload = (
                        preview_payload.get("plan")
                        if isinstance(preview_payload.get("plan"), dict)
                        else None
                    )
                    if plan_payload and isinstance(
                        plan_payload.get("mid_price"),
                        (int, float),
                    ):
                        previous_mid_price = float(plan_payload["mid_price"])

                    payload: dict[str, Any] | None = None
                    action = "watching"
                    cancel_payload = None
                    if preview_payload.get("status") not in {
                        "planned",
                        "unchanged",
                    }:
                        if tracked_orders:
                            (
                                cancel_payload,
                                tracked_orders,
                            ) = await _cancel_tracked_spot_grid_orders(
                                runtime_cfg,
                                manager,
                                tracked_orders,
                            )
                            canceled_count += int(
                                cancel_payload.get("canceled_count", 0) or 0
                            )
                            if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                                last_cancel_at = time.time()
                            write_trade_event(runtime_cfg.trade_log, cancel_payload)
                            write_strategy_timeline_from_payload(
                                runtime_cfg.strategy_timeline,
                                cancel_payload,
                                source="spot_grid_task",
                            )
                            if not tracked_orders:
                                open_order_exchange = ""
                                open_order_symbol = ""
                        action = "cancel_out_of_range"
                    elif missing_unconfirmed:
                        if not grid_cfg.auto_rebuild:
                            (
                                cancel_payload,
                                tracked_orders,
                            ) = await _cancel_tracked_spot_grid_orders(
                                runtime_cfg,
                                manager,
                                tracked_orders,
                            )
                            canceled_count += int(
                                cancel_payload.get("canceled_count", 0) or 0
                            )
                            if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                                last_cancel_at = time.time()
                            write_trade_event(runtime_cfg.trade_log, cancel_payload)
                            write_strategy_timeline_from_payload(
                                runtime_cfg.strategy_timeline,
                                cancel_payload,
                                source="spot_grid_task",
                            )
                            action = "manual_rebuild_required"
                        else:
                            action = "full_rebuild_after_unknown_order_gap"
                            payload = await run_spot_grid_cycle(
                                runtime_cfg,
                                manager,
                                live=True,
                                replace_order_ids=_tracked_spot_grid_order_ids(
                                    tracked_orders
                                ),
                                previous_mid_price=previous_mid_price,
                                last_cancel_at=last_cancel_at,
                                order_book=order_book,
                            )
                    elif confirmed_fills:
                        action = "replace_filled_orders"
                        payload = await run_spot_grid_cycle(
                            runtime_cfg,
                            manager,
                            live=True,
                            tracked_orders=tracked_orders,
                            replacement_fills=confirmed_fills,
                            previous_mid_price=previous_mid_price,
                            last_cancel_at=last_cancel_at,
                            order_book=order_book,
                        )
                    elif not tracked_orders:
                        action = "place_initial_grid"
                        payload = await run_spot_grid_cycle(
                            runtime_cfg,
                            manager,
                            live=True,
                            previous_mid_price=previous_mid_price,
                            last_cancel_at=last_cancel_at,
                            order_book=order_book,
                        )

                    execution = {}
                    if payload is not None:
                        payload["runtime_strategy"] = "spot_grid"
                        payload["runtime_action"] = action
                        write_trade_event(runtime_cfg.trade_log, payload)
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            payload,
                            source="spot_grid_task",
                        )
                        execution = (
                            payload.get("execution")
                            if isinstance(payload.get("execution"), dict)
                            else {}
                        )
                        placed_count += int(execution.get("placed_count", 0) or 0)
                        canceled_count += int(execution.get("canceled_count", 0) or 0)
                        if int(execution.get("canceled_count", 0) or 0) > 0:
                            last_cancel_at = time.time()
                        placed_orders = [
                            TrackedGridOrder.from_dict(row)
                            for row in execution.get("placed_orders", [])
                            if isinstance(row, dict)
                        ]
                        if payload.get("status") == "placed":
                            if action == "replace_filled_orders":
                                tracked_orders = [*tracked_orders, *placed_orders]
                            else:
                                tracked_orders = placed_orders
                            open_order_exchange = (
                                grid_cfg.exchange if tracked_orders else ""
                            )
                            open_order_symbol = (
                                grid_cfg.symbol if tracked_orders else ""
                            )
                        elif payload.get("status") == "cancel_retry":
                            remaining_ids = [
                                str(order_id)
                                for order_id in execution.get(
                                    "remaining_open_order_ids",
                                    [],
                                )
                                if order_id
                            ]
                            tracked_orders = (
                                _tracked_spot_grid_orders_for_ids(
                                    tracked_orders,
                                    remaining_ids,
                                )
                                or tracked_orders
                            )
                        elif payload.get("status") == "execution_error":
                            remaining_ids = [
                                str(order_id)
                                for order_id in execution.get(
                                    "remaining_open_order_ids",
                                    [],
                                )
                                if order_id
                            ]
                            tracked_orders = [
                                *tracked_orders,
                                *_tracked_spot_grid_orders_for_ids(
                                    placed_orders,
                                    remaining_ids,
                                ),
                            ]
                        elif payload.get("status") in {
                            "no_replacement",
                            "unchanged",
                        }:
                            tracked_orders = list(tracked_orders)

                    status_payload = payload or preview_payload
                    if action == "manual_rebuild_required":
                        status_payload = {
                            **preview_payload,
                            "status": "manual_rebuild_required",
                            "reason": (
                                "a tracked grid order disappeared but "
                                "spot_grid.auto_rebuild is false"
                            ),
                            "execution": cancel_payload,
                        }
                    elif cancel_payload is not None and payload is None:
                        status_payload = {
                            **preview_payload,
                            "execution": cancel_payload,
                        }
                    runtime = {
                        "status": status_payload.get("status", "watching"),
                        "mode": "live",
                        "reason": status_payload.get("reason"),
                        "action": action,
                        "config": spot_grid_config_to_dict(grid_cfg),
                        "open_order_ids": _tracked_spot_grid_order_ids(tracked_orders),
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(tracked_orders),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": order_sync,
                        "confirmed_fill_count": len(confirmed_fills),
                        "missing_unconfirmed_count": len(missing_unconfirmed),
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "runtime_path": _spot_grid_runtime_path(runtime_cfg),
                        "last_plan": status_payload.get("plan"),
                        "last_risk": status_payload.get("risk"),
                        "last_execution": status_payload.get("execution"),
                        "last_error": None,
                        "market_data": status_payload.get("market_data"),
                        "updated_at": time.time(),
                    }
                    await state.set_spot_grid_runtime(runtime)
                    save_spot_grid_runtime_state(
                        _spot_grid_runtime_path(runtime_cfg),
                        runtime_state_from_tracked(
                            tracked_orders,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                            stats=_spot_grid_stats(
                                placed_count=placed_count,
                                canceled_count=canceled_count,
                                cycle_count=cycle_count,
                                last_cancel_at=last_cancel_at,
                                previous_mid_price=previous_mid_price,
                            ),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                runtime = {
                    **runtime,
                    "status": "error",
                    "mode": "live" if live_allowed else "dry_run",
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                    "updated_at": time.time(),
                }
                await state.set_spot_grid_runtime(runtime)

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()


REBALANCE_MARKET_DATA_TIMEOUT_SECONDS = 15.0
