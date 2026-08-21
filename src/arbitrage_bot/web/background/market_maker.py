from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from ..coordination import (
    coordination_blocked_sides,
)
from ..state import MonitorState

from ...asset_ledger import AssetLedgerStore
from ...config import BotConfig
from ...exchanges import ExchangeManager
from ...market_maker import (
    cancel_order_ids as cancel_market_maker_order_ids,
    run_cycle as run_market_maker_cycle,
)
from ...models import OrderBookSnapshot
from ...orderbook_cache import OrderBookCache
from ...portfolio_metrics import (
    _portfolio_position_for_symbol,
)
from ...strategy_timeline import (
    write_strategy_timeline_from_payload,
)
from ...trade_log import write_trade_event
from ...user_live_strategies import (
    user_market_maker_binding,
    user_market_maker_bindings,
)
from ...user_workspace import UserWorkspaceStore
from ...web_config import (
    market_maker_config_to_dict,
    market_maker_configs_for_runtime,
)
from ..core import (
    _all_account_exchanges,
    _find_exchange_by_key,
    _normalize_order,
    _normalize_trade,
    _risk_account_enabled,
    _risk_strategy_enabled,
)
from ..services.shared import _market_maker_fill_source
from .common import _complete_market_maker_cycle_on_shutdown
from .spot_grid import _raw_client_order_id, _raw_order_id

MARKET_MAKER_FILL_SYNC_SECONDS = 30.0
MARKET_MAKER_FILL_SYNC_LIMIT = 200


def _market_maker_fill_rows(
    exchange: Any,
    maker_cfg: Any,
    raw_trades: list[dict[str, Any]],
    *,
    known_order_ids: set[str],
) -> list[dict[str, Any]]:
    prefix = str(maker_cfg.client_order_prefix or "").strip()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float | None]] = set()
    for raw in raw_trades:
        if not isinstance(raw, dict):
            continue
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        order_id = str(raw.get("order") or info.get("orderId") or "")
        client_order_id = _raw_client_order_id(raw)
        comparable_client_id = (
            client_order_id[2:]
            if client_order_id.startswith("t-")
            else client_order_id
        )
        belongs_to_instance = bool(
            (prefix and comparable_client_id.startswith(prefix))
            or (order_id and order_id in known_order_ids)
        )
        if not belongs_to_instance:
            continue
        row = {
            **_normalize_trade(exchange, raw, maker_cfg.symbol),
            "client_order_id": client_order_id,
            "source": "market_maker",
            "strategy": "market_maker",
            "strategy_instance_id": maker_cfg.id,
        }
        identity = (
            str(row.get("id") or ""),
            str(row.get("order_id") or ""),
            row.get("timestamp"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: float(row.get("timestamp") or 0.0),
        reverse=True,
    )


async def _sync_market_maker_fills(
    cfg: BotConfig,
    manager: ExchangeManager,
    maker_cfg: Any,
    *,
    known_order_ids: set[str],
) -> dict[str, Any]:
    exchange = _find_exchange_by_key(cfg, maker_cfg.exchange)
    raw_trades = await manager.fetch_my_trades(
        exchange,
        symbol=maker_cfg.symbol,
        limit=MARKET_MAKER_FILL_SYNC_LIMIT,
    )
    rows = _market_maker_fill_rows(
        exchange,
        maker_cfg,
        raw_trades,
        known_order_ids=known_order_ids,
    )
    ledger_result = AssetLedgerStore(cfg.asset_ledger).record_fills(
        account_key=maker_cfg.exchange,
        trades=rows,
        source=_market_maker_fill_source(maker_cfg.id),
    )
    return {
        **ledger_result,
        "fill_count": len(rows),
        "latest_fill_at": (
            max(float(row.get("timestamp") or 0.0) for row in rows) / 1000.0
            if rows
            else None
        ),
    }


async def _market_maker_open_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    current_ids: list[str],
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    fallback_ids = sorted({order_id for order_id in current_ids if order_id})
    if not maker_cfg.exchange or not maker_cfg.symbol:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": None,
        }
    exchange = next(
        (
            item
            for item in _all_account_exchanges(cfg)
            if item.key == maker_cfg.exchange
        ),
        None,
    )
    if exchange is None:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"market maker exchange is not configured: {maker_cfg.exchange}",
        }
    try:
        open_orders = await manager.fetch_open_orders(exchange, symbol=maker_cfg.symbol)
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


