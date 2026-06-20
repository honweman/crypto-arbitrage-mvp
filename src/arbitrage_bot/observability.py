from __future__ import annotations

import logging
import os
import re
from typing import Any


def configure_logging(*, default_level: str = "INFO") -> None:
    level_name = os.environ.get("CRYPTO_ARB_LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _label_value(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", value)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "unknown"


def _line(name: str, value: float, labels: dict[str, Any] | None = None) -> str:
    if not labels:
        return f"{name} {value:.12g}"
    label_text = ",".join(
        f'{_metric_name(str(key))}="{_label_value(label_value)}"'
        for key, label_value in sorted(labels.items())
    )
    return f"{name}{{{label_text}}} {value:.12g}"


def render_prometheus_metrics(payload: dict[str, Any]) -> str:
    scan = payload.get("scan") if isinstance(payload.get("scan"), dict) else {}
    order_activity = (
        payload.get("order_activity")
        if isinstance(payload.get("order_activity"), dict)
        else {}
    )
    market_maker = (
        payload.get("market_maker")
        if isinstance(payload.get("market_maker"), dict)
        else {}
    )
    spot_grid = (
        payload.get("spot_grid")
        if isinstance(payload.get("spot_grid"), dict)
        else {}
    )
    program = payload.get("program") if isinstance(payload.get("program"), dict) else {}
    mm_runtime = (
        market_maker.get("runtime")
        if isinstance(market_maker.get("runtime"), dict)
        else {}
    )
    grid_runtime = (
        spot_grid.get("runtime")
        if isinstance(spot_grid.get("runtime"), dict)
        else {}
    )
    status = str(payload.get("status") or "unknown")
    lines = [
        "# HELP crypto_arb_scan_count Total completed monitor scan cycles.",
        "# TYPE crypto_arb_scan_count counter",
        _line("crypto_arb_scan_count", _number(scan.get("count"))),
        "# HELP crypto_arb_scan_elapsed_ms Last scan elapsed time in milliseconds.",
        "# TYPE crypto_arb_scan_elapsed_ms gauge",
        _line("crypto_arb_scan_elapsed_ms", _number(scan.get("elapsed_ms"))),
        "# HELP crypto_arb_opportunity_count Current opportunity count.",
        "# TYPE crypto_arb_opportunity_count gauge",
        _line(
            "crypto_arb_opportunity_count",
            len(payload.get("opportunities") or []),
            {"status": status},
        ),
        "# HELP crypto_arb_warning_count Current warning count.",
        "# TYPE crypto_arb_warning_count gauge",
        _line("crypto_arb_warning_count", len(payload.get("warnings") or [])),
        "# HELP crypto_arb_program_running Program switch state.",
        "# TYPE crypto_arb_program_running gauge",
        _line("crypto_arb_program_running", 1.0 if program.get("running") else 0.0),
        "# HELP crypto_arb_open_order_count Current open order count from order activity.",
        "# TYPE crypto_arb_open_order_count gauge",
        _line(
            "crypto_arb_open_order_count",
            _number(order_activity.get("open_order_count")),
        ),
        "# HELP crypto_arb_recent_trade_count Current recent trade count.",
        "# TYPE crypto_arb_recent_trade_count gauge",
        _line(
            "crypto_arb_recent_trade_count",
            _number(order_activity.get("recent_trade_count")),
        ),
        "# HELP crypto_arb_market_maker_open_orders Tracked market maker open orders.",
        "# TYPE crypto_arb_market_maker_open_orders gauge",
        _line(
            "crypto_arb_market_maker_open_orders",
            _number(mm_runtime.get("open_order_count")),
            {"mode": mm_runtime.get("mode", "unknown")},
        ),
        "# HELP crypto_arb_spot_grid_open_orders Tracked spot grid open orders.",
        "# TYPE crypto_arb_spot_grid_open_orders gauge",
        _line(
            "crypto_arb_spot_grid_open_orders",
            _number(grid_runtime.get("open_order_count")),
            {"mode": grid_runtime.get("mode", "unknown")},
        ),
    ]
    return "\n".join(lines) + "\n"

