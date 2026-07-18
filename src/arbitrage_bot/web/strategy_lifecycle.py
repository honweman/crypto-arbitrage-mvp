from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..config import BotConfig
from ..web_config import market_maker_configs_for_runtime


DESIRED_STATES = {"running", "paused", "stopped"}
ACTUAL_STATES = {
    "starting",
    "running",
    "waiting",
    "pausing",
    "paused",
    "stopping",
    "stopped",
    "blocked",
    "error",
    "complete",
}

_TRANSITION_STATES = {"starting", "pausing", "stopping"}
_TERMINAL_AUTO_STATUSES = {
    "complete",
    "stopped",
    "stopped_by_price",
    "below_min_order_quote",
}


@dataclass(frozen=True)
class StrategyLifecycle:
    key: str
    strategy_id: str
    instance_id: str
    label: str
    account: str
    symbol: str
    mode: str
    desired_state: str
    actual_state: str
    convergence_state: str
    converged: bool
    raw_status: str
    reason: str | None
    allowed_actions: list[str]
    updated_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, list):
        return None
    for item in value:
        text = _text(item)
        if text:
            return text
    return None


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _reason_from_payload(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for field in (
        "status_reason",
        "last_error",
        "open_order_sync_error",
        "halt_reason",
        "reason",
        "error",
    ):
        text = _text(payload.get(field))
        if text:
            return text
    for field in ("errors", "warnings", "reasons"):
        text = _first_text(payload.get(field))
        if text:
            return text
    for field in (
        "risk",
        "last_risk",
        "execution",
        "last_execution",
        "order_validation",
        "balance_guard",
        "conflict_guard",
        "coordination",
        "safety",
        "last_payload",
    ):
        nested = _nested_mapping(payload.get(field))
        if not nested:
            continue
        text = _reason_from_payload(nested)
        if text:
            return text
    return None


def _updated_at(payload: Mapping[str, Any] | None) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    for field in ("updated_at", "observed_at", "last_cycle_at", "created_at"):
        value = payload.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _effective_desired_state(
    *,
    enabled: bool,
    program_running: bool,
    strategy_paused: bool,
) -> str:
    if not enabled:
        return "stopped"
    if not program_running or strategy_paused:
        return "paused"
    return "running"


def _convergence(desired: str, actual: str) -> tuple[str, bool]:
    if desired not in DESIRED_STATES:
        raise ValueError(f"unknown desired strategy state: {desired}")
    if actual not in ACTUAL_STATES:
        raise ValueError(f"unknown actual strategy state: {actual}")
    if actual == "error":
        return "error", False
    if actual == "blocked":
        return "blocked", False
    if actual in _TRANSITION_STATES:
        return "transitioning", False
    if desired == "running" and actual in {"running", "waiting"}:
        return "in_sync", True
    if desired == "paused" and actual == "paused":
        return "in_sync", True
    if desired == "stopped" and actual in {"stopped", "complete"}:
        return "in_sync", True
    return "transitioning", False


def _standard_actions(
    *,
    desired: str,
    actual: str,
    supports_start: bool = True,
    supports_pause: bool = True,
    terminal_action: str | None = None,
) -> list[str]:
    if actual in {"complete", "stopped"}:
        actions = ["start"] if supports_start else []
        if terminal_action:
            actions.insert(0, terminal_action)
        return actions
    if actual in {"stopping", "pausing"}:
        return []
    if desired == "paused" or actual == "paused":
        actions = ["resume"] if supports_pause else []
        actions.append("stop")
        return actions
    actions = ["pause"] if supports_pause else []
    actions.append("stop")
    return actions


def _row(
    *,
    key: str,
    strategy_id: str,
    instance_id: str,
    label: str,
    account: str,
    symbol: str,
    mode: str,
    desired: str,
    actual: str,
    raw_status: str,
    reason: str | None,
    actions: list[str],
    updated_at: float | None,
) -> StrategyLifecycle:
    convergence_state, converged = _convergence(desired, actual)
    if convergence_state not in {"blocked", "error"}:
        reason = None
    return StrategyLifecycle(
        key=key,
        strategy_id=strategy_id,
        instance_id=instance_id,
        label=label,
        account=account,
        symbol=symbol,
        mode=mode,
        desired_state=desired,
        actual_state=actual,
        convergence_state=convergence_state,
        converged=converged,
        raw_status=raw_status,
        reason=reason,
        allowed_actions=actions,
        updated_at=updated_at,
    )


def _market_maker_actual(raw_status: str, desired: str) -> str:
    status = raw_status.lower()
    if status in {"disabled", "dry_run", "removed"}:
        return "stopped"
    if status in {"paused", "program_paused"}:
        return "paused"
    if status in {"blocked_by_risk"}:
        return "blocked"
    if status == "reconciliation_required":
        return "blocked"
    if status in {
        "error",
        "execution_error",
        "open_order_sync_error",
        "coordination_cancel_retry",
    }:
        return "error"
    if status == "cancel_retry":
        return "stopping" if desired in {"stopped", "paused"} else "error"
    if status == "coordinating":
        return "waiting"
    if status in {"placed", "unchanged"}:
        if desired == "stopped":
            return "stopping"
        if desired == "paused":
            return "pausing"
        return "running"
    if status in {"starting", "planned", "live", ""}:
        return "starting" if desired == "running" else "stopped"
    return "starting" if desired == "running" else "stopped"


def _market_maker_rows(
    cfg: BotConfig,
    *,
    program_running: bool,
    strategy_paused: bool,
    payload: Mapping[str, Any],
) -> list[StrategyLifecycle]:
    configs = market_maker_configs_for_runtime(cfg)
    aggregate_runtime = _nested_mapping(payload.get("runtime"))
    runtime_by_id = {
        _text(item.get("id")): item
        for item in aggregate_runtime.get("instances", [])
        if isinstance(item, Mapping) and _text(item.get("id"))
    }
    instance_payloads = {
        _text(_nested_mapping(item.get("config")).get("id") or item.get("id")): item
        for item in payload.get("instances", [])
        if isinstance(item, Mapping)
        and _text(_nested_mapping(item.get("config")).get("id") or item.get("id"))
    }
    rows: list[StrategyLifecycle] = []
    for maker_cfg in configs:
        instance_id = maker_cfg.id
        instance_payload = _nested_mapping(instance_payloads.get(instance_id))
        runtime = _nested_mapping(
            runtime_by_id.get(instance_id) or instance_payload.get("runtime")
        )
        fallback_payload = instance_payload or (payload if len(configs) == 1 else {})
        raw_status = _text(
            runtime.get("status")
            or fallback_payload.get("status")
            or (
                "starting"
                if maker_cfg.enabled and maker_cfg.live_enabled
                else "disabled"
            )
        )
        desired = _effective_desired_state(
            enabled=maker_cfg.enabled and maker_cfg.live_enabled,
            program_running=program_running,
            strategy_paused=strategy_paused,
        )
        actual = _market_maker_actual(raw_status, desired)
        source = runtime or fallback_payload
        mode = _text(source.get("mode")) or (
            "live" if maker_cfg.enabled and maker_cfg.live_enabled else "dry_run"
        )
        rows.append(
            _row(
                key=f"market_maker:{instance_id}",
                strategy_id="market_maker",
                instance_id=instance_id,
                label="Market Maker",
                account=maker_cfg.exchange,
                symbol=maker_cfg.symbol,
                mode=mode,
                desired=desired,
                actual=actual,
                raw_status=raw_status,
                reason=_reason_from_payload(source)
                or _reason_from_payload(fallback_payload),
                actions=_standard_actions(desired=desired, actual=actual),
                updated_at=_updated_at(source) or _updated_at(fallback_payload),
            )
        )
    return rows


def _auto_actual(task: Mapping[str, Any], desired: str) -> str:
    status = _text(task.get("status")).lower()
    last_status = _text(task.get("last_status")).lower()
    if last_status in {"program_paused", "strategy_paused"} and desired == "paused":
        return "paused"
    if status == "paused":
        return "paused"
    if status == "complete":
        return "complete"
    if status in {"stopped", "stopped_by_price", "below_min_order_quote"}:
        return "stopped"
    if status == "stop_cancel_pending":
        return "stopping"
    if status == "blocked_by_risk":
        return "blocked"
    if status == "error":
        return "error"
    if status in {
        "waiting_for_start_price",
        "waiting_for_fill",
        "waiting_for_interval",
    }:
        return "waiting"
    if status in {"running", "placing", "placed"}:
        if desired == "paused":
            return "pausing"
        if desired == "stopped":
            return "stopping"
        return "running"
    return "starting" if desired == "running" else "stopped"


def _auto_rows(
    cfg: BotConfig,
    *,
    program_running: bool,
    strategy_paused: bool,
    tasks_payload: Mapping[str, Any],
) -> list[StrategyLifecycle]:
    tasks = [
        item for item in tasks_payload.get("tasks", []) if isinstance(item, Mapping)
    ]
    if not tasks:
        exec_cfg = cfg.slow_execution
        return [
            _row(
                key="slow_execution:default",
                strategy_id="slow_execution",
                instance_id="default",
                label="Auto Buy/Sell",
                account=exec_cfg.exchange,
                symbol=exec_cfg.symbol,
                mode="live",
                desired="stopped",
                actual="stopped",
                raw_status="no_task",
                reason=None,
                actions=["start"],
                updated_at=_updated_at(tasks_payload),
            )
        ]

    rows: list[StrategyLifecycle] = []
    for task in tasks:
        task_id = _text(task.get("id")) or "unknown"
        task_cfg = _nested_mapping(task.get("config"))
        status = _text(task.get("status")).lower()
        if status in _TERMINAL_AUTO_STATUSES or status == "stop_cancel_pending":
            desired = "stopped"
        elif status == "paused" or not program_running or strategy_paused:
            desired = "paused"
        else:
            desired = "running"
        actual = _auto_actual(task, desired)
        terminal_action = "cleanup" if status in _TERMINAL_AUTO_STATUSES else None
        rows.append(
            _row(
                key=f"slow_execution:{task_id}",
                strategy_id="slow_execution",
                instance_id=task_id,
                label="Auto Buy/Sell",
                account=_text(task_cfg.get("exchange")),
                symbol=_text(task_cfg.get("symbol")),
                mode="live",
                desired=desired,
                actual=actual,
                raw_status=status or "unknown",
                reason=_reason_from_payload(task),
                actions=_standard_actions(
                    desired=desired,
                    actual=actual,
                    supports_start=False,
                    terminal_action=terminal_action,
                ),
                updated_at=_updated_at(task),
            )
        )
    return rows


def _rebalance_actual(raw_status: str, desired: str) -> str:
    status = raw_status.lower()
    if status in {"disabled", "dry_run"}:
        return "stopped"
    if status in {"paused", "program_paused"}:
        return "paused"
    if status == "complete":
        return "complete"
    if status in {"starting", ""}:
        return "starting" if desired == "running" else "stopped"
    if status in {
        "planned",
        "waiting_for_cost",
        "waiting_for_market_data",
        "waiting_for_coordination",
        "no_fill",
    }:
        return "waiting"
    if status in {"progress", "placed", "execution_repaired"}:
        return "running"
    if status in {
        "blocked_by_plan",
        "blocked_by_risk",
        "blocked_by_validation",
        "blocked_by_conflict",
        "blocked_by_balance",
        "halted",
        "held_for_safety",
    }:
        return "blocked"
    if status in {"error", "execution_error", "hedge_required"}:
        return "error"
    return "waiting" if desired == "running" else "stopped"


def _rebalance_row(
    cfg: BotConfig,
    *,
    program_running: bool,
    strategy_paused: bool,
    payload: Mapping[str, Any],
) -> StrategyLifecycle:
    rebalance = cfg.cross_exchange_rebalance
    runtime = _nested_mapping(payload.get("runtime"))
    raw_status = _text(runtime.get("status") or payload.get("status") or "disabled")
    completed = raw_status == "complete"
    desired = _effective_desired_state(
        enabled=rebalance.enabled and rebalance.live_enabled and not completed,
        program_running=program_running,
        strategy_paused=strategy_paused,
    )
    actual = _rebalance_actual(raw_status, desired)
    source = runtime or payload
    actions = (
        ["reset"]
        if actual == "complete"
        else _standard_actions(desired=desired, actual=actual)
    )
    return _row(
        key="cross_exchange_rebalance:default",
        strategy_id="cross_exchange_rebalance",
        instance_id="default",
        label="Cross-Exchange Rebalance",
        account=rebalance.buy_exchange,
        symbol=rebalance.buy_symbol,
        mode=_text(source.get("mode"))
        or ("live" if rebalance.live_enabled else "dry_run"),
        desired=desired,
        actual=actual,
        raw_status=raw_status,
        reason=_reason_from_payload(source) or _reason_from_payload(payload),
        actions=actions,
        updated_at=_updated_at(source) or _updated_at(payload),
    )


def _spot_configured(cfg: BotConfig) -> bool:
    exchanges_by_asset: dict[str, set[str]] = {}
    for market in cfg.spot_markets:
        asset = _text(market.asset).upper()
        exchange = _text(market.exchange)
        if asset and exchange:
            exchanges_by_asset.setdefault(asset, set()).add(exchange)
    return any(len(exchanges) >= 2 for exchanges in exchanges_by_asset.values())


def _spot_actual(raw_status: str, desired: str) -> str:
    status = raw_status.lower()
    if status in {"disabled", "out_of_scope"}:
        return "stopped"
    if status in {"paused", "program_paused"}:
        return "paused"
    if status in {"starting", ""}:
        return "starting" if desired == "running" else "stopped"
    if status in {"no_opportunity", "live_disabled", "cooldown", "planned"}:
        return "waiting"
    if status in {"placed", "execution_repaired"}:
        return "running"
    if status in {
        "blocked_by_plan",
        "blocked_by_risk",
        "blocked_by_slippage",
        "blocked_by_validation",
        "blocked_by_balance",
    }:
        return "blocked"
    if status in {"error", "execution_error", "hedge_required"}:
        return "error"
    return "waiting" if desired == "running" else "stopped"


def _spot_row(
    cfg: BotConfig,
    *,
    program_running: bool,
    strategy_paused: bool,
    payload: Mapping[str, Any],
) -> StrategyLifecycle:
    configured = _spot_configured(cfg)
    desired = _effective_desired_state(
        enabled=configured,
        program_running=program_running,
        strategy_paused=strategy_paused,
    )
    raw_status = _text(
        payload.get("status") or ("starting" if configured else "disabled")
    )
    actual = _spot_actual(raw_status, desired)
    assets = sorted(
        {market.asset.upper() for market in cfg.spot_markets if market.asset}
    )
    actions = (
        ["resume"]
        if actual == "paused"
        else ["pause"]
        if desired == "running" and actual not in _TRANSITION_STATES
        else []
    )
    return _row(
        key="spot_spread:default",
        strategy_id="spot_spread",
        instance_id="default",
        label="Spot Arbitrage",
        account="",
        symbol=",".join(assets),
        mode=_text(payload.get("mode")) or "dry_run",
        desired=desired,
        actual=actual,
        raw_status=raw_status,
        reason=_reason_from_payload(payload),
        actions=actions,
        updated_at=_updated_at(payload),
    )


def build_strategy_lifecycle_payload(
    cfg: BotConfig,
    *,
    program: Mapping[str, Any] | None = None,
    strategy_paused: Mapping[str, bool] | None = None,
    market_maker: Mapping[str, Any] | None = None,
    auto_buy_sell_tasks: Mapping[str, Any] | None = None,
    cross_exchange_rebalance: Mapping[str, Any] | None = None,
    spot_arbitrage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    program = program or {}
    pauses = strategy_paused or {}
    program_running = bool(program.get("running", True))
    rows = [
        *_market_maker_rows(
            cfg,
            program_running=program_running,
            strategy_paused=bool(pauses.get("market_maker", False)),
            payload=market_maker or {},
        ),
        *_auto_rows(
            cfg,
            program_running=program_running,
            strategy_paused=bool(pauses.get("slow_execution", False)),
            tasks_payload=auto_buy_sell_tasks or {},
        ),
        _rebalance_row(
            cfg,
            program_running=program_running,
            strategy_paused=bool(pauses.get("cross_exchange_rebalance", False)),
            payload=cross_exchange_rebalance or {},
        ),
        _spot_row(
            cfg,
            program_running=program_running,
            strategy_paused=bool(pauses.get("spot_spread", False)),
            payload=spot_arbitrage or {},
        ),
    ]
    row_payloads = [row.to_dict() for row in rows]
    error_count = sum(row.convergence_state == "error" for row in rows)
    blocked_count = sum(row.convergence_state == "blocked" for row in rows)
    transitioning_count = sum(row.convergence_state == "transitioning" for row in rows)
    status = (
        "error"
        if error_count
        else "blocked"
        if blocked_count
        else "transitioning"
        if transitioning_count
        else "ok"
    )
    updated_values = [row.updated_at for row in rows if row.updated_at is not None]
    return {
        "version": 1,
        "status": status,
        "instances": row_payloads,
        "summary": {
            "instance_count": len(rows),
            "converged_count": sum(row.converged for row in rows),
            "attention_count": len(rows) - sum(row.converged for row in rows),
            "blocked_count": blocked_count,
            "error_count": error_count,
            "transitioning_count": transitioning_count,
        },
        "updated_at": max(updated_values, default=None),
    }


__all__ = [
    "ACTUAL_STATES",
    "DESIRED_STATES",
    "StrategyLifecycle",
    "build_strategy_lifecycle_payload",
]
