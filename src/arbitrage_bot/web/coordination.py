from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol, Union

from ..config import BotConfig
from ..web_config import market_maker_configs_for_runtime


class CoordinationState(Protocol):
    async def market_maker_runtime(self) -> dict[str, Any]: ...


CoordinationResource = Union[tuple[str, str], tuple[str, str, str]]


def coordination_resource_parts(
    resource: CoordinationResource | dict[str, Any],
) -> tuple[str, str, str]:
    if isinstance(resource, dict):
        return (
            str(resource.get("exchange") or "").strip(),
            str(resource.get("symbol") or "").strip(),
            str(resource.get("side") or "").strip().lower(),
        )
    if len(resource) == 2:
        return str(resource[0]).strip(), str(resource[1]).strip(), ""
    return (
        str(resource[0]).strip(),
        str(resource[1]).strip(),
        str(resource[2]).strip().lower(),
    )


def coordination_blocked_sides(
    hold: dict[str, Any] | None,
    exchange: str,
    symbol: str,
) -> set[str]:
    if not isinstance(hold, dict):
        return set()
    sides: set[str] = set()
    for resource in hold.get("resources", []):
        resource_exchange, resource_symbol, side = coordination_resource_parts(
            resource
        )
        if resource_exchange != exchange or resource_symbol != symbol:
            continue
        if side in {"buy", "sell"}:
            sides.add(side)
        else:
            return {"buy", "sell"}
    return sides


def rebalance_coordination_resources(cfg: BotConfig) -> list[tuple[str, str]]:
    rebalance = cfg.cross_exchange_rebalance
    return [
        (rebalance.buy_exchange, rebalance.buy_symbol),
        (rebalance.sell_exchange, rebalance.sell_symbol),
    ]


def market_maker_coordination_status(
    cfg: BotConfig,
    runtime: dict[str, Any],
    *,
    owner: str,
) -> dict[str, Any]:
    return market_maker_resources_coordination_status(
        cfg,
        runtime,
        resources=rebalance_coordination_resources(cfg),
        owner=owner,
    )


def market_maker_resources_coordination_status(
    cfg: BotConfig,
    runtime: dict[str, Any],
    *,
    resources: list[CoordinationResource],
    owner: str,
) -> dict[str, Any]:
    normalized_resources = [coordination_resource_parts(item) for item in resources]
    resource_keys = {
        (exchange, symbol)
        for exchange, symbol, _ in normalized_resources
        if exchange and symbol
    }
    affected = [
        maker_cfg
        for maker_cfg in market_maker_configs_for_runtime(cfg)
        if (maker_cfg.exchange, maker_cfg.symbol) in resource_keys
    ]
    instances = {
        str(instance.get("id") or ""): instance
        for instance in runtime.get("instances", [])
        if isinstance(instance, dict)
    }
    rows = []
    reasons = []
    for maker_cfg in affected:
        instance = instances.get(maker_cfg.id)
        if instance is None:
            rows.append(
                {
                    "id": maker_cfg.id,
                    "exchange": maker_cfg.exchange,
                    "symbol": maker_cfg.symbol,
                    "status": "starting",
                    "acknowledged": False,
                    "open_order_count": None,
                    "sync_error": None,
                }
            )
            reasons.append(f"waiting for MM instance {maker_cfg.id} to start")
            continue
        hold = (
            instance.get("coordination_hold")
            if isinstance(instance.get("coordination_hold"), dict)
            else {}
        )
        acknowledged = hold.get("owner") == owner
        open_order_count = int(instance.get("open_order_count") or 0)
        blocked_sides = coordination_blocked_sides(
            hold if acknowledged else None,
            maker_cfg.exchange,
            maker_cfg.symbol,
        )
        conflicting_open_order_count = int(
            instance.get("coordination_conflicting_open_order_count")
            if acknowledged
            and instance.get("coordination_conflicting_open_order_count") is not None
            else open_order_count
        )
        sync_error = instance.get("open_order_sync_error")
        rows.append(
            {
                "id": maker_cfg.id,
                "exchange": maker_cfg.exchange,
                "symbol": maker_cfg.symbol,
                "status": instance.get("status"),
                "acknowledged": acknowledged,
                "open_order_count": open_order_count,
                "conflicting_open_order_count": conflicting_open_order_count,
                "blocked_sides": sorted(blocked_sides),
                "sync_error": sync_error,
            }
        )
        if not acknowledged:
            reasons.append(f"waiting for MM instance {maker_cfg.id} acknowledgement")
        if sync_error:
            reasons.append(f"MM instance {maker_cfg.id} sync error: {sync_error}")
        if conflicting_open_order_count:
            reasons.append(
                f"MM instance {maker_cfg.id} still has "
                f"{conflicting_open_order_count} open conflicting order(s)"
            )
    return {
        "ready": not reasons,
        "affected_instance_count": len(affected),
        "instances": rows,
        "reasons": reasons,
    }


async def wait_for_market_maker_coordination(
    state: CoordinationState,
    cfg: BotConfig,
    *,
    owner: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        status = market_maker_coordination_status(
            cfg,
            await state.market_maker_runtime(),
            owner=owner,
        )
        elapsed = time.monotonic() - started
        status["waited_seconds"] = elapsed
        status["timeout_seconds"] = timeout_seconds
        if status["ready"]:
            status["status"] = "ready"
            return status
        if elapsed >= timeout_seconds:
            status["status"] = "timeout"
            return status
        await asyncio.sleep(min(0.25, max(0.01, timeout_seconds - elapsed)))


def rebalance_coordination_hold_required(payload: dict[str, Any]) -> bool:
    if payload.get("halt_required"):
        return True
    if payload.get("status") in {
        "blocked_by_conflict",
        "hedge_required",
        "waiting_for_coordination",
    }:
        return True
    execution = (
        payload.get("execution")
        if isinstance(payload.get("execution"), dict)
        else {}
    )
    return bool(
        execution.get("manual_intervention_required")
        or execution.get("remaining_open_order_ids")
    )


__all__ = [
    "CoordinationResource",
    "coordination_blocked_sides",
    "coordination_resource_parts",
    "market_maker_coordination_status",
    "market_maker_resources_coordination_status",
    "rebalance_coordination_hold_required",
    "rebalance_coordination_resources",
    "wait_for_market_maker_coordination",
]