def _market_maker_runtime_open_orders(
    cfg: BotConfig,
    maker_cfg: Any,
    open_order_snapshot: dict[str, Any],
    active_order_ids: list[str],
) -> list[dict[str, Any]]:
    exchange = _find_exchange_by_key(cfg, maker_cfg.exchange)
    active_ids = {str(order_id) for order_id in active_order_ids if order_id}
    rows: list[dict[str, Any]] = []
    for raw in open_order_snapshot.get("open_orders", []) or []:
        if not isinstance(raw, dict):
            continue
        order_id = _raw_order_id(raw)
        if not order_id or order_id not in active_ids:
            continue
        row = _normalize_order(exchange, raw, maker_cfg.symbol)
        row.update(
            {
                "label": exchange.display_label or exchange.label or exchange.key,
                "source": "market_maker",
                "strategy": "market_maker",
                "strategy_instance_id": maker_cfg.id,
            }
        )
        rows.append(row)
    return rows


def _market_maker_order_sync_delta(
    previous_order_ids: list[str],
    open_order_snapshot: dict[str, Any],
) -> dict[str, Any]:
    previous_ids = {str(order_id) for order_id in previous_order_ids if order_id}
    snapshot_ids = {
        str(order_id)
        for order_id in open_order_snapshot.get("order_ids", []) or []
        if order_id
    }
    source = str(open_order_snapshot.get("source") or "memory")
    exchange_confirmed = source == "exchange" and not open_order_snapshot.get("error")
    missing_tracked_ids = (
        sorted(previous_ids - snapshot_ids) if exchange_confirmed else []
    )
    new_exchange_ids = sorted(snapshot_ids - previous_ids) if exchange_confirmed else []
    changed = bool(
        exchange_confirmed
        and previous_ids
        and (missing_tracked_ids or new_exchange_ids)
    )
    return {
        "source": source,
        "exchange_confirmed": exchange_confirmed,
        "tracked_before_sync": sorted(previous_ids),
        "exchange_order_ids": sorted(snapshot_ids),
        "missing_tracked_order_ids": missing_tracked_ids,
        "new_exchange_order_ids": new_exchange_ids,
        "changed": changed,
        "checked_at": time.time(),
    }


def _market_maker_force_replace_reason(
    open_order_ids: list[str],
    previous_plan: dict[str, Any] | None,
    *,
    order_sync: dict[str, Any] | None = None,
    existing_open_orders: list[dict[str, Any]] | None = None,
    config_changed: bool = False,
) -> str | None:
    if config_changed and open_order_ids:
        return "market maker configuration changed"
    if order_sync and order_sync.get("changed"):
        return "exchange open orders differ from tracked MM ids; assuming fill/cancel drift"
    for order in existing_open_orders or []:
        try:
            amount = float(order.get("amount") or 0.0)
            filled = float(order.get("filled") or 0.0)
            remaining_raw = order.get("remaining")
            remaining = float(remaining_raw) if remaining_raw is not None else amount
        except (TypeError, ValueError):
            continue
        tolerance = max(abs(amount), 1.0) * 1e-10
        if filled > tolerance or remaining < amount - tolerance:
            return "an MM order is partially filled; rebuilding the full ladder"
    if not open_order_ids or not previous_plan:
        return None
    previous_orders = previous_plan.get("orders")
    if not isinstance(previous_orders, list):
        return None
    if len(open_order_ids) != len(previous_orders):
        return (
            "open order count differs from previous MM plan; assuming fill/cancel drift"
        )
    return None


def _market_maker_should_force_replace(
    open_order_ids: list[str],
    previous_plan: dict[str, Any] | None,
    *,
    order_sync: dict[str, Any] | None = None,
) -> bool:
    return (
        _market_maker_force_replace_reason(
            open_order_ids,
            previous_plan,
            order_sync=order_sync,
        )
        is not None
    )


def _market_maker_gate_status(
    cfg: BotConfig,
    *,
    strategy_paused: bool,
    program_running: bool,
) -> tuple[bool, str, str]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.enabled:
        return False, "disabled", "market_maker.enabled is false"
    if not maker_cfg.live_enabled:
        return False, "dry_run", "market_maker.live_enabled is false"
    if not program_running:
        return False, "program_paused", "program is paused"
    if strategy_paused:
        return False, "paused", "market_maker strategy is paused"
    if not cfg.risk.enabled or not cfg.risk.trading_enabled:
        return False, "blocked_by_risk", "risk trading is disabled"
    if not cfg.risk.allow_live_trading:
        return False, "blocked_by_risk", "risk.allow_live_trading is false"
    if not cfg.risk.allow_market_maker or not _risk_strategy_enabled(
        cfg,
        "market_maker",
    ):
        return False, "blocked_by_risk", "market_maker strategy is disabled"
    if maker_cfg.exchange and not _risk_account_enabled(cfg, maker_cfg.exchange):
        return False, "blocked_by_risk", f"{maker_cfg.exchange} account is disabled"
    return True, "live", "live"


def _market_maker_cache_max_age_seconds(cfg: BotConfig) -> float:
    poll_age = max(1.0, cfg.market_maker.poll_seconds * 2)
    risk_age = cfg.risk.max_order_book_age_seconds
    if risk_age > 0:
        return min(risk_age, poll_age)
    return poll_age


