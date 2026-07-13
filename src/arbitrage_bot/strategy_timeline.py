from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from .config import StrategyTimelineConfig, load_config
from .jsonl_rotation import rotate_jsonl_log_if_needed
from .trade_log import _read_recent_event_lines


@dataclass(frozen=True)
class StrategyTimelineEntry:
    event_id: str
    logged_at: float | None
    strategy: str
    mode: str
    status: str
    action: str
    event_type: str
    accounts: list[str]
    symbols: list[str]
    reason: str
    reasons: list[str]
    warnings: list[str]
    risk_triggers: list[str]
    metrics: dict[str, Any]
    source: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "logged_at": self.logged_at,
            "strategy": self.strategy,
            "mode": self.mode,
            "status": self.status,
            "action": self.action,
            "event_type": self.event_type,
            "accounts": self.accounts,
            "symbols": self.symbols,
            "reason": self.reason,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "risk_triggers": self.risk_triggers,
            "metrics": self.metrics,
            "source": self.source,
            "raw": self.raw,
        }


def _event_path(cfg: StrategyTimelineConfig) -> Path:
    return Path(cfg.path)


def _event_id(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        if value is not None and str(value).strip() != "":
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def _strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item)]


def _append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def _plan_orders(plan: dict[str, Any]) -> list[dict[str, Any]]:
    orders = plan.get("orders")
    if isinstance(orders, list):
        return [item for item in orders if isinstance(item, dict)]
    order = plan.get("order")
    if isinstance(order, dict):
        return [order]
    return []


def _accounts_and_symbols(
    plan: dict[str, Any], payload: dict[str, Any]
) -> tuple[list[str], list[str]]:
    accounts: list[str] = []
    symbols: list[str] = []
    exchange = plan.get("exchange")
    if exchange not in {None, "", "multi", "all"}:
        _append_unique(accounts, exchange)
    symbol = plan.get("symbol")
    if symbol not in {None, "", "configured_open_orders"}:
        _append_unique(symbols, symbol)
    for order in _plan_orders(plan):
        _append_unique(accounts, order.get("exchange"))
        _append_unique(symbols, order.get("symbol"))
    for key in ("exchange", "account"):
        _append_unique(accounts, payload.get(key))
    _append_unique(symbols, payload.get("symbol"))
    return accounts, symbols


def _message_from_row(row: Any) -> str:
    if isinstance(row, dict):
        parts = [
            str(row.get("exchange") or "").strip(),
            str(row.get("symbol") or "").strip(),
            str(row.get("order_id") or "").strip(),
            str(row.get("error") or row.get("message") or "").strip(),
        ]
        return " ".join(part for part in parts if part)
    return str(row)


