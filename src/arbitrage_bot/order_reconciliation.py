from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any


RECONCILIATION_AUTO_STOP_WARMUP_SECONDS = 15.0
RECONCILIATION_AUTO_STOP_TYPES: frozenset[str] = frozenset()
RECONCILIATION_RECOVERABLE_TYPES = {
    "account_order_activity_error",
    "order_activity_error",
    "uncertain_order_intent",
}


def _activity_order_key(exchange: str, symbol: str, order_id: str) -> str:
    return f"{exchange}|{symbol}|{order_id}" if exchange and symbol and order_id else ""


def _activity_lookup_keys(row: dict[str, Any], order_field: str = "id") -> list[str]:
    order_id = str(row.get(order_field) or "")
    if not order_id:
        return []
    exchange = str(row.get("exchange") or "")
    symbol = str(row.get("symbol") or "")
    keys = []
    composite = _activity_order_key(exchange, symbol, order_id)
    if composite:
        keys.append(composite)
    keys.append(order_id)
    return keys


def _tracked_order_row(
    *,
    strategy: str,
    exchange: str,
    symbol: str,
    order_id: str,
    source_id: str = "",
    expected_open: bool = True,
) -> dict[str, Any] | None:
    order_id = str(order_id or "")
    if not order_id:
        return None
    return {
        "strategy": strategy,
        "exchange": str(exchange or ""),
        "symbol": str(symbol or ""),
        "order_id": order_id,
        "source_id": source_id,
        "expected_open": expected_open,
        "key": _activity_order_key(str(exchange or ""), str(symbol or ""), order_id),
    }


