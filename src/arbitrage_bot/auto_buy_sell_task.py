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
from .slow_executor import cancel_order_ids, run_cycle
from .trade_log import write_trade_event


RUNNING_TASK_STATUSES = {
    "running",
    "waiting_for_start_price",
    "waiting_for_fill",
    "waiting_for_interval",
    "blocked_by_risk",
    "error",
}
TERMINAL_TASK_STATUSES = {"complete", "stopped_by_price", "below_min_order_quote"}


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
    if cfg.total_base <= 0 and cfg.total_quote <= 0:
        raise ValueError("total_base or total_quote must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if cfg.order_ttl_seconds < 0:
        raise ValueError("order_ttl_seconds must be non-negative")
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


def _trade_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("order") or raw.get("order_id") or "")


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
    last_status: str | None = None
    last_plan: dict[str, Any] | None = None
    last_risk: dict[str, Any] | None = None
    last_execution: dict[str, Any] | None = None
    placed_order_ids: list[str] = field(default_factory=list)
    open_order_ids: list[str] = field(default_factory=list)
    known_trade_ids: list[str] = field(default_factory=list)
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
        base_target_enabled = cfg.total_base > 0
        quote_target_enabled = cfg.total_quote > 0
        remaining_base = (
            max(0.0, cfg.total_base - self.filled_base)
            if base_target_enabled
            else 0.0
        )
        remaining_quote = (
            max(0.0, cfg.total_quote - self.filled_quote)
            if quote_target_enabled
            else 0.0
        )
        progress_mode = "quote" if quote_target_enabled else "base"
        progress_value = self.filled_quote if quote_target_enabled else self.filled_base
        progress_total = cfg.total_quote if quote_target_enabled else cfg.total_base
        progress_pct = (
            min(100.0, max(0.0, progress_value / progress_total * 100))
            if progress_total > 0
            else 0.0
        )
        return {
            **asdict(self),
            "remaining_base": remaining_base,
            "remaining_quote": remaining_quote,
            "progress_mode": progress_mode,
            "progress_pct": progress_pct,
            "progress_label": "Bought" if cfg.side == "buy" else "Sold",
            "open_order_count": len(self.open_order_ids),
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
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "tasks": [task.to_dict() for task in tasks],
        }
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)


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
            self._tasks.append(task)
            self.store.save(self._tasks)
            return task.to_dict()

    async def set_paused(self, task_id: str, paused: bool) -> dict[str, Any]:
        async with self._lock:
            task = self._get_task_unlocked(task_id)
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

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot_unlocked()

    async def run_due_tasks(
        self,
        cfg: BotConfig,
        manager: ExchangeManager,
        *,
        strategy_paused: bool = False,
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
            await self._run_task_cycle(task, cfg, manager)
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
    ) -> None:
        task_cfg = task.exec_cfg
        runtime_cfg = replace(cfg, slow_execution=task_cfg)
        now = time.time()
        task.cycle_count += 1
        task.last_cycle_at = now
        task.updated_at = now
        try:
            await self._refresh_task_activity(task, runtime_cfg, manager)
            if _task_is_complete(task, task_cfg):
                task.status = "complete"
                task.finished_at = time.time()
                task.last_status = "complete"
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
            task.next_run_at = self._next_check_time(task)
        except Exception as exc:  # noqa: BLE001
            task.status = "error"
            task.last_status = "error"
            task.last_error = f"{exc.__class__.__name__}: {exc}"
            task.next_run_at = time.time() + max(1.0, task_cfg.interval_seconds)

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
        closed_orders = await manager.fetch_closed_orders(exchange, symbol=symbol, limit=100)
        trades = await manager.fetch_my_trades(exchange, symbol=symbol, limit=100)
        open_ids = {
            _order_id(order)
            for order in open_orders
            if _order_id(order) in tracked
        }
        trade_fills: dict[str, dict[str, float]] = {}
        previous_known_trade_ids: set[str] = set(task.known_trade_ids)
        known_trade_ids: set[str] = set(previous_known_trade_ids)
        new_trade_base = 0.0
        new_trade_quote = 0.0
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
                task.last_fill_at = _float_value(trade.get("timestamp")) / 1000 or time.time()

        order_fills: dict[str, dict[str, float]] = {}
        for order in [*open_orders, *closed_orders]:
            order_id = _order_id(order)
            if order_id not in tracked:
                continue
            order_fills[order_id] = {
                "amount": _float_value(order.get("filled")),
                "cost": _float_value(order.get("cost")),
            }

        filled_base = 0.0
        filled_quote = 0.0
        for order_id in tracked:
            trade_fill = trade_fills.get(order_id, {"amount": 0.0, "cost": 0.0})
            order_fill = order_fills.get(order_id, {"amount": 0.0, "cost": 0.0})
            filled_base += max(trade_fill["amount"], order_fill["amount"])
            filled_quote += max(trade_fill["cost"], order_fill["cost"])

        next_filled_base = max(task.filled_base + new_trade_base, filled_base)
        next_filled_quote = max(task.filled_quote + new_trade_quote, filled_quote)
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
