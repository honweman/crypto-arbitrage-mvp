from __future__ import annotations

from typing import Any


MARKET_MAKER_ALERT_STATUSES = {
    "blocked_by_risk",
    "coordination_cancel_retry",
    "cancel_retry",
    "execution_error",
    "open_order_sync_error",
    "reconciliation_required",
    "error",
}


def market_maker_problem_warnings(runtime: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for instance in runtime.get("instances", []) or []:
        if not isinstance(instance, dict):
            continue
        status = str(instance.get("status") or "")
        if status not in MARKET_MAKER_ALERT_STATUSES:
            continue
        name = str(
            instance.get("display_name")
            or instance.get("id")
            or "market maker"
        )
        reason = str(
            instance.get("status_reason")
            or instance.get("reason")
            or instance.get("last_error")
            or instance.get("open_order_sync_error")
            or status
        )
        warnings.append(f"Market maker {name}: {status} ({reason})")
    return warnings