def _tracked_orders_from_local_state(
    *,
    market_maker_runtime: dict[str, Any] | None = None,
    auto_buy_sell_tasks: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    tracked: list[dict[str, Any]] = []
    runtime = market_maker_runtime or {}
    mm_instances = runtime.get("instances") if isinstance(runtime, dict) else None
    if isinstance(mm_instances, dict):
        instance_rows = list(mm_instances.values())
    elif isinstance(mm_instances, list):
        instance_rows = mm_instances
    else:
        instance_rows = []
    if not instance_rows:
        instance_rows = [runtime]
    for instance_runtime in instance_rows:
        if not isinstance(instance_runtime, dict):
            continue
        mm_exchange = str(instance_runtime.get("open_order_exchange") or "")
        mm_symbol = str(instance_runtime.get("open_order_symbol") or "")
        source_id = str(instance_runtime.get("id") or "market_maker_runtime")
        for order_id in instance_runtime.get("open_order_ids", []) or []:
            row = _tracked_order_row(
                strategy="market_maker",
                exchange=mm_exchange,
                symbol=mm_symbol,
                order_id=str(order_id),
                source_id=source_id,
                expected_open=True,
            )
            if row is not None:
                tracked.append(row)

    for task in (auto_buy_sell_tasks or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        config = task.get("config") if isinstance(task.get("config"), dict) else {}
        exchange = str(config.get("exchange") or "")
        symbol = str(config.get("symbol") or "")
        task_id = str(task.get("id") or "")
        open_order_ids = {
            str(order_id)
            for order_id in task.get("open_order_ids", []) or []
            if order_id
        }
        placed_order_ids = {
            str(order_id)
            for order_id in task.get("placed_order_ids", []) or []
            if order_id
        }
        for order_id in sorted(open_order_ids | placed_order_ids):
            row = _tracked_order_row(
                strategy="auto_buy_sell",
                exchange=exchange,
                symbol=symbol,
                order_id=order_id,
                source_id=task_id,
                expected_open=order_id in open_order_ids,
            )
            if row is not None:
                tracked.append(row)
    return tracked


def _lookup_activity_rows(
    rows: Iterable[dict[str, Any]],
    *,
    order_field: str = "id",
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key in _activity_lookup_keys(row, order_field=order_field):
            lookup.setdefault(key, row)
    return lookup


def _reconciliation_issue(
    *,
    level: str,
    issue_type: str,
    message: str,
    strategy: str = "",
    exchange: str = "",
    symbol: str = "",
    order_id: str = "",
    source_id: str = "",
) -> dict[str, Any]:
    return {
        "level": level,
        "type": issue_type,
        "strategy": strategy,
        "exchange": exchange,
        "symbol": symbol,
        "order_id": order_id,
        "source_id": source_id,
        "message": message,
    }


def _reconciliation_reasons_for_types(
    issues: Iterable[dict[str, Any]],
    issue_types: set[str] | frozenset[str],
) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        issue_type = str(issue.get("type") or "")
        if issue_type not in issue_types:
            continue
        exchange = str(issue.get("exchange") or "")
        symbol = str(issue.get("symbol") or "")
        order_id = str(issue.get("order_id") or "")
        message = str(issue.get("message") or issue_type)
        detail = " ".join(item for item in [exchange, symbol, order_id] if item)
        reason = f"{issue_type}: {detail or message}"
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)
    return reasons


def _reconciliation_auto_stop_reasons(
    issues: Iterable[dict[str, Any]],
) -> list[str]:
    return _reconciliation_reasons_for_types(issues, RECONCILIATION_AUTO_STOP_TYPES)


def _monitor_reconciliation_streak(
    *,
    current_count: int,
    previous_fingerprint: str,
    previous_observation: str,
    reconciliation_stop: bool,
    reasons: Iterable[str],
    observation: Any,
) -> tuple[int, str, str]:
    """Count distinct observations of one unchanged critical condition."""
    if not reconciliation_stop:
        return 0, "", ""

    fingerprint = "|".join(sorted({str(reason) for reason in reasons if reason}))
    fingerprint = fingerprint or "critical_reconciliation_issue"
    observation_key = str(observation) if observation is not None else ""
    if fingerprint != previous_fingerprint:
        return 1, fingerprint, observation_key
    if not observation_key or observation_key == previous_observation:
        return max(1, current_count), fingerprint, previous_observation
    return max(1, current_count + 1), fingerprint, observation_key


def _monitor_auto_stop_decision(
    *,
    auto_stop_enabled: bool,
    auto_stop_consecutive_errors: int,
    daily_loss_stop: bool,
    reconciliation_stop: bool,
    consecutive_problem_cycles: int,
) -> tuple[bool, str | None]:
    if not auto_stop_enabled:
        return False, None
    if daily_loss_stop:
        return True, "daily loss limit breached"
    if not reconciliation_stop:
        return False, None
    threshold = max(1, auto_stop_consecutive_errors)
    if consecutive_problem_cycles < threshold:
        return False, None
    return (
        True,
        "critical reconciliation issue after "
        f"{consecutive_problem_cycles} problem cycle(s)",
    )


def _monitor_reconciliation_warmup_active(
    *,
    process_uptime_seconds: float,
    program_age_seconds: float,
    warmup_seconds: float = RECONCILIATION_AUTO_STOP_WARMUP_SECONDS,
) -> bool:
    if warmup_seconds <= 0:
        return False
    if 0.0 <= process_uptime_seconds < warmup_seconds:
        return True
    return 0.0 <= program_age_seconds < warmup_seconds


def build_order_reconciliation_payload(
    order_activity: dict[str, Any],
    *,
    market_maker_runtime: dict[str, Any] | None = None,
    auto_buy_sell_tasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    open_orders = order_activity.get("open_orders", []) or []
    closed_orders = order_activity.get("closed_orders", []) or []
    recent_trades = order_activity.get("recent_trades", []) or []
    open_lookup = _lookup_activity_rows(open_orders)
    closed_lookup = _lookup_activity_rows(closed_orders)
    trade_lookup = _lookup_activity_rows(recent_trades, order_field="order_id")
    tracked_orders = _tracked_orders_from_local_state(
        market_maker_runtime=market_maker_runtime,
        auto_buy_sell_tasks=auto_buy_sell_tasks,
    )
    tracked_keys: set[str] = set()
    tracked_ids: set[str] = set()
    issues: list[dict[str, Any]] = []
    matched_open_count = 0
    matched_fill_count = 0

    reliability = order_activity.get("reliability")
    if isinstance(reliability, dict):
        pending_count = int(reliability.get("pending_count") or 0)
        unresolved_count = int(reliability.get("unresolved_count") or 0)
        if pending_count > 0 or unresolved_count > 0:
            issues.append(
                _reconciliation_issue(
                    level="warning",
                    issue_type="uncertain_order_intent",
                    message=(
                        f"{max(pending_count, unresolved_count)} order intent(s) "
                        "have an uncertain exchange outcome"
                    ),
                )
            )

    for tracked in tracked_orders:
        order_id = tracked["order_id"]
        tracked_ids.add(order_id)
        if tracked.get("key"):
            tracked_keys.add(tracked["key"])
        lookup_keys = [key for key in (tracked.get("key"), order_id) if key]
        is_open = any(key in open_lookup for key in lookup_keys)
        has_fill = any(key in trade_lookup for key in lookup_keys)
        is_closed = any(key in closed_lookup for key in lookup_keys)
        if is_open:
            matched_open_count += 1
            continue
        if has_fill:
            matched_fill_count += 1
            if tracked.get("expected_open"):
                issues.append(
                    _reconciliation_issue(
                        level="warning",
                        issue_type="tracked_order_filled_not_cleared",
                        strategy=tracked["strategy"],
                        exchange=tracked["exchange"],
                        symbol=tracked["symbol"],
                        order_id=order_id,
                        source_id=tracked.get("source_id", ""),
                        message="Local state still expects this order open, but a recent fill exists.",
                    )
                )
            continue
        if is_closed:
            if tracked.get("expected_open"):
                issues.append(
                    _reconciliation_issue(
                        level="warning",
                        issue_type="tracked_order_closed_not_cleared",
                        strategy=tracked["strategy"],
                        exchange=tracked["exchange"],
                        symbol=tracked["symbol"],
                        order_id=order_id,
                        source_id=tracked.get("source_id", ""),
                        message="Local state still expects this order open, but exchange reports it closed.",
                    )
                )
            continue
        if tracked.get("expected_open"):
            issues.append(
                _reconciliation_issue(
                    level="warning",
                    issue_type="tracked_order_missing",
                    strategy=tracked["strategy"],
                    exchange=tracked["exchange"],
                    symbol=tracked["symbol"],
                    order_id=order_id,
                    source_id=tracked.get("source_id", ""),
                    message="Local state tracks this open order, but it is not in exchange open orders or recent fills.",
                )
            )

    untracked_open_count = 0
    for order in open_orders:
        order_id = str(order.get("id") or "")
        if not order_id:
            continue
        keys = set(_activity_lookup_keys(order))
        attribution = (
            order.get("attribution")
            if isinstance(order.get("attribution"), dict)
            else None
        )
        if keys & tracked_keys or order_id in tracked_ids:
            continue
        untracked_open_count += 1
        issues.append(
            _reconciliation_issue(
                level="warning" if attribution else "info",
                issue_type="unmanaged_strategy_order"
                if attribution
                else "untracked_open_order",
                strategy=str((attribution or {}).get("strategy") or ""),
                exchange=str(order.get("exchange") or ""),
                symbol=str(order.get("symbol") or ""),
                order_id=order_id,
                source_id=str((attribution or {}).get("event_id") or ""),
                message=(
                    "Exchange has an attributed open order that is not in the current strategy runtime."
                    if attribution
                    else "Exchange has an open order that is not attributed to a local strategy."
                ),
            )
        )

    unattributed_fill_count = 0
    for trade in recent_trades:
        if str(trade.get("source") or "") != "unattributed":
            continue
        unattributed_fill_count += 1
        issues.append(
            _reconciliation_issue(
                level="info",
                issue_type="unattributed_fill",
                exchange=str(trade.get("exchange") or ""),
                symbol=str(trade.get("symbol") or ""),
                order_id=str(trade.get("order_id") or ""),
                message="Recent fill is not linked to a local strategy event.",
            )
        )

    if order_activity.get("status") == "error":
        accounts = [
            account
            for account in order_activity.get("accounts", []) or []
            if isinstance(account, dict) and account.get("status") != "idle"
        ]
        errored_accounts = [account for account in accounts if account.get("errors")]
        for account in errored_accounts:
            issues.insert(
                0,
                _reconciliation_issue(
                    level="warning",
                    issue_type="account_order_activity_error",
                    exchange=str(account.get("exchange") or ""),
                    message="Account order activity is unavailable; its trading resources are isolated.",
                ),
            )
        if not accounts or (errored_accounts and len(errored_accounts) == len(accounts)):
            issues.insert(
                0,
                _reconciliation_issue(
                    level="error",
                    issue_type="order_activity_error",
                    message="Order activity is unavailable for every active account.",
                ),
            )

    status = "ok"
    if any(issue["level"] == "error" for issue in issues):
        status = "error"
    elif any(issue["level"] == "warning" for issue in issues):
        status = "warning"
    auto_stop_reasons = _reconciliation_auto_stop_reasons(issues)
    recoverable_reasons = _reconciliation_reasons_for_types(
        issues,
        RECONCILIATION_RECOVERABLE_TYPES,
    )
    level_counts = {
        "error": sum(1 for issue in issues if issue.get("level") == "error"),
        "warning": sum(1 for issue in issues if issue.get("level") == "warning"),
        "info": sum(1 for issue in issues if issue.get("level") == "info"),
    }
    actionable_issue_count = level_counts["error"] + level_counts["warning"]
    return {
        "status": status,
        "tracked_order_count": len(tracked_orders),
        "matched_open_count": matched_open_count,
        "matched_fill_count": matched_fill_count,
        "untracked_open_count": untracked_open_count,
        "unattributed_fill_count": unattributed_fill_count,
        "issue_count": actionable_issue_count,
        "notice_count": level_counts["info"],
        "total_item_count": len(issues),
        "level_counts": level_counts,
        "critical_issue_count": len(auto_stop_reasons),
        "auto_stop_recommended": bool(auto_stop_reasons),
        "auto_stop_reasons": auto_stop_reasons[:10],
        "recoverable_issue_count": len(recoverable_reasons),
        "automatic_retry_active": bool(recoverable_reasons),
        "recoverable_reasons": recoverable_reasons[:10],
        "issues": issues[:50],
        "checked_at": time.time(),
    }
