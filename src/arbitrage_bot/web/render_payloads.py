from __future__ import annotations

from collections.abc import Iterable
from typing import Any


STATE_VIEW_IDS = {"status", "settings", "records"}


def _copy_payload_keys(
    payload: dict[str, Any] | None,
    keys: Iterable[str],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in keys if key in payload}


def _compact_config_payload(config: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    if full:
        return config
    return _copy_payload_keys(
        config,
        (
            "poll_seconds",
            "notional_quote",
            "min_profit_quote",
            "min_profit_bps",
            "common_quote_currency",
            "triangular_arbitrage",
            "contract_strategies",
        ),
    )


def _compact_plan_payload(
    plan: dict[str, Any] | None,
    *,
    include_orders: bool,
) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    if include_orders:
        return plan
    return {key: value for key, value in plan.items() if key != "orders"}


def _compact_market_maker_runtime(
    runtime: dict[str, Any] | None,
    *,
    include_orders: bool,
) -> dict[str, Any]:
    if not isinstance(runtime, dict):
        return {}
    result = dict(runtime)
    if isinstance(result.get("last_plan"), dict):
        result["last_plan"] = _compact_plan_payload(
            result["last_plan"],
            include_orders=include_orders,
        )
    return result

def _compact_market_maker_payload(
    market_maker: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return market_maker
    result = _copy_payload_keys(
        market_maker,
        (
            "status",
            "mode",
            "market_data",
            "quote_conversion",
            "exchange_features",
            "error",
        ),
    )
    result["runtime"] = _compact_market_maker_runtime(
        market_maker.get("runtime"),
        include_orders=False,
    )
    plan = market_maker.get("plan")
    if not isinstance(plan, dict):
        runtime_plan = result.get("runtime", {}).get("last_plan")
        plan = runtime_plan if isinstance(runtime_plan, dict) else None
    result["plan"] = _compact_plan_payload(plan, include_orders=False)
    return result


def _compact_slow_execution_payload(
    slow_execution: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    def compact_task_config(config: dict[str, Any] | None) -> dict[str, Any]:
        return _copy_payload_keys(
            config,
            (
                "exchange",
                "symbol",
                "side",
                "price_mode",
                "price_offset_bps",
                "total_base",
                "total_quote",
                "unlimited_total",
                "slice_mode",
                "slice_base",
                "slice_quote",
                "slice_base_min",
                "slice_base_max",
                "randomize_slice",
                "interval_seconds",
                "order_ttl_seconds",
                "start_price",
                "stop_price",
            ),
        )

    def compact_task_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(plan, dict):
            return None
        return _copy_payload_keys(
            plan,
            (
                "status",
                "best_bid",
                "best_ask",
                "mid_price",
                "trigger_price",
                "order",
                "observed_at",
            ),
        )

    def compact_task_execution(
        execution: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(execution, dict):
            return None
        return _copy_payload_keys(
            execution,
            (
                "placed_count",
                "canceled_count",
                "placed_order_ids",
                "canceled_order_ids",
                "errors",
            ),
        )

    def compact_task(task: dict[str, Any]) -> dict[str, Any]:
        compact = _copy_payload_keys(
            task,
            (
                "id",
                "status",
                "last_error",
                "last_status",
                "filled_base",
                "filled_quote",
                "remaining_base",
                "remaining_quote",
                "progress_label",
                "progress_mode",
                "progress_pct",
                "open_order_count",
                "placed_count",
                "canceled_count",
                "start_price_triggered",
                "last_cycle_at",
                "next_run_at",
                "last_fill_at",
                "created_at",
                "started_at",
                "updated_at",
                "finished_at",
            ),
        )
        compact["config"] = compact_task_config(task.get("config"))
        compact["last_plan"] = compact_task_plan(task.get("last_plan"))
        compact["last_execution"] = compact_task_execution(task.get("last_execution"))
        return compact

    def compact_tasks(task_payload: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(task_payload, dict):
            return {}
        result = _copy_payload_keys(
            task_payload,
            ("status", "path", "task_count", "active_count", "updated_at", "error"),
        )
        result["tasks"] = [
            compact_task(task)
            for task in task_payload.get("tasks", [])
            if isinstance(task, dict)
        ]
        return result

    if full:
        result = dict(slow_execution)
    else:
        result = _copy_payload_keys(
            slow_execution,
            ("status", "mode", "plan", "tasks", "error"),
        )
    if "tasks" in result:
        result["tasks"] = compact_tasks(result.get("tasks"))
    return result


def _compact_strategy_plan_payload(
    strategy_payload: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return strategy_payload
    return _copy_payload_keys(
        strategy_payload,
        (
            "status",
            "mode",
            "plan",
            "config",
            "accounts",
            "quote_conversion",
            "safety",
            "error",
        ),
    )


def _compact_order_activity_payload(
    order_activity: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return order_activity
    return _copy_payload_keys(
        order_activity,
        (
            "status",
            "open_order_count",
            "closed_order_count",
            "recent_trade_count",
            "pnl_summary",
            "daily_pnl",
            "reconciliation",
            "checked_account_count",
            "total_account_count",
            "last_finished",
            "errors",
            "warnings",
        ),
    )


def _compact_account_balances_payload(
    account_balances: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return account_balances
    return _copy_payload_keys(
        account_balances,
        (
            "status",
            "checked_account_count",
            "total_account_count",
            "last_finished",
            "errors",
            "warnings",
        ),
    )


def _compact_derivatives_payload(
    derivatives: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return derivatives
    return _copy_payload_keys(
        derivatives,
        (
            "status",
            "position_count",
            "checked_account_count",
            "total_account_count",
            "funding_rate_count",
            "limits",
            "last_finished",
            "errors",
            "warnings",
        ),
    )


def _compact_funding_basis_payload(
    funding_basis: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return funding_basis
    return _copy_payload_keys(
        funding_basis,
        (
            "status",
            "mode",
            "candidate_count",
            "configured_count",
            "checked_count",
            "last_finished",
            "errors",
            "warnings",
        ),
    )


def _compact_options_arbitrage_payload(
    options_arbitrage: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return options_arbitrage
    return _copy_payload_keys(
        options_arbitrage,
        (
            "status",
            "mode",
            "candidate_count",
            "parity_candidate_count",
            "enhanced_candidate_count",
            "configured_count",
            "checked_count",
            "thresholds",
            "risk",
            "execution_controls",
            "last_finished",
            "errors",
            "warnings",
        ),
    )


def _compact_contract_strategies_payload(
    contract_strategies: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return contract_strategies
    return _copy_payload_keys(
        contract_strategies,
        (
            "status",
            "mode",
            "summary",
            "candidate_count",
            "blocked_count",
            "configured_count",
            "derivative_status",
            "execution_controls",
            "last_finished",
            "errors",
            "warnings",
        ),
    )


def _compact_execution_protection_payload(
    execution_protection: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return execution_protection
    return _copy_payload_keys(
        execution_protection,
        (
            "status",
            "mode",
            "protection_count",
            "ok_count",
            "blocked_count",
            "warning_count",
            "manual_review_count",
            "slippage_block_count",
            "stale_block_count",
            "top_reasons",
            "updated_at",
        ),
    )


def _compact_onchain_payload(
    onchain: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return onchain
    history = onchain.get("history") if isinstance(onchain, dict) else {}
    compact = _copy_payload_keys(
        onchain,
        ("status", "label", "mint", "last_finished", "error"),
    )
    if isinstance(history, dict):
        compact["history"] = _copy_payload_keys(
            history,
            ("enabled", "path", "baseline_at", "updated_at", "event_count"),
        )
    return compact


def _compact_operations_payload(
    operations: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return operations
    return _copy_payload_keys(operations, ("risk",))


def _compact_strategy_center_payload(
    strategy_center: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    if full:
        return strategy_center
    return _copy_payload_keys(
        strategy_center,
        (
            "status",
            "updated_at",
            "summary",
            "funding_arbitrage",
            "signal_bot",
            "signals",
            "error",
        ),
    )


def state_payload_for_view(
    payload: dict[str, Any],
    view: str | None = None,
) -> dict[str, Any]:
    if view not in STATE_VIEW_IDS:
        return payload

    is_status = view == "status"
    is_settings = view == "settings"
    is_records = view == "records"

    result: dict[str, Any] = {
        "status": payload.get("status"),
        "config": _compact_config_payload(
            payload.get("config", {}),
            full=is_settings,
        ),
        "scan": payload.get("scan", {}),
        "opportunities": payload.get("opportunities", []),
        "portfolio": payload.get("portfolio", {}),
        "program": payload.get("program", {}),
        "warnings": payload.get("warnings", []),
        "market_maker": _compact_market_maker_payload(
            payload.get("market_maker", {}),
            full=is_settings,
        ),
        "slow_execution": _compact_slow_execution_payload(
            payload.get("slow_execution", {}),
            full=is_settings,
        ),
        "spot_grid": _compact_strategy_plan_payload(
            payload.get("spot_grid", {}),
            full=is_settings,
        ),
        "dca": _compact_strategy_plan_payload(
            payload.get("dca", {}),
            full=is_settings,
        ),
        "execution_algo": _compact_strategy_plan_payload(
            payload.get("execution_algo", {}),
            full=is_settings,
        ),
        "backtest": _copy_payload_keys(
            payload.get("backtest", {}),
            (
                "status",
                "mode",
                "result",
                "config",
                "accounts",
                "quote_conversion",
                "error",
            ),
        )
        if is_settings
        else _copy_payload_keys(
            payload.get("backtest", {}),
            ("status", "mode", "result", "error"),
        ),
        "spot_arbitrage": payload.get("spot_arbitrage", {}),
        "operations": _compact_operations_payload(
            payload.get("operations", {}),
            full=is_records,
        ),
        "strategy_center": _compact_strategy_center_payload(
            payload.get("strategy_center", {}),
            full=is_settings,
        ),
        "funding_basis": _compact_funding_basis_payload(
            payload.get("funding_basis", {}),
            full=is_status,
        ),
        "options_arbitrage": _compact_options_arbitrage_payload(
            payload.get("options_arbitrage", {}),
            full=is_status,
        ),
        "contract_strategies": _compact_contract_strategies_payload(
            payload.get("contract_strategies", {}),
            full=is_status,
        ),
        "execution_protection": _compact_execution_protection_payload(
            payload.get("execution_protection", {}),
            full=is_status,
        ),
        "order_activity": _compact_order_activity_payload(
            payload.get("order_activity", {}),
            full=is_records,
        ),
        "onchain": _compact_onchain_payload(
            payload.get("onchain", {}),
            full=is_status or is_records,
        ),
    }

    if is_status:
        result.update(
            {
                "markets": payload.get("markets", []),
                "quote_rates": payload.get("quote_rates", {}),
                "account_balances": _compact_account_balances_payload(
                    payload.get("account_balances", {}),
                    full=True,
                ),
                "derivatives": _compact_derivatives_payload(
                    payload.get("derivatives", {}),
                    full=True,
                ),
                "readiness": payload.get("readiness", {}),
                "runtime_store": payload.get("runtime_store", {}),
            }
        )
    elif is_settings:
        result["trading_console"] = payload.get("trading_console", {})
    elif is_records:
        result["trading_console"] = payload.get("trading_console", {})

    return result
