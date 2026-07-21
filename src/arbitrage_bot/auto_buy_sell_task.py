from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .config import BotConfig, ExchangeConfig, SlowExecutionConfig
from .exchanges import ExchangeManager
from .slow_executor import build_plan, cancel_order_ids, run_cycle
from .strategy_timeline import write_strategy_timeline_from_payload
from .trade_log import write_trade_event


RUNNING_TASK_STATUSES = {
    "running",
    "recovering",
    "waiting_for_start_price",
    "waiting_for_fill",
    "waiting_for_interval",
    "stop_cancel_pending",
    "blocked_by_risk",
    "error",
}
TERMINAL_TASK_STATUSES = {
    "complete",
    "stopped",
    "stopped_by_price",
    "below_min_order_quote",
}
TERMINAL_TASK_DETAIL_RETENTION_SECONDS = 3 * 24 * 60 * 60
TERMINAL_TASK_RETAINED_ORDER_IDS = 100
TASK_DUPLICATE_FIELDS = (
    "exchange",
    "symbol",
    "side",
    "start_price",
    "stop_price",
    "price_mode",
    "price_offset_bps",
    "unlimited_total",
    "slice_mode",
)


def default_task_store_path(cfg: BotConfig) -> str:
    return str(Path(cfg.trade_log.path).with_name("auto_buy_sell_tasks.json"))


def slow_execution_config_from_dict(raw: dict[str, Any]) -> SlowExecutionConfig:
    base = SlowExecutionConfig()
    values = {
        item.name: raw[item.name]
        for item in fields(SlowExecutionConfig)
        if item.name in raw
    }
    return replace(base, **values)


def validate_task_config(cfg: SlowExecutionConfig) -> None:
    if not cfg.exchange:
        raise ValueError("exchange is required")
    if not cfg.symbol:
        raise ValueError("symbol is required")
    if cfg.side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if cfg.total_base < 0 or cfg.total_quote < 0:
        raise ValueError("total_base and total_quote must be non-negative")
    if cfg.start_price < 0 or cfg.stop_price < 0:
        raise ValueError("start_price and stop_price must be non-negative")
    if cfg.price_mode not in {"taker", "maker"}:
        raise ValueError("price_mode must be taker or maker")
    if cfg.price_offset_bps < 0:
        raise ValueError("price_offset_bps must be non-negative")
    if cfg.slice_mode not in {"configured", "top_level"}:
        raise ValueError("slice_mode must be configured or top_level")
    if not cfg.unlimited_total and cfg.total_base <= 0 and cfg.total_quote <= 0:
        raise ValueError("total_base or total_quote must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if cfg.order_ttl_seconds < 0:
        raise ValueError("order_ttl_seconds must be non-negative")
    if cfg.slice_mode == "configured":
        slice_sources = [
            cfg.slice_base > 0,
            cfg.slice_quote > 0,
            cfg.slice_base_min > 0 or cfg.slice_base_max > 0,
        ]
        if sum(1 for item in slice_sources if item) != 1:
            raise ValueError(
                "configure exactly one of slice_base, slice_quote, or slice range"
            )
        if cfg.slice_base_min > 0 or cfg.slice_base_max > 0:
            if cfg.slice_base_min <= 0 or cfg.slice_base_max <= 0:
                raise ValueError("slice range min and max must both be positive")
            if cfg.slice_base_max < cfg.slice_base_min:
                raise ValueError("slice range max must be greater than or equal to min")


def _signature_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 12)
    return value


def _task_duplicate_signature(cfg: SlowExecutionConfig) -> tuple[Any, ...]:
    return tuple(
        _signature_value(getattr(cfg, field_name))
        for field_name in TASK_DUPLICATE_FIELDS
    )


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"Auto Buy/Sell exchange is not configured: {key}")


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _trade_cost(raw: dict[str, Any]) -> float:
    cost = _float_value(raw.get("cost"))
    if cost > 0:
        return cost
    amount = _float_value(raw.get("amount"))
    price = _float_value(raw.get("price"))
    return amount * price if amount > 0 and price > 0 else 0.0


def _order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("order") or "")


async def _cancel_and_confirm_order_ids(
    cfg: BotConfig,
    manager: ExchangeManager,
    order_ids: list[str],
) -> dict[str, Any]:
    requested_ids = list(dict.fromkeys(str(order_id) for order_id in order_ids if order_id))
    payload = await cancel_order_ids(cfg, manager, requested_ids)
    confirmation_errors: list[dict[str, str]] = []
    remaining_ids = list(requested_ids)
    try:
        exchange = _find_exchange(cfg, cfg.slow_execution.exchange)
        open_orders = await manager.fetch_open_orders(
            exchange,
            symbol=cfg.slow_execution.symbol,
        )
        open_ids = {_order_id(order) for order in open_orders if _order_id(order)}
        remaining_ids = [order_id for order_id in requested_ids if order_id in open_ids]
    except Exception as exc:  # noqa: BLE001
        confirmation_errors.append({"error": f"{exc.__class__.__name__}: {exc}"})

    payload["remaining_open_order_ids"] = remaining_ids
    payload["confirmed_absent_count"] = len(requested_ids) - len(remaining_ids)
    payload["confirmation_errors"] = confirmation_errors
    payload["cancel_confirmed"] = not remaining_ids and not confirmation_errors
    return payload


