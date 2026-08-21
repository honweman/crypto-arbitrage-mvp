from __future__ import annotations

import asyncio
from typing import Any

from ..coordination import (
    market_maker_resources_coordination_status,
)
from ..state import MonitorState

from ...auto_buy_sell_task import (
    TERMINAL_TASK_STATUSES,
    AutoBuySellTaskService,
)
from ...config import BotConfig
from ...exchanges import ExchangeManager
from ...user_workspace import UserWorkspaceStore
from ...workspace_runtime import (
    build_workspace_runtime_accounts,
    isolated_workspace_runtime_config,
)


async def auto_buy_sell_task_loop(
    cfg: BotConfig,
    state: MonitorState,
    tasks: AutoBuySellTaskService,
    workspace_store: UserWorkspaceStore | None = None,
) -> None:
    manager = ExchangeManager()
    coordination_owners: dict[str, str] = {}
    try:
        await state.set_auto_buy_sell_tasks(await tasks.snapshot())
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            before = await tasks.snapshot()
            task_rows = {
                str(task.get("id") or ""): task
                for task in before.get("tasks", [])
                if isinstance(task, dict) and task.get("id")
            }
            configs_by_owner: dict[str, BotConfig] = {}
            if workspace_store is not None:
                owners = {
                    str(task.get("owner_email") or "").strip().lower()
                    for task in task_rows.values()
                    if str(task.get("owner_email") or "").strip()
                }
                for owner_email in owners:
                    workspace = build_workspace_runtime_accounts(
                        workspace_store,
                        owner_emails=[owner_email],
                        include_unbound_market_types=True,
                    )
                    configs_by_owner[owner_email] = isolated_workspace_runtime_config(
                        runtime_cfg,
                        workspace,
                        risk_profile=workspace_store.risk_profile(owner_email),
                    )
            if not program_running or strategy_pauses.get("slow_execution", False):
                desired_task_ids: set[str] = set()
            else:
                desired_task_ids = {
                    task_id
                    for task_id, task in task_rows.items()
                    if _auto_buy_sell_coordination_required(
                        task,
                        already_coordinating=task_id in coordination_owners,
                    )
                }

            for task_id in set(coordination_owners) - desired_task_ids:
                await state.release_coordination_hold(coordination_owners.pop(task_id))

            ready_task_ids: set[str] = set()
            coordination_statuses: dict[str, dict[str, Any]] = {}
            market_maker_runtime = await state.market_maker_runtime()
            for task_id in sorted(desired_task_ids):
                task = task_rows[task_id]
                task_cfg = task.get("config") or {}
                owner = f"auto_buy_sell:{task_id}"
                resource = _auto_buy_sell_coordination_resource(task_cfg)
                coordination_owners[task_id] = owner
                await state.acquire_coordination_hold(
                    owner,
                    [resource],
                    reason=(
                        f"Auto Buy/Sell {task_id} temporarily withdrew the "
                        f"conflicting MM {resource[2]} side"
                    ),
                    ttl_seconds=5.0,
                )
                status = market_maker_resources_coordination_status(
                    runtime_cfg,
                    market_maker_runtime,
                    resources=[resource],
                    owner=owner,
                )
                coordination_statuses[task_id] = status
                if status.get("ready"):
                    ready_task_ids.add(task_id)
            payload = await tasks.run_due_tasks(
                runtime_cfg,
                manager,
                strategy_paused=strategy_pauses.get("slow_execution", False),
                market_maker_paused=strategy_pauses.get("market_maker", False),
                coordinated_market_maker_task_ids=ready_task_ids,
                program_running=program_running,
                configs_by_owner=configs_by_owner,
            )
            for task in payload.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("id") or "")
                if task_id in coordination_statuses:
                    task["market_maker_coordination"] = coordination_statuses[task_id]
            payload["market_maker_coordination"] = {
                "active_task_ids": sorted(coordination_owners),
                "ready_task_ids": sorted(ready_task_ids),
                "tasks": coordination_statuses,
            }
            await state.set_auto_buy_sell_tasks(payload)
            await asyncio.sleep(1.0)
    finally:
        for owner in coordination_owners.values():
            await state.release_coordination_hold(owner)
        await manager.close()


def _auto_buy_sell_coordination_resource(
    task_cfg: dict[str, Any],
) -> tuple[str, str, str]:
    task_side = str(task_cfg.get("side") or "").lower()
    blocked_side = "sell" if task_side == "buy" else "buy"
    return (
        str(task_cfg.get("exchange") or ""),
        str(task_cfg.get("symbol") or ""),
        blocked_side,
    )


def _auto_buy_sell_coordination_required(
    task: dict[str, Any],
    *,
    already_coordinating: bool,
) -> bool:
    task_cfg = task.get("config") if isinstance(task.get("config"), dict) else {}
    if not task_cfg.get("coordinate_market_maker"):
        return False
    if not task_cfg.get("block_conflicting_market_maker", True):
        return False
    status = str(task.get("status") or "")
    if status in TERMINAL_TASK_STATUSES or status == "paused":
        return False
    risk = task.get("last_risk") if isinstance(task.get("last_risk"), dict) else {}
    guard = (
        risk.get("self_trade_guard")
        if isinstance(risk.get("self_trade_guard"), dict)
        else {}
    )
    guard_blocked = bool(guard.get("blocked"))
    if guard_blocked:
        return True
    if not already_coordinating:
        return False
    if status in {"blocked_by_risk", "error", "waiting_for_start_price"}:
        return bool(task.get("open_order_count") or task.get("open_order_ids"))
    return True
