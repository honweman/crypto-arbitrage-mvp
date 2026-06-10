from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from .config import TradeLogConfig, load_config


@dataclass(frozen=True)
class TradeLogEntry:
    event_id: str
    logged_at: float | None
    event_type: str
    strategy: str
    mode: str
    status: str
    exchange: str
    symbol: str
    side: str
    order_count: int
    total_quote_notional: float
    placed_count: int
    canceled_count: int
    placed_order_ids: list[str]
    risk_level: str
    risk_approved: bool | None
    reason: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "logged_at": self.logged_at,
            "event_type": self.event_type,
            "strategy": self.strategy,
            "mode": self.mode,
            "status": self.status,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "order_count": self.order_count,
            "total_quote_notional": self.total_quote_notional,
            "placed_count": self.placed_count,
            "canceled_count": self.canceled_count,
            "placed_order_ids": self.placed_order_ids,
            "risk_level": self.risk_level,
            "risk_approved": self.risk_approved,
            "reason": self.reason,
            "raw": self.raw,
        }


def _event_path(cfg: TradeLogConfig) -> Path:
    return Path(cfg.path)


def write_trade_event(
    cfg: TradeLogConfig,
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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(enriched, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
    return enriched


def _event_id(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _first_order_side(plan: dict[str, Any]) -> str:
    order = plan.get("order")
    if isinstance(order, dict):
        return str(order.get("side", ""))
    orders = plan.get("orders")
    if isinstance(orders, list) and orders and isinstance(orders[0], dict):
        return str(orders[0].get("side", ""))
    return str(plan.get("side", ""))


def normalize_trade_event(event: dict[str, Any]) -> TradeLogEntry:
    plan = event.get("plan") if isinstance(event.get("plan"), dict) else {}
    risk = event.get("risk") if isinstance(event.get("risk"), dict) else {}
    execution = (
        event.get("execution")
        if isinstance(event.get("execution"), dict)
        else {}
    )
    reasons = risk.get("reasons") if isinstance(risk.get("reasons"), list) else []
    warnings = risk.get("warnings") if isinstance(risk.get("warnings"), list) else []
    reason = str((reasons or warnings or [""])[0])
    placed_order_ids = execution.get("placed_order_ids")
    if not isinstance(placed_order_ids, list):
        placed_order_ids = []

    event_type = str(event.get("type", ""))
    strategy = str(event.get("strategy") or event_type)
    return TradeLogEntry(
        event_id=_event_id(event),
        logged_at=(
            float(event["logged_at"])
            if isinstance(event.get("logged_at"), (int, float))
            else None
        ),
        event_type=event_type,
        strategy=strategy,
        mode=str(event.get("mode", "")),
        status=str(event.get("status", "")),
        exchange=str(plan.get("exchange", "")),
        symbol=str(plan.get("symbol", "")),
        side=_first_order_side(plan),
        order_count=int(risk.get("order_count", 0) or 0),
        total_quote_notional=float(risk.get("total_quote_notional", 0.0) or 0.0),
        placed_count=int(execution.get("placed_count", 0) or 0),
        canceled_count=int(
            execution.get("canceled_count", event.get("canceled_count", 0)) or 0
        ),
        placed_order_ids=[str(order_id) for order_id in placed_order_ids],
        risk_level=str(risk.get("level", "")),
        risk_approved=(
            bool(risk["approved"]) if isinstance(risk.get("approved"), bool) else None
        ),
        reason=reason,
        raw=event,
    )


def read_recent_trade_events(
    cfg: TradeLogConfig,
) -> list[dict[str, Any]]:
    if not cfg.enabled:
        return []

    path = _event_path(cfg)
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-cfg.max_recent_events :]:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return list(reversed(events))


def read_recent_trade_entries(cfg: TradeLogConfig) -> list[TradeLogEntry]:
    return [normalize_trade_event(event) for event in read_recent_trade_events(cfg)]


def summarize_trade_entries(entries: list[TradeLogEntry]) -> dict[str, Any]:
    return {
        "event_count": len(entries),
        "placed_event_count": sum(1 for item in entries if item.status == "placed"),
        "blocked_event_count": sum(
            1 for item in entries if item.status == "blocked_by_risk"
        ),
        "placed_order_count": sum(item.placed_count for item in entries),
        "canceled_order_count": sum(item.canceled_count for item in entries),
        "total_quote_notional": sum(item.total_quote_notional for item in entries),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect normalized trade log events")
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument("--limit", type=int, default=None, help="Number of rows to show")
    parser.add_argument("--json", action="store_true", help="Print JSON rows")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    log_cfg = cfg.trade_log
    if args.limit is not None:
        log_cfg = dataclass_replace(
            log_cfg,
            max_recent_events=max(0, args.limit),
        )
    entries = read_recent_trade_entries(log_cfg)
    if args.json:
        print(
            json.dumps(
                {
                    "summary": summarize_trade_entries(entries),
                    "entries": [entry.to_dict() for entry in entries],
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return

    if not entries:
        print("No trade events.")
        return

    for entry in entries:
        print(
            " | ".join(
                [
                    entry.event_id,
                    entry.strategy or "-",
                    entry.mode or "-",
                    entry.status or "-",
                    entry.exchange or "-",
                    entry.symbol or "-",
                    entry.side or "-",
                    f"orders={entry.order_count}",
                    f"notional={entry.total_quote_notional:.8f}",
                    f"risk={entry.risk_level or '-'}",
                    entry.reason or "-",
                ]
            )
        )


if __name__ == "__main__":
    main()