def _trade_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("order") or raw.get("order_id") or "")


def _order_fill_amounts(raw: dict[str, Any]) -> tuple[float, float]:
    amount = _float_value(raw.get("filled"))
    cost = _float_value(raw.get("cost"))
    if cost <= 0:
        price = _float_value(raw.get("price"))
        cost = amount * price if amount > 0 and price > 0 else 0.0
    return amount, cost


def _order_fill_timestamp(raw: dict[str, Any]) -> float | None:
    timestamp = _float_value(raw.get("timestamp"))
    if timestamp <= 0:
        return None
    return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp


def _is_final_filled_order(raw: dict[str, Any]) -> bool:
    filled, cost = _order_fill_amounts(raw)
    if filled <= 0 and cost <= 0:
        return False
    status = str(raw.get("status") or "").lower()
    if status in {"closed", "canceled", "cancelled"}:
        return True
    remaining = _float_value(raw.get("remaining"))
    return remaining <= 0


@dataclass
class AutoBuySellTask:
    id: str
    config: dict[str, Any]
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    paused_at: float | None = None
    finished_at: float | None = None
    next_run_at: float = 0.0
    last_cycle_at: float | None = None
    last_fill_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    last_recovered_at: float | None = None
    consecutive_error_count: int = 0
    last_order_recovery: dict[str, Any] | None = None
    last_status: str | None = None
    last_plan: dict[str, Any] | None = None
    last_risk: dict[str, Any] | None = None
    last_execution: dict[str, Any] | None = None
    placed_order_ids: list[str] = field(default_factory=list)
    open_order_ids: list[str] = field(default_factory=list)
    known_trade_ids: list[str] = field(default_factory=list)
    known_filled_order_ids: list[str] = field(default_factory=list)
    order_created_at: dict[str, float] = field(default_factory=dict)
    filled_base: float = 0.0
    filled_quote: float = 0.0
    start_price_triggered: bool = False
    canceled_count: int = 0
    placed_count: int = 0
    cycle_count: int = 0

    @property
    def exec_cfg(self) -> SlowExecutionConfig:
        return slow_execution_config_from_dict(self.config)

    def to_dict(self) -> dict[str, Any]:
        cfg = self.exec_cfg
        row = asdict(self)
        row["config"] = asdict(cfg)
        unlimited_total = cfg.unlimited_total
        base_target_enabled = not unlimited_total and cfg.total_base > 0
        quote_target_enabled = not unlimited_total and cfg.total_quote > 0
        remaining_base = (
            max(0.0, cfg.total_base - self.filled_base)
            if base_target_enabled
            else None
            if unlimited_total
            else 0.0
        )
        remaining_quote = (
            max(0.0, cfg.total_quote - self.filled_quote)
            if quote_target_enabled
            else None
            if unlimited_total
            else 0.0
        )
        progress_mode = (
            "unlimited"
            if unlimited_total
            else "quote"
            if quote_target_enabled
            else "base"
        )
        progress_value = self.filled_quote if quote_target_enabled else self.filled_base
        progress_total = cfg.total_quote if quote_target_enabled else cfg.total_base
        progress_pct = (
            min(100.0, max(0.0, progress_value / progress_total * 100))
            if progress_total > 0 and not unlimited_total
            else 0.0
        )
        return {
            **row,
            "remaining_base": remaining_base,
            "remaining_quote": remaining_quote,
            "progress_mode": progress_mode,
            "progress_pct": progress_pct,
            "progress_label": "Bought" if cfg.side == "buy" else "Sold",
            "open_order_count": len(self.open_order_ids),
            "auto_retry_active": self.status in {"error", "recovering"}
            and self.next_run_at > 0,
        }


def _cleanup_task_summary(task: AutoBuySellTask) -> dict[str, Any]:
    row = task.to_dict()
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "last_status": row.get("last_status"),
        "exchange": config.get("exchange"),
        "symbol": config.get("symbol"),
        "side": config.get("side"),
        "filled_base": row.get("filled_base"),
        "filled_quote": row.get("filled_quote"),
        "remaining_base": row.get("remaining_base"),
        "remaining_quote": row.get("remaining_quote"),
        "progress_mode": row.get("progress_mode"),
        "progress_pct": row.get("progress_pct"),
        "open_order_count": row.get("open_order_count"),
        "placed_count": row.get("placed_count"),
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
        "last_error": row.get("last_error"),
    }


class AutoBuySellTaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[AutoBuySellTask]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("tasks", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        tasks = []
        task_fields = {item.name for item in fields(AutoBuySellTask)}
        for item in items:
            if not isinstance(item, dict):
                continue
            values = {key: value for key, value in item.items() if key in task_fields}
            try:
                task = AutoBuySellTask(**values)
                validate_task_config(task.exec_cfg)
            except (TypeError, ValueError):
                continue
            if task.status == "placing":
                task.status = "running"
            tasks.append(task)
        return tasks

    def save(self, tasks: list[AutoBuySellTask]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        serialized_tasks: list[dict[str, Any]] = []
        for task in tasks:
            row = task.to_dict()
            terminal_at = float(task.finished_at or task.updated_at or 0.0)
            if (
                task.status in TERMINAL_TASK_STATUSES
                and terminal_at > 0
                and now - terminal_at >= TERMINAL_TASK_DETAIL_RETENTION_SECONDS
            ):
                row["placed_order_ids"] = list(task.placed_order_ids)[
                    -TERMINAL_TASK_RETAINED_ORDER_IDS:
                ]
                row["known_filled_order_ids"] = list(task.known_filled_order_ids)[
                    -TERMINAL_TASK_RETAINED_ORDER_IDS:
                ]
                row["open_order_ids"] = []
                row["last_execution"] = {
                    "history_compacted": True,
                    "compacted_at": now,
                    "placed_count": task.placed_count,
                    "filled_base": task.filled_base,
                    "filled_quote": task.filled_quote,
                }
            serialized_tasks.append(row)
        payload = {
            "version": 1,
            "updated_at": now,
            "tasks": serialized_tasks,
        }
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.path)
        os.chmod(self.path, 0o600)


class AutoBuySellTaskService:
    def __init__(self, path: str | Path) -> None:
        self.store = AutoBuySellTaskStore(path)
        self._lock = asyncio.Lock()
        self._tasks = self.store.load()

    async def create_task(self, cfg: SlowExecutionConfig) -> dict[str, Any]:
        validate_task_config(cfg)
        task = AutoBuySellTask(
            id=uuid.uuid4().hex[:12],
            config=asdict(replace(cfg, enabled=True)),
            status="running",
            started_at=time.time(),
            next_run_at=0.0,
        )
        async with self._lock:
            duplicate = self._find_duplicate_unlocked(cfg)
            if duplicate is not None:
                raise ValueError(
                    f"duplicate active Auto Buy/Sell task: {duplicate.id}"
                )
            self._tasks.append(task)
            self.store.save(self._tasks)
            return task.to_dict()

    async def set_paused(self, task_id: str, paused: bool) -> dict[str, Any]:
        async with self._lock:
            task = self._get_task_unlocked(task_id)
            if task.status == "stop_cancel_pending":
                raise ValueError(
                    "cannot pause or resume while order cancellation is pending"
                )
            if paused:
                task.status = "paused"
                task.paused_at = time.time()
            else:
                if task.status in TERMINAL_TASK_STATUSES:
                    raise ValueError(f"cannot resume terminal task: {task.status}")
                task.status = "running"
                task.paused_at = None
                task.next_run_at = 0.0
            task.updated_at = time.time()
            self.store.save(self._tasks)
            return task.to_dict()

    async def enable_market_maker_coordination(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        async with self._lock:
            task = self._get_task_unlocked(task_id)
            if task.status in TERMINAL_TASK_STATUSES:
                raise ValueError(
                    f"cannot enable MM coordination for terminal task: {task.status}"
                )
            if task.open_order_ids:
                raise ValueError(
                    "cannot enable MM coordination while the task has open orders"
                )
            if task.filled_base > 0 or task.filled_quote > 0 or task.placed_count > 0:
                raise ValueError(
                    "MM coordination can only be enabled in place for an unfilled "
                    "task with no submitted orders"
                )
            risk = task.last_risk if isinstance(task.last_risk, dict) else {}
            guard = (
                risk.get("self_trade_guard")
                if isinstance(risk.get("self_trade_guard"), dict)
                else {}
            )
            if task.status != "blocked_by_risk" or not guard.get("blocked"):
                raise ValueError(
                    "task is not currently blocked by the MM self-trade guard"
                )
            task_cfg = task.exec_cfg
            if not task_cfg.block_conflicting_market_maker:
                raise ValueError("MM self-trade guard must remain enabled")
            task.config = asdict(
                replace(
                    task_cfg,
                    coordinate_market_maker=True,
                    block_conflicting_market_maker=True,
                )
            )
            task.status = "running"
            task.last_status = "mm_coordination_enabled"
            task.last_error = None
            task.next_run_at = 0.0
            task.updated_at = time.time()
            self.store.save(self._tasks)
            return task.to_dict()

    async def stop_task(
        self,
        task_id: str,
        cfg: BotConfig,
        manager: ExchangeManager,
        *,
        cancel_open_orders: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            task = self._get_task_unlocked(task_id)
            task_cfg = task.exec_cfg
            open_order_ids = list(task.open_order_ids)
            if task.status in TERMINAL_TASK_STATUSES:
                return task.to_dict()

        cancel_payload: dict[str, Any] | None = None
        if cancel_open_orders and open_order_ids:
            cancel_payload = await _cancel_and_confirm_order_ids(
                replace(cfg, slow_execution=task_cfg),
                manager,
                open_order_ids,
            )
            cancel_payload["task_id"] = task_id
            cancel_payload["status"] = (
                "stop_cancel_pending"
                if cancel_payload.get("remaining_open_order_ids")
                else "stopped"
            )
            cancel_payload["reason"] = "manual_stop"
            cancel_payload["target_status"] = "stopped"
            write_trade_event(cfg.trade_log, cancel_payload)
            write_strategy_timeline_from_payload(
                cfg.strategy_timeline,
                cancel_payload,
                source="auto_buy_sell_task",
            )

        async with self._lock:
            task = self._get_task_unlocked(task_id)
            now = time.time()
            if cancel_payload is not None:
                remaining_ids = list(cancel_payload.get("remaining_open_order_ids", []))
                newly_tracked_ids = [
                    order_id
                    for order_id in task.open_order_ids
                    if order_id not in open_order_ids
                ]
                task.open_order_ids = list(
                    dict.fromkeys([*remaining_ids, *newly_tracked_ids])
                )
                task.canceled_count += min(
                    int(cancel_payload.get("canceled_count", 0) or 0),
                    int(cancel_payload.get("confirmed_absent_count", 0) or 0),
                )
                task.last_execution = {
                    "canceled_count": cancel_payload.get("canceled_count", 0),
                    "cancel_requested_order_ids": open_order_ids,
                    "remaining_open_order_ids": list(task.open_order_ids),
                    "errors": cancel_payload.get("errors", []),
                    "confirmation_errors": cancel_payload.get(
                        "confirmation_errors", []
                    ),
                    "cancel_confirmed": not task.open_order_ids
                    and bool(cancel_payload.get("cancel_confirmed")),
                    "reason": "manual_stop",
                    "target_status": "stopped",
                }
            if task.open_order_ids and cancel_open_orders:
                task.status = "stop_cancel_pending"
                task.last_status = "stop_cancel_pending"
                task.last_error = (
                    "stop requested; waiting for confirmed cancellation of "
                    f"{len(task.open_order_ids)} open order(s)"
                )
                task.finished_at = None
                task.next_run_at = now + max(1.0, min(5.0, task_cfg.interval_seconds))
            else:
                task.status = "stopped"
                task.last_status = "stopped"
                task.last_error = None
                task.finished_at = now
                task.next_run_at = 0.0
            task.updated_at = now
            self.store.save(self._tasks)
            return task.to_dict()

    async def clear_terminal_tasks(self) -> dict[str, Any]:
        async with self._lock:
            removed_tasks = [
                _cleanup_task_summary(task)
                for task in self._tasks
                if task.status in TERMINAL_TASK_STATUSES
            ]
            self._tasks = [
                task for task in self._tasks if task.status not in TERMINAL_TASK_STATUSES
            ]
            self.store.save(self._tasks)
            snapshot = self._snapshot_unlocked()
        return {
            "removed_count": len(removed_tasks),
            "removed_task_ids": [task["id"] for task in removed_tasks],
            "removed_tasks": removed_tasks,
            "tasks": snapshot,
        }

    async def preview_terminal_tasks(self) -> dict[str, Any]:
        async with self._lock:
            terminal_tasks = [
                _cleanup_task_summary(task)
                for task in self._tasks
                if task.status in TERMINAL_TASK_STATUSES
            ]
        return {
            "removed_count": len(terminal_tasks),
            "removed_task_ids": [task["id"] for task in terminal_tasks],
            "removed_tasks": terminal_tasks,
        }

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot_unlocked()

    async def run_due_tasks(
        self,
        cfg: BotConfig,
        manager: ExchangeManager,
        *,
        strategy_paused: bool = False,
        market_maker_paused: bool = False,
        coordinated_market_maker_task_ids: set[str] | None = None,
        program_running: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            tasks = list(self._tasks)

        now = time.time()
        changed = False
        for task in tasks:
            if task.status in TERMINAL_TASK_STATUSES or task.status == "paused":
                continue
            if not program_running:
                task.last_status = "program_paused"
                task.next_run_at = now + 1.0
                changed = True
                continue
            if strategy_paused:
                task.last_status = "strategy_paused"
                task.next_run_at = now + 1.0
                changed = True
                continue
            if task.next_run_at > now:
                continue
            await self._run_task_cycle(
                task,
                cfg,
                manager,
                market_maker_paused=(
                    market_maker_paused
                    or task.id in (coordinated_market_maker_task_ids or set())
                ),
            )
            changed = True

        async with self._lock:
            by_id = {task.id: task for task in tasks}
            self._tasks = [by_id.get(task.id, task) for task in self._tasks]
            if changed:
                self.store.save(self._tasks)
            return self._snapshot_unlocked()

    def _get_task_unlocked(self, task_id: str) -> AutoBuySellTask:
        for task in self._tasks:
            if task.id == task_id:
                return task
        raise ValueError(f"unknown Auto Buy/Sell task: {task_id}")

    def _find_duplicate_unlocked(
        self,
        cfg: SlowExecutionConfig,
    ) -> AutoBuySellTask | None:
        signature = _task_duplicate_signature(cfg)
        for task in self._tasks:
            if task.status in TERMINAL_TASK_STATUSES:
                continue
            if _task_duplicate_signature(task.exec_cfg) == signature:
                return task
        return None

    def _snapshot_unlocked(self) -> dict[str, Any]:
        task_rows = [task.to_dict() for task in self._tasks]
        return {
            "status": "ok",
            "path": str(self.store.path),
            "tasks": task_rows,
            "task_count": len(task_rows),
            "active_count": sum(
                1
                for task in task_rows
                if task["status"] in RUNNING_TASK_STATUSES or task["status"] == "paused"
            ),
            "updated_at": time.time(),
        }

    async def _run_task_cycle(
        self,
        task: AutoBuySellTask,
        cfg: BotConfig,
        manager: ExchangeManager,
        *,
        market_maker_paused: bool = False,
    ) -> None:
        task_cfg = task.exec_cfg
        runtime_cfg = replace(cfg, slow_execution=task_cfg)
        now = time.time()
        task.cycle_count += 1
        task.last_cycle_at = now
        task.updated_at = now
        try:
            if task.status == "stop_cancel_pending":
                await self._retry_pending_stop(task, runtime_cfg, manager)
                return
            if await self._recover_pending_order_state(task, runtime_cfg, manager):
                return
            await self._refresh_task_activity(task, runtime_cfg, manager)
            self._mark_recovered(task)
            if _task_is_complete(task, task_cfg):
                task.status = "complete"
                task.finished_at = time.time()
                task.last_status = "complete"
                return
            if await self._stop_task_if_price_limit_reached(
                task,
                runtime_cfg,
                manager,
            ):
                return
            if task.open_order_ids:
                await self._handle_open_orders(task, runtime_cfg, manager)
                return
            if self._must_wait_for_next_slice(task, task_cfg, now):
                return

            payload, _ = await run_cycle(
                runtime_cfg,
                manager,
                submitted_base=task.filled_base,
                submitted_quote=task.filled_quote,
                start_price_triggered=task.start_price_triggered,
                market_maker_paused=market_maker_paused,
                live=True,
                replace_existing=False,
            )
            payload["task_id"] = task.id
            task.last_status = str(payload.get("status") or "")
            task.last_plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
            task.last_risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else None
            task.last_execution = (
                payload.get("execution")
                if isinstance(payload.get("execution"), dict)
                else None
            )
            task.last_error = None
            task.status = _task_status_from_cycle_status(task.last_status)
            if (
                task_cfg.start_price > 0
                and task.last_status != "waiting_for_start_price"
            ):
                task.start_price_triggered = True

            if task.last_status in TERMINAL_TASK_STATUSES:
                task.finished_at = time.time()
            execution = task.last_execution or {}
            placed_order_ids = [
                str(order_id)
                for order_id in execution.get("placed_order_ids", [])
                if order_id
            ]
            for order_id in placed_order_ids:
                if order_id not in task.placed_order_ids:
                    task.placed_order_ids.append(order_id)
                    task.open_order_ids.append(order_id)
                    task.order_created_at[order_id] = time.time()
            task.placed_count += int(execution.get("placed_count", 0) or 0)
            task.canceled_count += int(execution.get("canceled_count", 0) or 0)
            write_trade_event(cfg.trade_log, payload)
            write_strategy_timeline_from_payload(
                cfg.strategy_timeline,
                payload,
                source="auto_buy_sell_task",
            )
            task.next_run_at = self._next_check_time(task)
        except Exception as exc:  # noqa: BLE001
            if task.status == "stop_cancel_pending":
                task.last_status = "stop_cancel_pending"
                task.last_error = (
                    "stop cancellation retry failed: "
                    f"{exc.__class__.__name__}: {exc}"
                )
            else:
                task.status = "error"
                task.last_status = "error"
                task.last_error = f"{exc.__class__.__name__}: {exc}"
                task.last_error_at = time.time()
                task.consecutive_error_count += 1
            task.next_run_at = time.time() + max(1.0, task_cfg.interval_seconds)

    async def _recover_pending_order_state(
        self,
        task: AutoBuySellTask,
        cfg: BotConfig,
        manager: ExchangeManager,
    ) -> bool:
        summary_getter = getattr(manager, "order_reliability_summary", None)
        recovery_runner = getattr(manager, "recover_pending_order_intents", None)
        if not callable(summary_getter) or not callable(recovery_runner):
            return False
        summary = summary_getter()
        quarantined = any(
            str(row.get("exchange") or "") == task.exec_cfg.exchange
            and str(row.get("symbol") or "") == task.exec_cfg.symbol
            and int(row.get("count") or 0) > 0
            for row in summary.get("quarantined_resources", [])
            if isinstance(row, dict)
        )
        if not quarantined:
            return False

        exchange_cfg = _find_exchange(cfg, task.exec_cfg.exchange)
        recovery = await recovery_runner(
            [exchange_cfg],
            exchange=task.exec_cfg.exchange,
            symbol=task.exec_cfg.symbol,
            resolve_confirmed_absent=True,
        )
        task.last_order_recovery = {
            key: recovery.get(key)
            for key in (
                "status",
                "recovered_count",
                "reconciled_absent_count",
                "unresolved_count",
                "checked_at",
            )
        }
        if int(recovery.get("unresolved_count") or 0) <= 0:
            self._mark_recovered(task)
            return False

        unresolved = recovery.get("unresolved") or []
        first = unresolved[0] if unresolved and isinstance(unresolved[0], dict) else {}
        task.status = "recovering"
        task.last_status = "recovering_order_state"
        task.last_error = str(
            first.get("recovery_error")
            or first.get("last_error")
            or "waiting for exchange order reconciliation"
        )
        task.last_error_at = time.time()
        task.consecutive_error_count += 1
        task.next_run_at = time.time() + max(
            1.0,
            min(10.0, task.exec_cfg.interval_seconds),
        )
        return True

    @staticmethod
    def _mark_recovered(task: AutoBuySellTask) -> None:
        if task.last_error is not None or task.consecutive_error_count > 0:
            task.last_recovered_at = time.time()
        task.last_error = None
        task.last_error_at = None
        task.consecutive_error_count = 0

    async def _retry_pending_stop(
        self,
        task: AutoBuySellTask,
        cfg: BotConfig,
        manager: ExchangeManager,
    ) -> None:
        previous_execution = task.last_execution or {}
        target_status = str(previous_execution.get("target_status") or "stopped")
        if target_status not in {"stopped", "stopped_by_price"}:
            target_status = "stopped"
        reason = str(previous_execution.get("reason") or "manual_stop")
        requested_ids = list(task.open_order_ids)
        if requested_ids:
            payload = await _cancel_and_confirm_order_ids(cfg, manager, requested_ids)
        else:
            payload = {
                "type": "slow_execution_cancel",
                "order_ids": [],
                "canceled_count": 0,
                "errors": [],
                "remaining_open_order_ids": [],
                "confirmed_absent_count": 0,
                "confirmation_errors": [],
                "cancel_confirmed": True,
            }
        remaining_ids = list(payload.get("remaining_open_order_ids", []))
        task.open_order_ids = remaining_ids
        task.canceled_count += min(
            int(payload.get("canceled_count", 0) or 0),
            int(payload.get("confirmed_absent_count", 0) or 0),
        )
        task.last_execution = {
            "canceled_count": payload.get("canceled_count", 0),
            "cancel_requested_order_ids": requested_ids,
            "remaining_open_order_ids": remaining_ids,
            "errors": payload.get("errors", []),
            "confirmation_errors": payload.get("confirmation_errors", []),
            "cancel_confirmed": bool(payload.get("cancel_confirmed")),
            "reason": reason,
            "target_status": target_status,
        }
        now = time.time()
        task.updated_at = now
        if remaining_ids:
            task.status = "stop_cancel_pending"
            task.last_status = "stop_cancel_pending"
            task.last_error = (
                "stop requested; waiting for confirmed cancellation of "
                f"{len(remaining_ids)} open order(s)"
            )
            task.finished_at = None
            task.next_run_at = now + max(
                1.0,
                min(5.0, task.exec_cfg.interval_seconds),
            )
        else:
            task.status = target_status
            task.last_status = target_status
            task.last_error = None
            task.finished_at = now
            task.next_run_at = 0.0
        payload["task_id"] = task.id
        payload["status"] = task.status
        payload["reason"] = reason
        payload["target_status"] = target_status
        write_trade_event(cfg.trade_log, payload)
        write_strategy_timeline_from_payload(
            cfg.strategy_timeline,
            payload,
            source="auto_buy_sell_task",
        )

    async def _stop_task_if_price_limit_reached(
        self,
        task: AutoBuySellTask,
        cfg: BotConfig,
        manager: ExchangeManager,
    ) -> bool:
        task_cfg = task.exec_cfg
        if task_cfg.stop_price <= 0:
            return False

        plan = await build_plan(
            cfg,
            manager,
            submitted_base=task.filled_base,
            submitted_quote=task.filled_quote,
            start_price_triggered=task.start_price_triggered,
        )
        task.last_plan = plan.to_dict()
        if plan.status != "stopped_by_price":
            return False

        open_order_ids = list(task.open_order_ids)
        now = time.time()
        if not open_order_ids:
            task.status = "stopped_by_price"
            task.last_status = "stopped_by_price"
            task.last_error = None
            task.finished_at = now
            task.updated_at = now
            task.next_run_at = 0.0
            return True

        cancel_payload = await _cancel_and_confirm_order_ids(
            cfg,
            manager,
            open_order_ids,
        )
        cancel_payload["task_id"] = task.id
        cancel_payload["reason"] = "stop_price_reached"
        task.open_order_ids = list(cancel_payload.get("remaining_open_order_ids", []))
        task.canceled_count += min(
            int(cancel_payload.get("canceled_count", 0) or 0),
            int(cancel_payload.get("confirmed_absent_count", 0) or 0),
        )
        task.last_execution = {
            "canceled_count": cancel_payload.get("canceled_count", 0),
            "cancel_requested_order_ids": open_order_ids,
            "remaining_open_order_ids": list(task.open_order_ids),
            "errors": cancel_payload.get("errors", []),
            "confirmation_errors": cancel_payload.get("confirmation_errors", []),
            "cancel_confirmed": bool(cancel_payload.get("cancel_confirmed")),
            "reason": "stop_price_reached",
            "target_status": "stopped_by_price",
        }
        task.updated_at = time.time()
        cancel_payload["status"] = (
            "stop_cancel_pending" if task.open_order_ids else "stopped_by_price"
        )
        write_trade_event(cfg.trade_log, cancel_payload)
        write_strategy_timeline_from_payload(
            cfg.strategy_timeline,
            cancel_payload,
            source="auto_buy_sell_task",
        )
        if task.open_order_ids:
            task.status = "stop_cancel_pending"
            task.last_status = "stop_cancel_pending"
            task.last_error = (
                "stop price reached; waiting for confirmed cancellation of "
                f"{len(task.open_order_ids)} open order(s)"
            )
            task.finished_at = None
            task.next_run_at = time.time() + max(
                1.0,
                min(5.0, task_cfg.interval_seconds),
            )
            return True

        task.status = "stopped_by_price"
        task.last_status = "stopped_by_price"
        task.last_error = None
        task.finished_at = time.time()
        task.next_run_at = 0.0
        return True

    async def _handle_open_orders(
        self,
        task: AutoBuySellTask,
        cfg: BotConfig,
        manager: ExchangeManager,
    ) -> None:
        task_cfg = task.exec_cfg
        ttl = task_cfg.order_ttl_seconds
        now = time.time()
        if ttl <= 0:
            task.status = "waiting_for_fill"
            task.last_status = "waiting_for_fill"
            task.last_error = None
            task.next_run_at = now + max(1.0, task_cfg.interval_seconds)
            return
        expired = [
            order_id
            for order_id in task.open_order_ids
            if now - float(task.order_created_at.get(order_id, now)) >= ttl
        ]
        if not expired:
            task.status = "waiting_for_fill"
            task.last_status = "waiting_for_fill"
            task.last_error = None
            task.next_run_at = self._next_check_time(task)
            return

        cancel_payload = await cancel_order_ids(cfg, manager, expired)
        cancel_payload["task_id"] = task.id
        task.canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
        task.open_order_ids = [
            order_id for order_id in task.open_order_ids if order_id not in expired
        ]
        task.last_execution = {
            "canceled_count": cancel_payload.get("canceled_count", 0),
            "canceled_order_ids": expired,
            "errors": cancel_payload.get("errors", []),
        }
        task.last_status = "canceled_stale_orders"
        task.status = "running"
        task.last_error = None
        task.next_run_at = now + max(1.0, task_cfg.interval_seconds)
        write_trade_event(cfg.trade_log, cancel_payload)
        write_strategy_timeline_from_payload(
            cfg.strategy_timeline,
            cancel_payload,
            source="auto_buy_sell_task",
        )
        await self._refresh_task_activity(task, cfg, manager)

    async def _refresh_task_activity(
        self,
        task: AutoBuySellTask,
        cfg: BotConfig,
        manager: ExchangeManager,
    ) -> None:
        if not task.placed_order_ids:
            task.filled_base = 0.0
            task.filled_quote = 0.0
            task.open_order_ids = []
            return

        exchange = _find_exchange(cfg, task.exec_cfg.exchange)
        symbol = task.exec_cfg.symbol
        tracked = set(task.placed_order_ids)
        open_orders = await manager.fetch_open_orders(exchange, symbol=symbol)
        history_limit = min(max(len(tracked), 100), 1000)
        closed_orders = await manager.fetch_closed_orders(
            exchange,
            symbol=symbol,
            limit=history_limit,
        )
        trades = await manager.fetch_my_trades(
            exchange,
            symbol=symbol,
            limit=history_limit,
        )
        open_ids = {
            _order_id(order)
            for order in open_orders
            if _order_id(order) in tracked
        }
        trade_fills: dict[str, dict[str, float]] = {}
        previous_known_trade_ids: set[str] = set(task.known_trade_ids)
        known_trade_ids: set[str] = set(previous_known_trade_ids)
        previous_known_filled_order_ids: set[str] = set(task.known_filled_order_ids)
        known_filled_order_ids: set[str] = set(previous_known_filled_order_ids)
        bootstrap_order_fills = not previous_known_filled_order_ids and (
            task.filled_base > 0 or task.filled_quote > 0
        )
        new_trade_base = 0.0
        new_trade_quote = 0.0
        new_order_fill_base = 0.0
        new_order_fill_quote = 0.0
        latest_fill_at = task.last_fill_at or 0.0
        for trade in trades:
            order_id = _trade_order_id(trade)
            if order_id not in tracked:
                continue
            amount = _float_value(trade.get("amount"))
            cost = _trade_cost(trade)
            row = trade_fills.setdefault(order_id, {"amount": 0.0, "cost": 0.0})
            row["amount"] += amount
            row["cost"] += cost
            trade_id = str(trade.get("id") or "")
            if trade_id:
                if trade_id not in previous_known_trade_ids:
                    new_trade_base += amount
                    new_trade_quote += cost
                known_trade_ids.add(trade_id)
                trade_timestamp = _float_value(trade.get("timestamp"))
                if trade_timestamp > 0:
                    latest_fill_at = max(latest_fill_at, trade_timestamp / 1000)
                else:
                    latest_fill_at = max(latest_fill_at, time.time())

        order_fills: dict[str, dict[str, float]] = {}
        for order in [*open_orders, *closed_orders]:
            order_id = _order_id(order)
            if order_id not in tracked:
                continue
            amount, cost = _order_fill_amounts(order)
            existing = order_fills.get(order_id, {"amount": 0.0, "cost": 0.0})
            order_fills[order_id] = {
                "amount": max(existing["amount"], amount),
                "cost": max(existing["cost"], cost),
            }
            if _is_final_filled_order(order):
                fill_timestamp = _order_fill_timestamp(order)
                if fill_timestamp is not None:
                    latest_fill_at = max(latest_fill_at, fill_timestamp)
                if order_id not in previous_known_filled_order_ids:
                    if not bootstrap_order_fills:
                        new_order_fill_base += amount
                        new_order_fill_quote += cost
                    known_filled_order_ids.add(order_id)

        filled_base = 0.0
        filled_quote = 0.0
        for order_id in tracked:
            trade_fill = trade_fills.get(order_id, {"amount": 0.0, "cost": 0.0})
            order_fill = order_fills.get(order_id, {"amount": 0.0, "cost": 0.0})
            filled_base += max(trade_fill["amount"], order_fill["amount"])
            filled_quote += max(trade_fill["cost"], order_fill["cost"])

        next_filled_base = max(
            task.filled_base + new_trade_base + new_order_fill_base,
            filled_base,
        )
        next_filled_quote = max(
            task.filled_quote + new_trade_quote + new_order_fill_quote,
            filled_quote,
        )
        task.filled_base = (
            min(task.exec_cfg.total_base, next_filled_base)
            if task.exec_cfg.total_base > 0
            else next_filled_base
        )
        task.filled_quote = (
            min(task.exec_cfg.total_quote, next_filled_quote)
            if task.exec_cfg.total_quote > 0
            else next_filled_quote
        )
        task.open_order_ids = sorted(open_ids)
        task.known_trade_ids = sorted(known_trade_ids)
        task.known_filled_order_ids = sorted(known_filled_order_ids)
        task.last_fill_at = latest_fill_at or task.last_fill_at

    def _next_check_time(self, task: AutoBuySellTask) -> float:
        cfg = task.exec_cfg
        interval = max(1.0, cfg.interval_seconds)
        if task.open_order_ids and cfg.order_ttl_seconds > 0:
            return time.time() + min(interval, max(1.0, cfg.order_ttl_seconds))
        return time.time() + interval

    def _must_wait_for_next_slice(
        self,
        task: AutoBuySellTask,
        cfg: SlowExecutionConfig,
        now: float,
    ) -> bool:
        if not task.order_created_at:
            return False
        interval = max(1.0, cfg.interval_seconds)
        last_order_at = max(float(value) for value in task.order_created_at.values())
        next_slice_at = last_order_at + interval
        if next_slice_at <= now:
            return False
        task.status = "waiting_for_interval"
        task.last_status = "waiting_for_interval"
        task.last_error = None
        task.next_run_at = next_slice_at
        return True


def _task_status_from_cycle_status(status: str) -> str:
    if status in TERMINAL_TASK_STATUSES:
        return status
    if status == "waiting_for_start_price":
        return "waiting_for_start_price"
    if status == "placed":
        return "waiting_for_fill"
    if status == "blocked_by_risk":
        return "blocked_by_risk"
    if status == "planned":
        return "running"
    return status or "running"


def _task_is_complete(task: AutoBuySellTask, cfg: SlowExecutionConfig) -> bool:
    if cfg.total_base > 0 and task.filled_base >= cfg.total_base:
        return True
    if cfg.total_quote > 0 and task.filled_quote >= cfg.total_quote:
        return True
    return False