def _reason_list(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    order_validation = (
        payload.get("order_validation")
        if isinstance(payload.get("order_validation"), dict)
        else {}
    )
    execution = (
        payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    )

    reasons: list[str] = []
    warnings: list[str] = []
    for value in _strings(risk.get("reasons")):
        _append_unique(reasons, value)
    for value in _strings(risk.get("warnings")):
        _append_unique(warnings, value)
    for value in _strings(payload.get("errors")):
        _append_unique(reasons, value)
    for value in _strings(payload.get("warnings")):
        _append_unique(warnings, value)
    for value in _strings(order_validation.get("errors")):
        _append_unique(reasons, f"order validation: {value}")
    for key in (
        "create_errors",
        "cancel_errors",
        "emergency_cancel_errors",
        "hedge_errors",
    ):
        rows = execution.get(key)
        if isinstance(rows, list):
            for row in rows:
                _append_unique(reasons, _message_from_row(row))
    reason = payload.get("reason")
    if reason:
        _append_unique(reasons, reason)
    cancel_reason = payload.get("cancel_reason")
    if cancel_reason:
        _append_unique(reasons, f"cancel: {cancel_reason}")
    return reasons, warnings


def _risk_triggers(reasons: list[str]) -> list[str]:
    keywords = (
        "risk.",
        "exceeds",
        "slippage",
        "depth",
        "order book",
        "daily loss",
        "post-only",
        "balance",
        "free ",
    )
    return [
        reason for reason in reasons if any(keyword in reason for keyword in keywords)
    ]


def _action_for_status(status: str, payload: dict[str, Any]) -> str:
    event_type = str(payload.get("type", ""))
    execution = (
        payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    )
    canceled_count = int(execution.get("canceled_count", 0) or 0)
    placed_count = int(execution.get("placed_count", 0) or 0)
    if canceled_count > 0 or "cancel" in event_type:
        return "cancel"
    if status in {
        "blocked_by_risk",
        "blocked_by_plan",
        "blocked_by_validation",
        "blocked_by_balance",
        "blocked_by_slippage",
    }:
        return "blocked"
    if status == "hedge_required":
        return "hedge_required"
    if status in {"no_opportunity", "live_disabled", "cooldown"}:
        return "no_order"
    if status in {"paused", "disabled", "waiting_for_start_price"}:
        return status
    if status in {"execution_error", "cancel_retry"}:
        return status
    if placed_count > 0 or status in {"placed", "execution_repaired"}:
        return "place"
    if status == "unchanged":
        return "unchanged"
    if status == "planned":
        return "plan"
    return status or "unknown"


def _max_slippage(plan: dict[str, Any]) -> float | None:
    values = [_as_float(order.get("slippage_bps")) for order in _plan_orders(plan)]
    numbers = [value for value in values if value is not None]
    return max(numbers) if numbers else None


def _metrics(
    payload: dict[str, Any],
    plan: dict[str, Any],
    opportunity: dict[str, Any],
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key in ("profit_quote", "profit_bps"):
        value = _as_float(opportunity.get(key) if opportunity else payload.get(key))
        if value is not None:
            metrics[key] = value
    if isinstance(payload.get("timing"), dict):
        for key, value in payload["timing"].items():
            if isinstance(value, (int, float)):
                metrics[key] = value
    plan_orders = _plan_orders(plan)
    if plan_orders:
        metrics["plan_order_count"] = len(plan_orders)
        metrics["plan_buy_order_count"] = sum(
            1 for order in plan_orders if str(order.get("side") or "") == "buy"
        )
        metrics["plan_sell_order_count"] = sum(
            1 for order in plan_orders if str(order.get("side") or "") == "sell"
        )
    for key in (
        "existing_spread_bps",
        "max_level_gap_bps",
        "bid_depth_quote",
        "ask_depth_quote",
        "mid_price",
    ):
        value = _as_float(plan.get(key))
        if value is not None:
            metrics[key] = value
    reprice_bps = _as_float(payload.get("reprice_bps"))
    if reprice_bps is not None:
        metrics["reprice_bps"] = reprice_bps
    protection = payload.get("protection")
    if isinstance(protection, dict):
        for key in (
            "max_slippage_bps",
            "max_allowed_slippage_bps",
            "opportunity_to_decision_ms",
        ):
            value = _as_float(protection.get(key))
            if value is not None:
                metrics[key] = value
    slippage = _max_slippage(plan)
    if slippage is not None:
        metrics.setdefault("max_slippage_bps", slippage)
    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    total_quote = _as_float(risk.get("total_quote_notional"))
    if total_quote is not None:
        metrics["total_quote_notional"] = total_quote
    execution = (
        payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    )
    for key in (
        "placed_count",
        "canceled_count",
        "create_latency_ms",
        "opportunity_to_submit_ms",
    ):
        value = _as_float(execution.get(key))
        if value is not None:
            metrics[key] = value
    fill_status = execution.get("fill_status")
    if isinstance(fill_status, dict):
        for key in ("imbalance_base", "buy_filled_base", "sell_filled_base"):
            value = _as_float(fill_status.get(key))
            if value is not None:
                metrics[key] = value
    return metrics


def strategy_timeline_event_from_payload(
    payload: dict[str, Any],
    *,
    source: str = "runtime",
) -> dict[str, Any]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    opportunity = (
        payload.get("opportunity")
        if isinstance(payload.get("opportunity"), dict)
        else {}
    )
    status = str(payload.get("status", ""))
    event_type = str(payload.get("type", ""))
    reasons, warnings = _reason_list(payload)
    accounts, symbols = _accounts_and_symbols(plan, payload)
    return {
        "type": "strategy_timeline",
        "event_type": event_type,
        "strategy": str(
            payload.get("strategy") or payload.get("runtime_strategy") or event_type
        ),
        "mode": str(payload.get("mode", "")),
        "status": status,
        "action": _action_for_status(status, payload),
        "accounts": accounts,
        "symbols": symbols,
        "reason": str((reasons or warnings or [""])[0]),
        "reasons": reasons,
        "warnings": warnings,
        "risk_triggers": _risk_triggers(reasons),
        "metrics": _metrics(payload, plan, opportunity),
        "source": source,
        "payload_status": status,
    }


def normalize_strategy_timeline_event(event: dict[str, Any]) -> StrategyTimelineEntry:
    reasons = _strings(event.get("reasons"))
    warnings = _strings(event.get("warnings"))
    return StrategyTimelineEntry(
        event_id=_event_id(event),
        logged_at=(
            float(event["logged_at"])
            if isinstance(event.get("logged_at"), (int, float))
            else None
        ),
        strategy=str(event.get("strategy", "")),
        mode=str(event.get("mode", "")),
        status=str(event.get("status", "")),
        action=str(event.get("action", "")),
        event_type=str(event.get("event_type", "")),
        accounts=_strings(event.get("accounts")),
        symbols=_strings(event.get("symbols")),
        reason=str(event.get("reason") or (reasons or warnings or [""])[0]),
        reasons=reasons,
        warnings=warnings,
        risk_triggers=_strings(event.get("risk_triggers")),
        metrics=(
            event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        ),
        source=str(event.get("source", "")),
        raw=event,
    )


def strategy_timeline_fingerprint(event: dict[str, Any]) -> str:
    payload = {
        "strategy": event.get("strategy"),
        "mode": event.get("mode"),
        "status": event.get("status"),
        "action": event.get("action"),
        "accounts": event.get("accounts"),
        "symbols": event.get("symbols"),
        "reason": event.get("reason"),
        "risk_triggers": event.get("risk_triggers"),
    }
    return _event_id(payload)


def write_strategy_timeline_event(
    cfg: StrategyTimelineConfig,
    event: dict[str, Any],
) -> dict[str, Any]:
    enriched = {
        "logged_at": time.time(),
        **event,
    }
    if not cfg.enabled:
        return enriched

    path = _event_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_jsonl_log_if_needed(
        path,
        max_bytes=cfg.rotate_max_bytes,
        keep_files=cfg.rotate_keep_files,
        compress=cfg.rotate_compress,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(enriched, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
    return enriched


def write_strategy_timeline_from_payload(
    cfg: StrategyTimelineConfig,
    payload: dict[str, Any],
    *,
    source: str = "runtime",
) -> dict[str, Any]:
    return write_strategy_timeline_event(
        cfg,
        strategy_timeline_event_from_payload(payload, source=source),
    )


def read_recent_strategy_timeline_events(
    cfg: StrategyTimelineConfig,
) -> list[dict[str, Any]]:
    if not cfg.enabled:
        return []

    path = _event_path(cfg)
    if not path.exists():
        return []

    lines = _read_recent_event_lines(path, cfg.max_recent_events)
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return list(reversed(events))


def read_recent_strategy_timeline_entries(
    cfg: StrategyTimelineConfig,
) -> list[StrategyTimelineEntry]:
    return [
        normalize_strategy_timeline_event(event)
        for event in read_recent_strategy_timeline_events(cfg)
    ]


def find_latest_strategy_timeline_entry(
    cfg: StrategyTimelineConfig,
    *,
    strategy: str,
    status: str,
) -> StrategyTimelineEntry | None:
    """Find a historical event without being limited by the UI's recent-event window."""
    if not cfg.enabled:
        return None
    path = _event_path(cfg)
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return None
    latest: StrategyTimelineEntry | None = None
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            entry = normalize_strategy_timeline_event(event)
            if entry.strategy == strategy and entry.status == status:
                latest = entry
    return latest


def summarize_strategy_timeline_entries(
    entries: list[StrategyTimelineEntry],
) -> dict[str, Any]:
    return {
        "event_count": len(entries),
        "blocked_count": sum(1 for item in entries if item.action == "blocked"),
        "no_order_count": sum(1 for item in entries if item.action == "no_order"),
        "cancel_count": sum(1 for item in entries if item.action == "cancel"),
        "execution_error_count": sum(
            1 for item in entries if item.action == "execution_error"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect strategy timeline events")
    parser.add_argument(
        "--config", default="config.acs.json", help="Path to JSON config"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Number of rows to show"
    )
    parser.add_argument("--json", action="store_true", help="Print JSON rows")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    timeline_cfg = cfg.strategy_timeline
    if args.limit is not None:
        timeline_cfg = dataclass_replace(
            timeline_cfg,
            max_recent_events=max(0, args.limit),
        )
    entries = read_recent_strategy_timeline_entries(timeline_cfg)
    if args.json:
        print(
            json.dumps(
                {
                    "summary": summarize_strategy_timeline_entries(entries),
                    "entries": [entry.to_dict() for entry in entries],
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return

    if not entries:
        print("No strategy timeline events.")
        return

    for entry in entries:
        print(
            " | ".join(
                [
                    entry.event_id,
                    entry.strategy or "-",
                    entry.mode or "-",
                    entry.action or "-",
                    entry.status or "-",
                    ",".join(entry.accounts) or "-",
                    ",".join(entry.symbols) or "-",
                    entry.reason or "-",
                ]
            )
        )


if __name__ == "__main__":
    main()
