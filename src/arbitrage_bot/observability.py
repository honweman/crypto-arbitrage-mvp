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


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _flag(value: Any) -> float:
    return 1.0 if bool(value) else 0.0


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
    scan = _dict(payload.get("scan"))
    order_activity = _dict(payload.get("order_activity"))
    market_maker = _dict(payload.get("market_maker"))
    spot_grid = _dict(payload.get("spot_grid"))
    derivatives = _dict(payload.get("derivatives"))
    execution_protection = _dict(payload.get("execution_protection"))
    readiness = _dict(payload.get("readiness"))
    readiness_summary = _dict(readiness.get("summary"))
    readiness_order_checks = _dict(readiness.get("order_checks"))
    program = _dict(payload.get("program"))
    operations = _dict(payload.get("operations"))
    risk = _dict(operations.get("risk"))
    if not risk:
        risk = {
            "enabled": readiness.get("risk_enabled"),
            "trading_enabled": readiness.get("trading_enabled"),
            "allow_live_trading": readiness.get("live_trading"),
        }
    trading_console = _dict(payload.get("trading_console"))
    mm_runtime = _dict(market_maker.get("runtime"))
    grid_runtime = _dict(spot_grid.get("runtime"))
    status = str(payload.get("status") or "unknown")
    readiness_status = str(readiness.get("status") or "unknown")
    lines = [
        "# HELP crypto_arb_status Current monitor status as a labeled gauge.",
        "# TYPE crypto_arb_status gauge",
        _line("crypto_arb_status", 1.0, {"status": status}),
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
            len(_list_items(payload.get("opportunities"))),
            {"status": status},
        ),
        "# HELP crypto_arb_warning_count Current warning count.",
        "# TYPE crypto_arb_warning_count gauge",
        _line("crypto_arb_warning_count", len(_list_items(payload.get("warnings")))),
        "# HELP crypto_arb_program_running Program switch state.",
        "# TYPE crypto_arb_program_running gauge",
        _line("crypto_arb_program_running", _flag(program.get("running"))),
        "# HELP crypto_arb_program_auto_stopped Program auto-stop state.",
        "# TYPE crypto_arb_program_auto_stopped gauge",
        _line("crypto_arb_program_auto_stopped", _flag(program.get("auto_stopped"))),
        "# HELP crypto_arb_risk_enabled Risk engine enabled state.",
        "# TYPE crypto_arb_risk_enabled gauge",
        _line("crypto_arb_risk_enabled", _flag(risk.get("enabled"))),
        "# HELP crypto_arb_risk_trading_enabled Risk trading switch state.",
        "# TYPE crypto_arb_risk_trading_enabled gauge",
        _line("crypto_arb_risk_trading_enabled", _flag(risk.get("trading_enabled"))),
        "# HELP crypto_arb_risk_live_trading_allowed Live trading risk switch state.",
        "# TYPE crypto_arb_risk_live_trading_allowed gauge",
        _line(
            "crypto_arb_risk_live_trading_allowed",
            _flag(risk.get("allow_live_trading")),
        ),
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
        "# HELP crypto_arb_order_activity_status Current order activity status.",
        "# TYPE crypto_arb_order_activity_status gauge",
        _line(
            "crypto_arb_order_activity_status",
            1.0,
            {"status": order_activity.get("status", "unknown")},
        ),
        "# HELP crypto_arb_reconciliation_issue_count Actionable order reconciliation issue count.",
        "# TYPE crypto_arb_reconciliation_issue_count gauge",
        _line(
            "crypto_arb_reconciliation_issue_count",
            _number(readiness_order_checks.get("reconciliation_issue_count")),
        ),
        "# HELP crypto_arb_market_maker_open_orders Tracked market maker open orders.",
        "# TYPE crypto_arb_market_maker_open_orders gauge",
        _line(
            "crypto_arb_market_maker_open_orders",
            _number(mm_runtime.get("open_order_count")),
            {"mode": mm_runtime.get("mode", "unknown")},
        ),
        "# HELP crypto_arb_market_maker_instance_status Market maker instance status by account and symbol.",
        "# TYPE crypto_arb_market_maker_instance_status gauge",
        "# HELP crypto_arb_market_maker_instance_open_orders Tracked market maker open orders by instance.",
        "# TYPE crypto_arb_market_maker_instance_open_orders gauge",
        "# HELP crypto_arb_market_maker_instance_placed_count Market maker placed order count by instance since task start.",
        "# TYPE crypto_arb_market_maker_instance_placed_count counter",
        "# HELP crypto_arb_market_maker_instance_canceled_count Market maker canceled order count by instance since task start.",
        "# TYPE crypto_arb_market_maker_instance_canceled_count counter",
        "# HELP crypto_arb_market_maker_instance_cycle_count Market maker cycle count by instance since task start.",
        "# TYPE crypto_arb_market_maker_instance_cycle_count counter",
        "# HELP crypto_arb_market_maker_instance_id_mismatch Market maker configured ID mismatch flag.",
        "# TYPE crypto_arb_market_maker_instance_id_mismatch gauge",
        "# HELP crypto_arb_spot_grid_open_orders Tracked spot grid open orders.",
        "# TYPE crypto_arb_spot_grid_open_orders gauge",
        _line(
            "crypto_arb_spot_grid_open_orders",
            _number(grid_runtime.get("open_order_count")),
            {"mode": grid_runtime.get("mode", "unknown")},
        ),
        "# HELP crypto_arb_derivatives_risk_status Current derivatives risk monitor status.",
        "# TYPE crypto_arb_derivatives_risk_status gauge",
        _line(
            "crypto_arb_derivatives_risk_status",
            1.0,
            {"status": derivatives.get("status", "unknown")},
        ),
        "# HELP crypto_arb_derivatives_position_count Current derivative position count.",
        "# TYPE crypto_arb_derivatives_position_count gauge",
        _line(
            "crypto_arb_derivatives_position_count",
            _number(derivatives.get("position_count")),
        ),
        "# HELP crypto_arb_derivatives_checked_account_count Checked derivative account count.",
        "# TYPE crypto_arb_derivatives_checked_account_count gauge",
        _line(
            "crypto_arb_derivatives_checked_account_count",
            _number(derivatives.get("checked_account_count")),
        ),
        "# HELP crypto_arb_derivatives_blocked_account_count Blocked derivative account count.",
        "# TYPE crypto_arb_derivatives_blocked_account_count gauge",
        _line(
            "crypto_arb_derivatives_blocked_account_count",
            sum(
                1
                for account in _list_items(derivatives.get("accounts"))
                if isinstance(account, dict) and account.get("status") == "blocked"
            ),
        ),
        "# HELP crypto_arb_readiness_status Current readiness status.",
        "# TYPE crypto_arb_readiness_status gauge",
        _line("crypto_arb_readiness_status", 1.0, {"status": readiness_status}),
        "# HELP crypto_arb_readiness_blocked_count Blocked readiness item count.",
        "# TYPE crypto_arb_readiness_blocked_count gauge",
        _line(
            "crypto_arb_readiness_blocked_count",
            _number(readiness_summary.get("blocked_count")),
        ),
        "# HELP crypto_arb_readiness_warning_count Warning readiness item count.",
        "# TYPE crypto_arb_readiness_warning_count gauge",
        _line(
            "crypto_arb_readiness_warning_count",
            _number(readiness_summary.get("warning_count")),
        ),
        "# HELP crypto_arb_readiness_action_count Suggested readiness action count.",
        "# TYPE crypto_arb_readiness_action_count gauge",
        _line(
            "crypto_arb_readiness_action_count",
            _number(readiness_summary.get("action_count")),
        ),
        "# HELP crypto_arb_execution_protection_count Multi-leg paper execution protection count.",
        "# TYPE crypto_arb_execution_protection_count gauge",
        _line(
            "crypto_arb_execution_protection_count",
            _number(execution_protection.get("protection_count")),
            {"status": execution_protection.get("status", "unknown")},
        ),
        "# HELP crypto_arb_execution_protection_blocked_count Blocked multi-leg paper protections.",
        "# TYPE crypto_arb_execution_protection_blocked_count gauge",
        _line(
            "crypto_arb_execution_protection_blocked_count",
            _number(execution_protection.get("blocked_count")),
        ),
        "# HELP crypto_arb_execution_protection_warning_count Warning multi-leg paper protections.",
        "# TYPE crypto_arb_execution_protection_warning_count gauge",
        _line(
            "crypto_arb_execution_protection_warning_count",
            _number(execution_protection.get("warning_count")),
        ),
        "# HELP crypto_arb_execution_protection_manual_review_count Multi-leg protections requiring manual review.",
        "# TYPE crypto_arb_execution_protection_manual_review_count gauge",
        _line(
            "crypto_arb_execution_protection_manual_review_count",
            _number(execution_protection.get("manual_review_count")),
        ),
    ]

    for instance in _list_items(market_maker.get("instances")):
        if not isinstance(instance, dict):
            continue
        config = _dict(instance.get("config"))
        runtime = _dict(instance.get("runtime"))
        instance_id = config.get("id") or instance.get("id") or "unknown"
        labels = {
            "id": instance_id,
            "exchange": config.get("exchange", "unknown"),
            "symbol": config.get("symbol", "unknown"),
            "mode": runtime.get("mode") or instance.get("mode") or "unknown",
            "status": runtime.get("status") or instance.get("status") or "unknown",
        }
        lines.extend(
            [
                _line("crypto_arb_market_maker_instance_status", 1.0, labels),
                _line(
                    "crypto_arb_market_maker_instance_open_orders",
                    _number(runtime.get("open_order_count")),
                    labels,
                ),
                _line(
                    "crypto_arb_market_maker_instance_placed_count",
                    _number(runtime.get("placed_count")),
                    labels,
                ),
                _line(
                    "crypto_arb_market_maker_instance_canceled_count",
                    _number(runtime.get("canceled_count")),
                    labels,
                ),
                _line(
                    "crypto_arb_market_maker_instance_cycle_count",
                    _number(runtime.get("cycle_count")),
                    labels,
                ),
                _line(
                    "crypto_arb_market_maker_instance_id_mismatch",
                    _flag(config.get("id_mismatch")),
                    {
                        "id": instance_id,
                        "expected_id": config.get("expected_id", ""),
                    },
                ),
            ]
        )

    lines.extend(
        [
            "# HELP crypto_arb_readiness_account_status Readiness status by account.",
            "# TYPE crypto_arb_readiness_account_status gauge",
        ]
    )
    for account in _list_items(readiness.get("accounts")):
        if not isinstance(account, dict):
            continue
        lines.append(
            _line(
                "crypto_arb_readiness_account_status",
                1.0,
                {
                    "account": account.get("key", "unknown"),
                    "status": account.get("status", "unknown"),
                    "used": str(bool(account.get("used"))).lower(),
                },
        )
    )

    lines.extend(
        [
            "# HELP crypto_arb_derivatives_account_status Derivative account risk status.",
            "# TYPE crypto_arb_derivatives_account_status gauge",
            "# HELP crypto_arb_derivatives_margin_usage_pct Derivative account margin usage percentage.",
            "# TYPE crypto_arb_derivatives_margin_usage_pct gauge",
            "# HELP crypto_arb_derivatives_min_liquidation_buffer_pct Minimum liquidation buffer percentage by derivative account.",
            "# TYPE crypto_arb_derivatives_min_liquidation_buffer_pct gauge",
            "# HELP crypto_arb_derivatives_position_notional_quote Derivative position notional by account in quote currency.",
            "# TYPE crypto_arb_derivatives_position_notional_quote gauge",
        ]
    )
    for account in _list_items(derivatives.get("accounts")):
        if not isinstance(account, dict):
            continue
        account_key = account.get("exchange", "unknown")
        summary = _dict(account.get("summary"))
        labels = {
            "account": account_key,
            "status": account.get("status", "unknown"),
        }
        lines.append(_line("crypto_arb_derivatives_account_status", 1.0, labels))
        if summary:
            account_label = {"account": account_key}
            lines.append(
                _line(
                    "crypto_arb_derivatives_margin_usage_pct",
                    _number(summary.get("margin_usage_pct")),
                    account_label,
                )
            )
            lines.append(
                _line(
                    "crypto_arb_derivatives_min_liquidation_buffer_pct",
                    _number(summary.get("min_liquidation_buffer_pct")),
                    account_label,
                )
            )
            lines.append(
                _line(
                    "crypto_arb_derivatives_position_notional_quote",
                    _number(summary.get("position_notional_quote")),
                    account_label,
                )
            )

    lines.extend(
        [
            "# HELP crypto_arb_readiness_strategy_status Readiness status by strategy.",
            "# TYPE crypto_arb_readiness_strategy_status gauge",
            "# HELP crypto_arb_strategy_live Strategy live state from the trading console.",
            "# TYPE crypto_arb_strategy_live gauge",
            "# HELP crypto_arb_strategy_paused Strategy pause state from the trading console.",
            "# TYPE crypto_arb_strategy_paused gauge",
            "# HELP crypto_arb_strategy_configured Strategy configured state from the trading console.",
            "# TYPE crypto_arb_strategy_configured gauge",
        ]
    )
    strategy_rows = [
        row
        for row in _list_items(readiness.get("strategies"))
        if isinstance(row, dict)
    ]
    if not strategy_rows:
        strategy_rows = [
            row
            for row in _list_items(trading_console.get("strategies"))
            if isinstance(row, dict)
        ]
    for strategy in strategy_rows:
        strategy_id = strategy.get("id", "unknown")
        labels = {
            "strategy": strategy_id,
            "status": strategy.get("status", "unknown"),
            "mode": strategy.get("mode", "unknown"),
        }
        lines.append(_line("crypto_arb_readiness_strategy_status", 1.0, labels))
        lines.append(
            _line(
                "crypto_arb_strategy_live",
                _flag(strategy.get("live")),
                {"strategy": strategy_id},
            )
        )
        lines.append(
            _line(
                "crypto_arb_strategy_paused",
                _flag(strategy.get("paused")),
                {"strategy": strategy_id},
            )
        )
        lines.append(
            _line(
                "crypto_arb_strategy_configured",
                _flag(strategy.get("configured")),
                {"strategy": strategy_id},
            )
        )

    lines.extend(
        [
            "# HELP crypto_arb_runtime_status Runtime status by background strategy loop.",
            "# TYPE crypto_arb_runtime_status gauge",
            _line(
                "crypto_arb_runtime_status",
                1.0,
                {
                    "strategy": "market_maker",
                    "status": mm_runtime.get("status", "unknown"),
                    "mode": mm_runtime.get("mode", "unknown"),
                },
            ),
            _line(
                "crypto_arb_runtime_status",
                1.0,
                {
                    "strategy": "spot_grid",
                    "status": grid_runtime.get("status", "unknown"),
                    "mode": grid_runtime.get("mode", "unknown"),
                },
            ),
        ]
    )
    return "\n".join(lines) + "\n"