async def _cached_market_maker_order_book(
    cfg: BotConfig,
    cache: OrderBookCache,
) -> tuple[OrderBookSnapshot | None, dict[str, Any]]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.exchange or not maker_cfg.symbol:
        return None, {}
    max_age_seconds = _market_maker_cache_max_age_seconds(cfg)
    try:
        exchange_cfg = _find_exchange_by_key(cfg, maker_cfg.exchange)
        depth = max(cfg.order_book_depth, maker_cfg.levels)
        await cache.ensure_watch(exchange_cfg, maker_cfg.symbol, depth)
        snapshot = cache.get(
            maker_cfg.exchange,
            maker_cfg.symbol,
            max_age_seconds=max_age_seconds,
        )
        status = cache.status(
            maker_cfg.exchange,
            maker_cfg.symbol,
            max_age_seconds=max_age_seconds,
        )
        status["using_cached"] = snapshot is not None
        return snapshot, status
    except Exception as exc:  # noqa: BLE001
        return None, {
            "exchange": maker_cfg.exchange,
            "symbol": maker_cfg.symbol,
            "source": None,
            "fresh": False,
            "using_cached": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _private_balance_total(balance: dict[str, Any], currency: str) -> float:
    code = str(currency or "").upper()
    total = balance.get("total")
    if isinstance(total, dict):
        try:
            return float(total.get(code) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    row = balance.get(code)
    if isinstance(row, dict):
        try:
            return float(row.get("total") or row.get("free") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _market_order_reconciliation_is_clear(
    reliability: dict[str, Any],
    *,
    exchange: str,
    symbol: str,
) -> bool:
    if not reliability.get("enabled"):
        return False
    try:
        pending_count = int(reliability.get("pending_count") or 0)
    except (TypeError, ValueError):
        return False
    if pending_count <= 0:
        return True
    resources = reliability.get("quarantined_resources")
    if not isinstance(resources, list) or not resources:
        return False
    return not any(
        isinstance(row, dict)
        and str(row.get("exchange") or "") == exchange
        and str(row.get("symbol") or "") == symbol
        for row in resources
    )


async def _market_maker_instance_task_loop(
    cfg: BotConfig,
    state: MonitorState,
    instance_id: str,
    user_workspace_store: UserWorkspaceStore | None = None,
    user_strategy_id: str = "",
) -> None:
    manager = ExchangeManager()
    orderbook_cache = OrderBookCache(manager)
    open_order_ids: list[str] = []
    open_order_exchange = ""
    open_order_symbol = ""
    placed_count = 0
    canceled_count = 0
    cycle_count = 0
    last_cancel_at: float | None = None
    previous_mid_price: float | None = None
    previous_plan: dict[str, Any] | None = None
    last_maker_cfg = None
    last_instance_runtime_cfg: BotConfig | None = None
    active_maker_cfg = None
    previous_live_allowed = False
    last_start_recovery: dict[str, Any] | None = None
    start_reconciliation_block_reason: str | None = None
    known_order_ids: set[str] = set()
    last_fill_sync_monotonic = 0.0
    fill_sync: dict[str, Any] = {
        "status": "waiting" if user_strategy_id else "not_required",
        "recent_fill_count": 0,
        "new_fill_count": 0,
        "latest_fill_at": None,
        "last_finished": None,
        "error": None,
    }

    async def publish_runtime(payload: dict[str, Any]) -> None:
        await state.set_market_maker_instance_runtime(
            instance_id,
            {
                **payload,
                "fill_sync": dict(fill_sync),
            },
        )

    async def sync_user_fills(
        runtime_cfg: BotConfig,
        maker_cfg: Any,
        *,
        force: bool = False,
    ) -> None:
        nonlocal fill_sync, last_fill_sync_monotonic
        if not user_strategy_id:
            return
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - last_fill_sync_monotonic
            < MARKET_MAKER_FILL_SYNC_SECONDS
        ):
            return
        last_fill_sync_monotonic = now_monotonic
        try:
            result = await _sync_market_maker_fills(
                runtime_cfg,
                manager,
                maker_cfg,
                known_order_ids=known_order_ids,
            )
            fill_sync = {
                "status": "ok",
                "recent_fill_count": int(result.get("fill_count") or 0),
                "new_fill_count": int(result.get("new_fill_count") or 0),
                "latest_fill_at": result.get("latest_fill_at"),
                "last_finished": time.time(),
                "error": None,
            }
            if fill_sync["new_fill_count"]:
                write_trade_event(
                    runtime_cfg.trade_log,
                    {
                        "type": "market_maker_fill_sync",
                        "strategy": "market_maker",
                        "runtime_strategy": "market_maker",
                        "strategy_instance_id": instance_id,
                        "mode": "live",
                        "status": "fills_recorded",
                        "plan": {
                            "exchange": maker_cfg.exchange,
                            "symbol": maker_cfg.symbol,
                        },
                        "fill_count": fill_sync["new_fill_count"],
                        "recent_fill_count": fill_sync["recent_fill_count"],
                        "latest_fill_at": fill_sync["latest_fill_at"],
                    },
                )
        except Exception as exc:  # noqa: BLE001
            fill_sync = {
                **fill_sync,
                "status": "error",
                "new_fill_count": 0,
                "last_finished": time.time(),
                "error": f"{exc.__class__.__name__}: {exc}",
            }
    runtime: dict[str, Any] = {
        "id": instance_id,
        "status": "starting",
        "mode": "dry_run",
        "open_order_ids": [],
        "open_order_exchange": "",
        "open_order_symbol": "",
        "open_order_count": 0,
        "open_orders": [],
        "open_order_details_complete": False,
        "placed_count": 0,
        "canceled_count": 0,
        "cycle_count": 0,
        "last_error": None,
        "market_data": None,
        "open_order_sync": None,
        "coordination_hold": None,
        "updated_at": time.time(),
    }
    try:
        await publish_runtime(runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            maker_cfg = next(
                (
                    item
                    for item in market_maker_configs_for_runtime(runtime_cfg)
                    if item.id == instance_id
                ),
                None,
            )
            if (
                maker_cfg is None
                and user_workspace_store is not None
                and user_strategy_id
            ):
                user_strategy = user_workspace_store.get_strategy(user_strategy_id)
                user_binding = (
                    user_market_maker_binding(
                        runtime_cfg,
                        user_workspace_store,
                        user_strategy,
                    )
                    if user_strategy is not None
                    else None
                )
                if user_binding is not None:
                    runtime_cfg = user_binding.runtime_config
                    maker_cfg = user_binding.config
            if maker_cfg is None:
                cancel_payload = None
                removal_sync: dict[str, Any] | None = None
                if (
                    user_strategy_id
                    and last_maker_cfg is not None
                    and last_instance_runtime_cfg is not None
                ):
                    await sync_user_fills(
                        last_instance_runtime_cfg,
                        last_maker_cfg,
                    )
                if (
                    open_order_ids
                    and last_maker_cfg is not None
                    and last_instance_runtime_cfg is not None
                ):
                    cancel_cfg = replace(
                        last_instance_runtime_cfg,
                        market_maker=replace(
                            last_maker_cfg,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                        market_makers=[last_maker_cfg],
                    )
                    cancel_payload = await cancel_market_maker_order_ids(
                        cancel_cfg,
                        manager,
                        open_order_ids,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="market_maker_task",
                    )
                    removal_sync = await _market_maker_open_order_snapshot(
                        cancel_cfg,
                        manager,
                        open_order_ids,
                    )
                    open_order_ids = [
                        str(order_id)
                        for order_id in removal_sync.get("order_ids", [])
                        if order_id
                    ]
                    if not open_order_ids and not removal_sync.get("error"):
                        open_order_exchange = ""
                        open_order_symbol = ""
                removal_open_orders = (
                    _market_maker_runtime_open_orders(
                        cancel_cfg,
                        last_maker_cfg,
                        removal_sync,
                        open_order_ids,
                    )
                    if removal_sync is not None
                    and last_maker_cfg is not None
                    and last_instance_runtime_cfg is not None
                    else []
                )
                removal_pending = bool(
                    open_order_ids or (removal_sync or {}).get("error")
                )
                runtime = {
                    **runtime,
                    "id": instance_id,
                    "status": "remove_cancel_retry" if removal_pending else "removed",
                    "mode": "paused",
                    "reason": (
                        "market maker instance was removed; cancellation confirmation is pending"
                        if removal_pending
                        else "market maker instance removed"
                    ),
                    "open_order_ids": open_order_ids,
                    "open_order_count": len(open_order_ids),
                    "open_orders": removal_open_orders,
                    "open_order_details_complete": bool(
                        removal_sync
                        and removal_sync.get("source") == "exchange"
                        and not removal_sync.get("error")
                    ),
                    "placed_count": placed_count,
                    "canceled_count": canceled_count,
                    "last_execution": cancel_payload,
                    "open_order_sync_error": (removal_sync or {}).get("error"),
                    "last_error": (removal_sync or {}).get("error"),
                    "updated_at": time.time(),
                }
                await publish_runtime(runtime)
                if removal_pending:
                    await asyncio.sleep(1.0)
                    continue
                return
            maker_cfg = replace(maker_cfg, id=instance_id)
            last_maker_cfg = maker_cfg
            runtime_cfg = replace(
                runtime_cfg,
                market_maker=maker_cfg,
                market_makers=[maker_cfg],
            )
            last_instance_runtime_cfg = runtime_cfg
            interval = max(1.0, maker_cfg.poll_seconds)
            started = time.monotonic()
            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            live_allowed, status, reason = _market_maker_gate_status(
                runtime_cfg,
                strategy_paused=strategy_pauses.get("market_maker", False),
                program_running=program_running,
            )
            if not live_allowed:
                previous_live_allowed = False
                start_reconciliation_block_reason = None
            elif start_reconciliation_block_reason and (
                _market_order_reconciliation_is_clear(
                    manager.order_reliability_summary(),
                    exchange=maker_cfg.exchange,
                    symbol=maker_cfg.symbol,
                )
            ):
                start_reconciliation_block_reason = None
                if last_start_recovery is not None:
                    last_start_recovery = {
                        **last_start_recovery,
                        "status": "ok",
                        "unresolved_count": 0,
                        "auto_cleared_at": time.time(),
                    }
            coordination_hold = await state.coordination_hold_for(
                maker_cfg.exchange,
                maker_cfg.symbol,
                requester=f"market_maker:{instance_id}",
            )
            coordination_sides = coordination_blocked_sides(
                coordination_hold,
                maker_cfg.exchange,
                maker_cfg.symbol,
            )
            if coordination_hold is not None:
                live_allowed = False
                status = "coordinating"
                reason = str(
                    coordination_hold.get("reason")
                    or "temporarily paused for another strategy"
                )
            starting_live_run = live_allowed and not previous_live_allowed
            previous_live_allowed = live_allowed
            try:
                if starting_live_run:
                    previous_plan = None
                    previous_mid_price = None
                    active_maker_cfg = None
                    placed_count = 0
                    canceled_count = 0
                    cycle_count = 0
                    last_cancel_at = None
                    exchange_cfg = _find_exchange_by_key(
                        runtime_cfg,
                        maker_cfg.exchange,
                    )
                    try:
                        last_start_recovery = (
                            await manager.recover_pending_order_intents(
                                [exchange_cfg],
                                exchange=maker_cfg.exchange,
                                symbol=maker_cfg.symbol,
                                resolve_confirmed_absent=True,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        last_start_recovery = {
                            "status": "error",
                            "unresolved_count": 1,
                            "unresolved": [],
                            "error": f"{exc.__class__.__name__}: {exc}",
                            "checked_at": time.time(),
                        }
                    if int(last_start_recovery.get("unresolved_count") or 0) > 0:
                        unresolved = last_start_recovery.get("unresolved") or []
                        first = unresolved[0] if unresolved else {}
                        start_reconciliation_block_reason = (
                            "an earlier order result is still uncertain; "
                            + str(
                                first.get("recovery_error")
                                or first.get("last_error")
                                or last_start_recovery.get("error")
                                or "exchange reconciliation is required"
                            )
                        )
                    else:
                        start_reconciliation_block_reason = None

                if start_reconciliation_block_reason:
                    live_allowed = False
                    status = "reconciliation_required"
                    reason = start_reconciliation_block_reason

                current_tracking_key = (maker_cfg.exchange, maker_cfg.symbol)
                previous_tracking_key = (open_order_exchange, open_order_symbol)
                if (
                    open_order_ids
                    and previous_tracking_key != ("", "")
                    and previous_tracking_key != current_tracking_key
                ):
                    cancel_cfg = replace(
                        runtime_cfg,
                        market_maker=replace(
                            runtime_cfg.market_maker,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                    )
                    cancel_payload = await cancel_market_maker_order_ids(
                        cancel_cfg,
                        manager,
                        open_order_ids,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    if cancel_payload.get("canceled_count"):
                        last_cancel_at = time.time()
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="market_maker_task",
                    )
                    open_order_ids = []
                    open_order_exchange = ""
                    open_order_symbol = ""
                    previous_plan = None
                    previous_mid_price = None
                    active_maker_cfg = None

                open_order_sync: dict[str, Any] | None = None
                if live_allowed or open_order_ids or coordination_hold is not None:
                    tracked_before_sync = list(open_order_ids)
                    known_order_ids.update(tracked_before_sync)
                    open_order_snapshot = await _market_maker_open_order_snapshot(
                        runtime_cfg,
                        manager,
                        open_order_ids,
                    )
                    open_order_sync = _market_maker_order_sync_delta(
                        tracked_before_sync,
                        open_order_snapshot,
                    )
                    open_order_ids = [
                        str(order_id)
                        for order_id in open_order_snapshot.get("order_ids", [])
                        if order_id
                    ]
                    if open_order_ids:
                        open_order_exchange = maker_cfg.exchange
                        open_order_symbol = maker_cfg.symbol
                else:
                    open_order_snapshot = {
                        "source": "memory",
                        "order_ids": open_order_ids,
                        "open_orders": [],
                        "open_order_count": len(open_order_ids),
                        "error": None,
                    }
                    open_order_sync = _market_maker_order_sync_delta(
                        open_order_ids,
                        open_order_snapshot,
                    )
                runtime_open_orders = _market_maker_runtime_open_orders(
                    runtime_cfg,
                    maker_cfg,
                    open_order_snapshot,
                    open_order_ids,
                )
                await sync_user_fills(runtime_cfg, maker_cfg)
                if not live_allowed:
                    cancel_payload = None
                    open_orders_by_id = {
                        _raw_order_id(order): order
                        for order in open_order_snapshot.get("open_orders", [])
                        if isinstance(order, dict) and _raw_order_id(order)
                    }
                    side_scoped_coordination = bool(
                        coordination_hold is not None
                        and coordination_sides
                        and coordination_sides != {"buy", "sell"}
                    )
                    ids_to_cancel = list(open_order_ids)
                    if side_scoped_coordination and not open_order_snapshot.get(
                        "error"
                    ):
                        ids_to_cancel = [
                            order_id
                            for order_id in open_order_ids
                            if str(
                                open_orders_by_id.get(order_id, {}).get("side") or ""
                            ).lower()
                            in coordination_sides
                        ]
                    if ids_to_cancel:
                        ids_before_cancel = list(open_order_ids)
                        cancel_payload = await cancel_market_maker_order_ids(
                            runtime_cfg,
                            manager,
                            ids_to_cancel,
                        )
                        canceled_count += int(
                            cancel_payload.get("canceled_count", 0) or 0
                        )
                        if cancel_payload.get("canceled_count"):
                            last_cancel_at = time.time()
                        write_trade_event(runtime_cfg.trade_log, cancel_payload)
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            cancel_payload,
                            source="market_maker_task",
                        )
                        open_order_snapshot = await _market_maker_open_order_snapshot(
                            runtime_cfg,
                            manager,
                            ids_before_cancel,
                        )
                        open_order_ids = [
                            str(order_id)
                            for order_id in open_order_snapshot.get("order_ids", [])
                            if order_id
                        ]
                        open_order_sync = _market_maker_order_sync_delta(
                            ids_before_cancel,
                            open_order_snapshot,
                        )
                        if not open_order_ids and not open_order_snapshot.get("error"):
                            open_order_exchange = ""
                            open_order_symbol = ""
                    runtime_open_orders = _market_maker_runtime_open_orders(
                        runtime_cfg,
                        maker_cfg,
                        open_order_snapshot,
                        open_order_ids,
                    )
                    sync_error = open_order_snapshot.get("error")
                    conflicting_open_order_count = len(open_order_ids)
                    if side_scoped_coordination and not sync_error:
                        conflicting_open_order_count = sum(
                            1
                            for order in open_order_snapshot.get("open_orders", [])
                            if str(order.get("side") or "").lower()
                            in coordination_sides
                        )
                    if sync_error or conflicting_open_order_count:
                        status = (
                            "coordination_cancel_retry"
                            if coordination_hold is not None
                            else "cancel_retry"
                        )
                        reason = (
                            "could not confirm all conflicting MM orders are canceled; "
                            "new orders remain blocked"
                        )
                    runtime = {
                        "status": status,
                        "mode": (
                            "paused"
                            if status
                            in {
                                "paused",
                                "coordinating",
                                "coordination_cancel_retry",
                                "cancel_retry",
                            }
                            else "dry_run"
                        ),
                        "reason": reason,
                        "config": market_maker_config_to_dict(maker_cfg),
                        "open_order_ids": open_order_ids,
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(open_order_ids),
                        "open_orders": runtime_open_orders,
                        "open_order_details_complete": bool(
                            open_order_snapshot.get("source") == "exchange"
                            and not open_order_snapshot.get("error")
                        ),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": open_order_sync,
                        "coordination_blocked_sides": sorted(coordination_sides),
                        "coordination_conflicting_open_order_count": (
                            conflicting_open_order_count
                        ),
                        "coordination_retained_open_order_count": max(
                            0,
                            len(open_order_ids) - conflicting_open_order_count,
                        ),
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "last_error": sync_error,
                        "start_recovery": last_start_recovery,
                        "last_execution": cancel_payload,
                        "market_data": None,
                        "coordination_hold": coordination_hold,
                        "updated_at": time.time(),
                    }
                    await publish_runtime(runtime)
                else:
                    if open_order_snapshot.get("error"):
                        cycle_count += 1
                        runtime = {
                            "status": "open_order_sync_error",
                            "mode": "live",
                            "reason": "could not confirm current open orders",
                            "config": market_maker_config_to_dict(maker_cfg),
                            "open_order_ids": open_order_ids,
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(open_order_ids),
                            "open_orders": runtime_open_orders,
                            "open_order_details_complete": False,
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync_error": open_order_snapshot.get("error"),
                            "open_order_sync": open_order_sync,
                            "placed_count": placed_count,
                            "canceled_count": canceled_count,
                            "cycle_count": cycle_count,
                            "last_error": open_order_snapshot.get("error"),
                            "start_recovery": last_start_recovery,
                            "market_data": None,
                            "coordination_hold": None,
                            "updated_at": time.time(),
                        }
                        await publish_runtime(runtime)
                        sleep_for = max(0.0, interval - (time.monotonic() - started))
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    cycle_count += 1
                    (
                        order_book,
                        market_data_status,
                    ) = await _cached_market_maker_order_book(
                        runtime_cfg,
                        orderbook_cache,
                    )
                    previous_plan_for_cycle = previous_plan
                    config_changed = (
                        active_maker_cfg is not None and maker_cfg != active_maker_cfg
                    )
                    force_replace_reason = _market_maker_force_replace_reason(
                        open_order_ids,
                        previous_plan,
                        order_sync=open_order_sync,
                        existing_open_orders=open_order_snapshot.get("open_orders"),
                        config_changed=config_changed,
                    )
                    force_replace = force_replace_reason is not None
                    if force_replace:
                        previous_plan_for_cycle = None
                    exchange_cfg = _find_exchange_by_key(
                        runtime_cfg,
                        maker_cfg.exchange,
                    )
                    if (
                        maker_cfg.inventory_control_enabled
                        and exchange_cfg.credential_owner_email
                    ):
                        balance = await manager.fetch_balance(exchange_cfg)
                        inventory_base = _private_balance_total(
                            balance,
                            maker_cfg.symbol.split("/", 1)[0],
                        )
                    else:
                        portfolio_snapshot = await state.portfolio_payload()
                        inventory_base = _portfolio_position_for_symbol(
                            portfolio_snapshot,
                            maker_cfg.symbol,
                            cfg=runtime_cfg,
                        )
                    # When force-replacing (fills detected) don't pass open_orders:
                    # _previous_plan_from_open_orders would otherwise reconstruct a
                    # "previous plan" from whatever orders remain, which can trick
                    # the reprice-threshold check into returning "unchanged" and
                    # skipping the full-grid rebuild we explicitly want.
                    existing_open_orders_for_cycle = (
                        None
                        if force_replace
                        else open_order_snapshot.get("open_orders")
                    )
                    (
                        payload,
                        shutdown_requested,
                    ) = await _complete_market_maker_cycle_on_shutdown(
                        run_market_maker_cycle(
                            runtime_cfg,
                            manager,
                            live=True,
                            replace_existing=False,
                            replace_order_ids=open_order_ids,
                            previous_plan=previous_plan_for_cycle,
                            existing_open_orders=existing_open_orders_for_cycle,
                            previous_mid_price=previous_mid_price,
                            last_cancel_at=last_cancel_at,
                            order_book=order_book,
                            inventory_base=inventory_base,
                            force_full_replace=force_replace,
                            force_replace_reason=force_replace_reason,
                        )
                    )
                    if force_replace:
                        payload["force_replace_reason"] = force_replace_reason
                    market_data = (
                        payload.get("market_data")
                        if isinstance(payload.get("market_data"), dict)
                        else {}
                    )
                    if market_data_status:
                        market_data = {
                            **market_data,
                            "cache": market_data_status,
                        }
                    payload["market_data"] = market_data
                    payload["runtime_strategy"] = "market_maker"
                    payload["strategy_instance_id"] = instance_id
                    write_trade_event(runtime_cfg.trade_log, payload)
                    write_strategy_timeline_from_payload(
                        runtime_cfg.strategy_timeline,
                        payload,
                        source="market_maker_task",
                    )
                    active_plan_payload = (
                        payload.get("active_plan")
                        if isinstance(payload.get("active_plan"), dict)
                        else None
                    )
                    if (
                        active_plan_payload
                        and isinstance(
                            active_plan_payload.get("mid_price"),
                            (int, float),
                        )
                        and payload.get("status") in {"placed", "unchanged"}
                    ):
                        previous_plan = active_plan_payload
                        previous_mid_price = float(active_plan_payload["mid_price"])
                        active_maker_cfg = maker_cfg
                    execution = (
                        payload.get("execution")
                        if isinstance(payload.get("execution"), dict)
                        else {}
                    )
                    known_order_ids.update(
                        str(order_id)
                        for order_id in execution.get("placed_order_ids", [])
                        if order_id
                    )
                    placed_count += int(execution.get("placed_count", 0) or 0)
                    canceled_count += int(execution.get("canceled_count", 0) or 0)
                    if int(execution.get("canceled_count", 0) or 0) > 0:
                        last_cancel_at = time.time()
                    open_order_ids = (
                        [
                            str(order_id)
                            for order_id in execution.get("active_order_ids", [])
                            if order_id
                        ]
                        or [
                            str(order_id)
                            for order_id in execution.get("placed_order_ids", [])
                            if order_id
                        ]
                        or [
                            str(order_id)
                            for order_id in execution.get(
                                "remaining_open_order_ids", []
                            )
                            if order_id
                        ]
                        or open_order_ids
                    )
                    if open_order_ids:
                        open_order_exchange = maker_cfg.exchange
                        open_order_symbol = maker_cfg.symbol
                    elif payload.get("status") == "placed":
                        open_order_exchange = ""
                        open_order_symbol = ""
                    detail_snapshot = open_order_snapshot
                    if execution.get("placed_count") or execution.get(
                        "canceled_count"
                    ):
                        confirmed_snapshot = await _market_maker_open_order_snapshot(
                            runtime_cfg,
                            manager,
                            open_order_ids,
                        )
                        if not confirmed_snapshot.get("error"):
                            detail_snapshot = confirmed_snapshot
                    runtime_open_orders = _market_maker_runtime_open_orders(
                        runtime_cfg,
                        maker_cfg,
                        detail_snapshot,
                        open_order_ids,
                    )
                    runtime = {
                        "status": payload.get("status", "unknown"),
                        "mode": "live",
                        "reason": None,
                        "config": market_maker_config_to_dict(maker_cfg),
                        "open_order_ids": open_order_ids,
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(open_order_ids),
                        "open_orders": runtime_open_orders,
                        "open_order_details_complete": bool(
                            detail_snapshot.get("source") == "exchange"
                            and not detail_snapshot.get("error")
                        ),
                        "open_order_detail_source": detail_snapshot.get("source"),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": open_order_sync,
                        "force_replace": force_replace,
                        "force_replace_reason": force_replace_reason,
                        "replacement_mode": payload.get("replacement_mode"),
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "last_plan": payload.get("plan"),
                        "last_risk": payload.get("risk"),
                        "last_execution": execution,
                        "last_error": None,
                        "start_recovery": last_start_recovery,
                        "market_data": payload.get("market_data"),
                        "coordination_hold": None,
                        "updated_at": time.time(),
                    }
                    await publish_runtime(runtime)
                    if shutdown_requested:
                        raise asyncio.CancelledError
            except Exception as exc:  # noqa: BLE001
                runtime = {
                    **runtime,
                    "status": "error",
                    "mode": "live" if live_allowed else "dry_run",
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                    "updated_at": time.time(),
                }
                await publish_runtime(runtime)

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await orderbook_cache.close()
        await manager.close()


async def market_maker_task_loop(
    cfg: BotConfig,
    state: MonitorState,
    user_workspace_store: UserWorkspaceStore | None = None,
) -> None:
    tasks: dict[str, asyncio.Task[None]] = {}
    await state.set_market_maker_runtime(
        {
            "status": "starting",
            "mode": "dry_run",
            "instances": [],
            "instance_count": 0,
            "active_instance_count": 0,
            "open_order_count": 0,
            "placed_count": 0,
            "canceled_count": 0,
            "cycle_count": 0,
            "updated_at": time.time(),
        }
    )
    try:
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            maker_configs = market_maker_configs_for_runtime(runtime_cfg)
            user_strategy_ids: dict[str, str] = {}
            if user_workspace_store is not None:
                user_bindings = user_market_maker_bindings(
                    runtime_cfg,
                    user_workspace_store,
                )
                user_strategy_ids = {
                    binding.config.id: binding.strategy_id
                    for binding in user_bindings
                }
                maker_configs = [
                    *maker_configs,
                    *(binding.config for binding in user_bindings),
                ]
            configured_ids = {maker_cfg.id for maker_cfg in maker_configs}

            for instance_id, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await state.set_market_maker_instance_runtime(
                        instance_id,
                        {
                            "id": instance_id,
                            "status": "error",
                            "mode": "dry_run",
                            "last_error": f"{exc.__class__.__name__}: {exc}",
                            "open_order_ids": [],
                            "open_order_count": 0,
                            "updated_at": time.time(),
                        },
                    )
                del tasks[instance_id]

            for maker_cfg in maker_configs:
                if maker_cfg.id in tasks:
                    continue
                tasks[maker_cfg.id] = asyncio.create_task(
                    _market_maker_instance_task_loop(
                        cfg,
                        state,
                        maker_cfg.id,
                        user_workspace_store,
                        user_strategy_ids.get(maker_cfg.id, ""),
                    )
                )

            for instance_id in list(tasks):
                if instance_id not in configured_ids and tasks[instance_id].done():
                    del tasks[instance_id]

            await asyncio.sleep(1.0)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
