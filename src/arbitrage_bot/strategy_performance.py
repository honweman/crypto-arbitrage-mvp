from __future__ import annotations

import math
import time
from typing import Any, Iterable


SOURCE_STRATEGIES = {
    "market_maker": "market_maker",
    "arbitrage": "spot_spread",
    "auto_buy_sell": "slow_execution",
    "manual": "manual",
    "unattributed": "unattributed",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_strategy(value: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_")
    if key in {"slow_execution", "auto_buy_sell", "slow_execution_cancel"}:
        return "slow_execution"
    if key in {"spot_spread", "arbitrage", "spot_spread_execution"}:
        return "spot_spread"
    return key or "unattributed"


def _row(strategy: str, instance_id: str) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "instance_id": instance_id or "default",
        "account": "",
        "symbol": "",
        "mode": "",
        "event_count": 0,
        "blocked_count": 0,
        "error_count": 0,
        "submitted_order_count": 0,
        "paper_order_count": 0,
        "fill_count": 0,
        "filled_order_count": 0,
        "notional_common": 0.0,
        "fees_common": 0.0,
        "realized_pnl": 0.0,
        "paper_profit_quote": 0.0,
        "paper_event_count": 0,
        "total_base": 0.0,
        "total_cost": 0.0,
        "buy_base": 0.0,
        "buy_cost": 0.0,
        "sell_base": 0.0,
        "sell_cost": 0.0,
        "_submitted_ids": set(),
        "_filled_ids": set(),
        "_latencies": [],
        "_slippages": [],
    }


def _event_instance(entry: Any) -> str:
    value = str(getattr(entry, "strategy_instance_id", "") or "")
    if value and value != "default":
        return value
    raw = getattr(entry, "raw", {})
    if isinstance(raw, dict):
        return str(raw.get("task_id") or raw.get("strategy_instance_id") or value)
    return value or "default"


def _plan_orders(raw: dict[str, Any]) -> list[dict[str, Any]]:
    plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
    order = plan.get("order")
    if isinstance(order, dict):
        return [order]
    return [row for row in plan.get("orders", []) if isinstance(row, dict)]


def _adverse_slippage_bps(
    fill: dict[str, Any],
    event: Any | None,
) -> float | None:
    if event is None:
        return None
    price = _number(fill.get("price"))
    if price is None or price <= 0:
        return None
    side = str(fill.get("side") or "").lower()
    exchange = str(fill.get("exchange") or "")
    symbol = str(fill.get("symbol") or "")
    for order in _plan_orders(getattr(event, "raw", {})):
        if side and str(order.get("side") or "").lower() != side:
            continue
        if exchange and order.get("exchange") and order.get("exchange") != exchange:
            continue
        if symbol and order.get("symbol") and order.get("symbol") != symbol:
            continue
        reference = _number(order.get("price"))
        if reference is None or reference <= 0:
            continue
        signed = price - reference if side == "buy" else reference - price
        return max(0.0, signed / reference * 10_000)
    return None


def build_strategy_performance_payload(
    entries: Iterable[Any],
    fills: Iterable[dict[str, Any]],
    *,
    currency: str,
    market_maker_runtime: dict[str, Any] | None = None,
    auto_buy_sell_tasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    events_by_id: dict[str, Any] = {}

    def get_row(strategy: str, instance_id: str) -> dict[str, Any]:
        key = (_canonical_strategy(strategy), instance_id or "default")
        if key not in rows:
            rows[key] = _row(*key)
        return rows[key]

    for entry in entries:
        strategy = _canonical_strategy(str(getattr(entry, "strategy", "")))
        instance_id = _event_instance(entry)
        row = get_row(strategy, instance_id)
        event_id = str(getattr(entry, "event_id", "") or "")
        if event_id:
            events_by_id[event_id] = entry
        row["event_count"] += 1
        row["mode"] = str(getattr(entry, "mode", "") or row["mode"])
        row["account"] = str(getattr(entry, "exchange", "") or row["account"])
        row["symbol"] = str(getattr(entry, "symbol", "") or row["symbol"])
        status = str(getattr(entry, "status", "") or "")
        if status.startswith("blocked") or status in {"hedge_required", "cancel_retry"}:
            row["blocked_count"] += 1
        if status in {"error", "execution_error", "hedge_required"}:
            row["error_count"] += 1
        placed_ids = {
            str(order_id)
            for order_id in getattr(entry, "placed_order_ids", []) or []
            if order_id
        }
        row["_submitted_ids"].update(placed_ids)
        row["submitted_order_count"] += int(getattr(entry, "placed_count", 0) or 0)
        raw = getattr(entry, "raw", {})
        raw = raw if isinstance(raw, dict) else {}
        paper = raw.get("paper_execution")
        if isinstance(paper, dict):
            row["paper_event_count"] += 1
            row["paper_order_count"] += int(paper.get("order_count") or 0)
            row["paper_profit_quote"] += float(
                paper.get("estimated_profit_quote") or 0.0
            )
        execution = (
            raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
        )
        latency = _number(execution.get("opportunity_to_submit_ms"))
        if latency is not None:
            row["_latencies"].append(latency)

    for fill in fills:
        attribution = (
            fill.get("attribution") if isinstance(fill.get("attribution"), dict) else {}
        )
        strategy = _canonical_strategy(
            str(
                attribution.get("strategy")
                or SOURCE_STRATEGIES.get(str(fill.get("source") or ""), "unattributed")
            )
        )
        instance_id = str(
            fill.get("strategy_instance_id")
            or attribution.get("strategy_instance_id")
            or "default"
        )
        row = get_row(strategy, instance_id)
        row["account"] = str(fill.get("exchange") or row["account"])
        row["symbol"] = str(fill.get("symbol") or row["symbol"])
        row["fill_count"] += 1
        order_id = str(fill.get("order_id") or "")
        if order_id:
            row["_filled_ids"].add(order_id)
        amount = _number(fill.get("amount")) or 0.0
        cost = _number(fill.get("notional_common"))
        if cost is None:
            cost = _number(fill.get("cost")) or 0.0
        row["total_base"] += amount
        row["total_cost"] += cost
        row["notional_common"] += cost
        row["fees_common"] += _number(fill.get("fee_common")) or 0.0
        row["realized_pnl"] += _number(fill.get("realized_pnl_common")) or 0.0
        side = str(fill.get("side") or "").lower()
        if side in {"buy", "sell"}:
            row[f"{side}_base"] += amount
            row[f"{side}_cost"] += cost
        event = events_by_id.get(str(attribution.get("event_id") or ""))
        slippage = _adverse_slippage_bps(fill, event)
        if slippage is not None:
            row["_slippages"].append(slippage)

    mm_payload = market_maker_runtime or {}
    for instance in mm_payload.get("instances", []) or []:
        if not isinstance(instance, dict):
            continue
        config = (
            instance.get("config") if isinstance(instance.get("config"), dict) else {}
        )
        instance_id = str(instance.get("id") or config.get("id") or "default")
        row = get_row("market_maker", instance_id)
        row["account"] = str(config.get("exchange") or row["account"])
        row["symbol"] = str(config.get("symbol") or row["symbol"])
        row["mode"] = str(instance.get("mode") or row["mode"])

    for task in (auto_buy_sell_tasks or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        config = task.get("config") if isinstance(task.get("config"), dict) else {}
        row = get_row("slow_execution", str(task.get("id") or "default"))
        row["account"] = str(config.get("exchange") or row["account"])
        row["symbol"] = str(config.get("symbol") or row["symbol"])
        row["mode"] = "live"
        row["progress_pct"] = _number(task.get("progress_pct")) or 0.0
        row["progress_mode"] = str(task.get("progress_mode") or "")
        row["filled_base"] = _number(task.get("filled_base")) or 0.0
        row["filled_quote"] = _number(task.get("filled_quote")) or 0.0

    result_rows: list[dict[str, Any]] = []
    for row in rows.values():
        submitted_ids = row.pop("_submitted_ids")
        filled_ids = row.pop("_filled_ids")
        latencies = row.pop("_latencies")
        slippages = row.pop("_slippages")
        submitted_count = max(row["submitted_order_count"], len(submitted_ids))
        row["submitted_order_count"] = submitted_count
        row["filled_order_count"] = len(filled_ids)
        row["fill_rate_pct"] = (
            min(100.0, len(filled_ids) / submitted_count * 100)
            if submitted_count > 0
            else None
        )
        row["average_fill_price"] = (
            row["total_cost"] / row["total_base"] if row["total_base"] > 0 else None
        )
        row["average_slippage_bps"] = (
            sum(slippages) / len(slippages) if slippages else None
        )
        row["average_submit_latency_ms"] = (
            sum(latencies) / len(latencies) if latencies else None
        )
        row["max_submit_latency_ms"] = max(latencies) if latencies else None
        if row["strategy"] == "market_maker":
            buy_avg = row["buy_cost"] / row["buy_base"] if row["buy_base"] > 0 else None
            sell_avg = (
                row["sell_cost"] / row["sell_base"] if row["sell_base"] > 0 else None
            )
            matched_base = min(row["buy_base"], row["sell_base"])
            spread_capture = (
                (sell_avg - buy_avg) * matched_base
                if buy_avg is not None and sell_avg is not None
                else 0.0
            )
            row["spread_capture_estimate"] = spread_capture
            row["inventory_pnl_residual"] = row["realized_pnl"] - spread_capture
        if row["strategy"] == "slow_execution" and row.get("filled_base", 0) > 0:
            row["task_average_fill_price"] = (
                row.get("filled_quote", 0.0) / row["filled_base"]
            )
        row["paper_vs_live_delta"] = (
            row["realized_pnl"] - row["paper_profit_quote"]
            if row["paper_event_count"] > 0
            else None
        )
        result_rows.append(row)

    result_rows.sort(
        key=lambda item: (
            item["strategy"],
            item["account"],
            item["symbol"],
            item["instance_id"],
        )
    )
    return {
        "status": "ok",
        "currency": currency,
        "window": "daily",
        "row_count": len(result_rows),
        "rows": result_rows,
        "summary": {
            "realized_pnl": sum(row["realized_pnl"] for row in result_rows),
            "fees_common": sum(row["fees_common"] for row in result_rows),
            "fill_count": sum(row["fill_count"] for row in result_rows),
            "submitted_order_count": sum(
                row["submitted_order_count"] for row in result_rows
            ),
        },
        "updated_at": time.time(),
    }
