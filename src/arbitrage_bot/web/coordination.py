from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from ..config import BotConfig
from ..web_config import market_maker_configs_for_runtime


class CoordinationState(Protocol):
    async def market_maker_runtime(self) -> dict[str, Any]: ...


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
    resources = set(rebalance_coordination_resources(cfg))
    affected = [
        maker_cfg
        for maker_cfg in market_maker_configs_for_runtime(cfg)
        if (maker_cfg.exchange, maker_cfg.symbol) in resources
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
        sync_error = instance.get("open_order_sync_error")
        rows.append(
            {
                "id": maker_cfg.id,
                "exchange": maker_cfg.exchange,
                "symbol": maker_cfg.symbol,
                "status": instance.get("status"),
                "acknowledged": acknowledged,
                "open_order_count": open_order_count,
                "sync_error": sync_error,
            }
        )
        if not acknowledged:
            reasons.append(f"waiting for MM instance {maker_cfg.id} acknowledgement")
        if sync_error:
            reasons.append(f"MM instance {maker_cfg.id} sync error: {sync_error}")
        if open_order_count:
            reasons.append(
                f"MM instance {maker_cfg.id} still has "
                f"{open_order_count} open order(s)"
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
    "market_maker_coordination_status",
    "rebalance_coordination_hold_required",
    "rebalance_coordination_resources",
    "wait_for_market_maker_coordination",
]
