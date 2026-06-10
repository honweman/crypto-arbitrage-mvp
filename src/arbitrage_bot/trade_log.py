from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import TradeLogConfig


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
