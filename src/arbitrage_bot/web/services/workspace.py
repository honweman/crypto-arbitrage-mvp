from __future__ import annotations

import sqlite3
from typing import Any


from ..users import (
    WebUser,
)


from ...config import (
    BotConfig,
)
from ...strategy_center import (
    FundingArbitrageSettings,
    SignalBotSettings,
    StrategyCenterStore,
    build_strategy_center_public_payload,
)
from ...user_paper_store import UserPaperTradingStore
from ...user_live_strategies import user_market_maker_instance_id
from ...user_strategies import (
    LIVE_USER_STRATEGY_TYPES,
    USER_STRATEGY_DEFINITIONS,
    UserStrategy,
)
from ...user_workspace import (
    UserWorkspaceStore,
)


from .exchange_data import (
    _account_balance_status,
    _aggregate_account_balance_totals,
    _number_or_none,
)
from ..paths import default_strategy_center_path


_OWNER_MM_PROBLEM_STATUSES = {
    "blocked",
    "blocked_by_risk",
    "cancel_retry",
    "error",
    "execution_error",
    "open_order_sync_error",
    "reconciliation_required",
    "sync_error",
}


def build_owner_market_maker_payload(
    workspace: dict[str, Any],
    market_maker_runtime: dict[str, Any],
) -> dict[str, Any]:
    """Build a request-owner-only MM summary from owner strategies and runtimes."""
    accounts_by_id = {
        str(row.get("id") or ""): row
        for row in workspace.get("accounts", [])
        if isinstance(row, dict) and row.get("id")
    }
    runtime_by_id = {
        str(row.get("id") or ""): row
        for row in market_maker_runtime.get("instances", [])
        if isinstance(row, dict) and row.get("id")
    }
    instances: list[dict[str, Any]] = []
    for strategy in workspace.get("strategies", []):
        if (
            not isinstance(strategy, dict)
            or strategy.get("strategy_type") != "market_maker"
        ):
            continue
        runtime_id = str(strategy.get("runtime_instance_id") or "")
        account = next(
            (
                accounts_by_id.get(str(account_id or ""))
                for account_id in strategy.get("account_ids", [])
                if accounts_by_id.get(str(account_id or "")) is not None
            ),
            {},
        )
        runtime = {
            **dict(
                runtime_by_id.get(runtime_id)
                or strategy.get("live_runtime")
                or {}
            ),
            "id": runtime_id,
        }
        desired_state = str(strategy.get("run_state") or "paused")
        if not strategy.get("enabled"):
            cancellation_pending = bool(
                runtime.get("open_order_count")
                or runtime.get("open_order_sync_error")
                or runtime.get("status")
                in {"cancel_retry", "remove_cancel_retry"}
            )
            runtime = {
                **runtime,
                "status": (
                    runtime.get("status") if cancellation_pending else desired_state
                ),
                "mode": "live",
                "reason": runtime.get("reason")
                or f"strategy is {desired_state}",
                "open_order_count": int(runtime.get("open_order_count") or 0),
            }
        elif not runtime.get("status"):
            runtime = {
                **runtime,
                "status": "starting",
                "mode": "live",
                "reason": "runtime is starting",
                "open_order_count": 0,
            }
        parameters = strategy.get("parameters") or {}
        risk = strategy.get("risk") or {}
        config = {
            "id": runtime_id,
            "enabled": bool(strategy.get("enabled")),
            "live_enabled": bool(strategy.get("enabled")),
            "exchange": str(account.get("exchange") or ""),
            "exchange_label": str(account.get("label") or ""),
            "symbol": str(account.get("symbol") or ""),
            **{
                key: parameters[key]
                for key in (
                    "levels",
                    "price_band_pct",
                    "quote_per_level",
                    "refresh_seconds",
                    "post_only",
                    "depth_shape",
                )
                if key in parameters
            },
            "max_order_quote": risk.get("max_order_quote"),
            "max_cycle_quote": risk.get("max_total_quote"),
            "max_open_orders": risk.get("max_open_orders"),
        }
        plan = (
            runtime.get("last_plan")
            if isinstance(runtime.get("last_plan"), dict)
            else None
        )
        instances.append(
            {
                "id": runtime_id,
                "owner_strategy_id": strategy.get("id"),
                "run_state": desired_state,
                "display_name": strategy.get("name"),
                "status": runtime.get("status") or "paused",
                "mode": "live",
                "config": config,
                "runtime": runtime,
                "plan": plan,
                "status_reason": runtime.get("status_reason")
                or runtime.get("reason"),
                "error": runtime.get("last_error"),
            }
        )

    priority = {
        "error": 0,
        "execution_error": 0,
        "sync_error": 0,
        "open_order_sync_error": 0,
        "reconciliation_required": 1,
        "blocked_by_risk": 2,
        "blocked": 2,
        "cancel_retry": 3,
        "starting": 4,
        "running": 5,
        "waiting": 6,
        "paused": 7,
        "stopped": 8,
        "disabled": 8,
    }
    selected = min(
        instances,
        key=lambda row: priority.get(str(row.get("status") or ""), 9),
        default={},
    )
    runtimes = [row["runtime"] for row in instances]
    problem_instances = [
        {
            "id": row.get("id"),
            "display_name": row.get("display_name"),
            "status": row.get("status"),
            "reason": row.get("status_reason") or row.get("error"),
        }
        for row in instances
        if row.get("status") in _OWNER_MM_PROBLEM_STATUSES
    ]
    selected_runtime = (
        selected.get("runtime")
        if isinstance(selected.get("runtime"), dict)
        else {}
    )
    aggregate_runtime = {
        "status": selected.get("status") or "disabled",
        "mode": "live",
        "instances": runtimes,
        "instance_count": len(instances),
        "active_instance_count": sum(
            row.get("status") not in {"paused", "disabled"} for row in instances
        ),
        "problem_instance_count": len(problem_instances),
        "problem_instances": problem_instances,
        "open_order_count": sum(
            int(row.get("open_order_count") or 0) for row in runtimes
        ),
        "placed_count": sum(int(row.get("placed_count") or 0) for row in runtimes),
        "canceled_count": sum(int(row.get("canceled_count") or 0) for row in runtimes),
        "status_reason": selected.get("status_reason"),
        "last_error": selected.get("error"),
        "last_plan": selected_runtime.get("last_plan"),
    }
    return {
        "status": selected.get("status") or "disabled",
        "mode": "live",
        "owner_scoped": True,
        "instances": instances,
        "instance_count": len(instances),
        "problem_instance_count": len(problem_instances),
        "runtime": aggregate_runtime,
        "plan": selected.get("plan"),
        "accounts": [
            {
                "id": row.get("id"),
                "label": row.get("label"),
                "exchange": row.get("exchange"),
                "symbol": row.get("symbol"),
            }
            for row in accounts_by_id.values()
        ],
        "error": selected.get("error"),
        "status_reason": selected.get("status_reason"),
    }

def build_user_workspace_payload(
    store: UserWorkspaceStore,
    *,
    user: WebUser | None,
    paper_store: UserPaperTradingStore | None = None,
) -> dict[str, Any]:
    strategy_access = {
        "scope": "owner",
        "platform_manage": bool(user is not None and user.role == "admin"),
        "core_trading": {
            "enabled": user is not None,
            "strategy_types": (
                ["auto_buy_sell", "market_maker"] if user is not None else []
            ),
            "live_strategy_types": (
                ["auto_buy_sell", "market_maker"] if user is not None else []
            ),
            "requires_live_confirmation": True,
        },
        "quant": {
            "enabled": user is not None,
            "mode": "mixed",
            "live_submit_allowed": bool(user is not None),
            "strategy_types": (
                list(USER_STRATEGY_DEFINITIONS) if user is not None else []
            ),
            "live_strategy_types": (
                sorted(LIVE_USER_STRATEGY_TYPES) if user is not None else []
            ),
            "paper_strategy_types": (
                [
                    strategy_type
                    for strategy_type in USER_STRATEGY_DEFINITIONS
                    if strategy_type not in LIVE_USER_STRATEGY_TYPES
                ]
                if user is not None
                else []
            ),
        },
    }
    empty_paper = {
        "status": "user_account_required" if user is None else "unavailable",
        "mode": "paper",
        "live_submit_allowed": False,
        "states": [],
        "events": [],
        "recent_fills": [],
        "counts": {},
        "summary": {
            "state_count": 0,
            "running_count": 0,
            "complete_count": 0,
            "blocked_count": 0,
            "fill_count": 0,
            "open_order_count": 0,
            "total_pnl_common": 0.0,
            "daily_pnl_common": 0.0,
            "common_quote_currency": "",
        },
    }
    if user is None:
        return {
            "status": "user_account_required",
            "projects": [],
            "accounts": [],
            "connections": [],
            "wallets": [],
            "venue_connections": [],
            "strategies": [],
            "exchange_catalog": [],
            "dex_venue_catalog": [],
            "strategy_catalog": [],
            "strategy_access": strategy_access,
            "paper": empty_paper,
            "vault_available": store.cipher.available,
            "summary": {
                "project_count": 0,
                "pending_project_count": 0,
                "ready_project_count": 0,
                "attention_project_count": 0,
                "setup_completed_steps": 0,
                "setup_total_steps": 0,
                "setup_progress_pct": 0.0,
                "next_project_id": "",
                "next_action": {
                    "code": "create_project",
                    "label": "Create your first trading project",
                },
                "account_count": 0,
                "connection_count": 0,
                "wallet_count": 0,
                "venue_connection_count": 0,
                "healthy_venue_connection_count": 0,
                "stale_venue_connection_count": 0,
                "error_venue_connection_count": 0,
                "configured_account_count": 0,
                "ready_account_count": 0,
                "strategy_count": 0,
                "enabled_strategy_count": 0,
                "ready_strategy_count": 0,
                "blocked_strategy_count": 0,
            },
        }
    try:
        payload = store.public_payload(
            owner_email=user.email,
            is_admin=False,
        )
        payload["strategy_access"] = strategy_access
        paper = (
            paper_store.public_payload(owner_email=user.email, is_admin=False)
            if paper_store is not None
            else empty_paper
        )
        paper_states = {
            str(row.get("strategy_id") or ""): row
            for row in paper.get("states", [])
            if isinstance(row, dict) and row.get("strategy_id")
        }
        paper_counts = paper.get("counts", {})
        for strategy in payload["strategies"]:
            if strategy.get("mode") == "live":
                strategy["runtime_instance_id"] = user_market_maker_instance_id(
                    UserStrategy.from_dict(strategy)
                )
                strategy["live_runtime"] = {
                    "status": (
                        "starting"
                        if strategy.get("enabled")
                        else strategy.get("run_state") or "paused"
                    ),
                    "mode": "live",
                    "reason": "waiting for the owner strategy runtime",
                    "open_order_count": 0,
                }
                continue
            strategy_id = str(strategy.get("id") or "")
            strategy["paper_counts"] = dict(paper_counts.get(strategy_id) or {})
            strategy["paper_runtime"] = paper_states.get(strategy_id) or {
                "strategy_id": strategy_id,
                "status": "waiting" if strategy.get("enabled") else "paused",
                "mode": "paper",
                "reason": (
                    "waiting for the paper strategy runtime"
                    if strategy.get("enabled")
                    else "paper strategy is paused"
                ),
                "open_order_count": 0,
            }
        payload["paper"] = paper
        payload["platform_projects"] = (
            [
                project
                for project in store.platform_projects()
                if project.get("owner_email") != user.email
            ]
            if user.role == "admin"
            else []
        )
        payload["summary"]["paper_running_count"] = int(
            (paper.get("summary") or {}).get("running_count") or 0
        )
        payload["summary"]["paper_fill_count"] = int(
            (paper.get("summary") or {}).get("fill_count") or 0
        )
        return payload
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {
            "status": "error",
            "error": str(exc),
            "projects": [],
            "accounts": [],
            "connections": [],
            "wallets": [],
            "venue_connections": [],
            "strategies": [],
            "exchange_catalog": [],
            "dex_venue_catalog": [],
            "strategy_catalog": [],
            "strategy_access": strategy_access,
            "paper": {**empty_paper, "status": "error"},
            "vault_available": store.cipher.available,
            "summary": {
                "project_count": 0,
                "pending_project_count": 0,
                "ready_project_count": 0,
                "attention_project_count": 0,
                "setup_completed_steps": 0,
                "setup_total_steps": 0,
                "setup_progress_pct": 0.0,
                "next_project_id": "",
                "next_action": {
                    "code": "create_project",
                    "label": "Create your first trading project",
                },
                "account_count": 0,
                "connection_count": 0,
                "wallet_count": 0,
                "venue_connection_count": 0,
                "healthy_venue_connection_count": 0,
                "stale_venue_connection_count": 0,
                "error_venue_connection_count": 0,
                "configured_account_count": 0,
                "ready_account_count": 0,
                "strategy_count": 0,
                "enabled_strategy_count": 0,
                "ready_strategy_count": 0,
                "blocked_strategy_count": 0,
            },
        }

def _merge_workspace_account_balances(
    account_balances: dict[str, Any] | None,
    workspace: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(account_balances or {})
    full_accounts_available = "accounts" in merged
    accounts = [
        dict(row)
        for row in merged.get("accounts", [])
        if isinstance(row, dict)
    ]

    def account_connection_id(row: dict[str, Any]) -> str:
        direct = str(
            row.get("workspace_connection_id")
            or row.get("credential_connection_id")
            or ""
        ).strip()
        if direct:
            return direct
        auth = row.get("auth") if isinstance(row.get("auth"), dict) else {}
        nested = str(
            auth.get("workspace_connection_id")
            or auth.get("credential_connection_id")
            or ""
        ).strip()
        if nested:
            return nested
        exchange_key = str(row.get("exchange") or "").strip()
        if not exchange_key.startswith("workspace:"):
            return ""
        connection_and_market = exchange_key.removeprefix("workspace:")
        connection_id, separator, market_type = connection_and_market.rpartition(":")
        if separator and connection_id and market_type in {"spot", "swap", "future"}:
            return connection_id
        return ""

    platform_keys = {
        str(row.get("exchange") or "").strip()
        for row in accounts
        if str(row.get("source") or "") != "user_workspace"
    }
    workspace_accounts: list[dict[str, Any]] = []
    for connection in (workspace or {}).get("connections", []) or []:
        if not isinstance(connection, dict):
            continue
        connection_id = str(connection.get("id") or "").strip()
        if not connection_id:
            continue
        connection_status = str(connection.get("status") or "unverified")
        checked_at = _number_or_none(connection.get("checked_at"))
        runtime_keys = {
            str(item or "").strip()
            for item in connection.get("runtime_keys", []) or []
            if str(item or "").strip()
        }
        linked_platform_keys = runtime_keys & platform_keys
        linked_connection_accounts = [
            row for row in accounts if account_connection_id(row) == connection_id
        ]
        has_linked_account = bool(linked_platform_keys or linked_connection_accounts)
        markets = [
            row
            for row in connection.get("markets", []) or []
            if isinstance(row, dict)
        ]
        symbols = sorted(
            {
                str(row.get("symbol") or "").strip()
                for row in markets
                if str(row.get("symbol") or "").strip()
            }
        )
        status = (
            "ok"
            if connection_status == "healthy"
            else "error"
            if connection_status == "error"
            else "warning"
        )
        balances = [
            dict(row)
            for row in connection.get("balances", []) or []
            if isinstance(row, dict) and row.get("currency")
        ]
        if has_linked_account and (
            connection_status != "healthy" or checked_at is None or not balances
        ):
            # Keep the live platform snapshot until the imported encrypted account
            # has completed its own private check. This prevents both double-counting
            # and a temporary blank balance during migration.
            continue
        if has_linked_account:
            accounts = [
                row
                for row in accounts
                if account_connection_id(row) != connection_id
                and str(row.get("exchange") or "").strip()
                not in linked_platform_keys
            ]
            platform_keys.difference_update(linked_platform_keys)
        open_order_count = max(
            0,
            int(_number_or_none(connection.get("open_order_count")) or 0),
        )
        workspace_accounts.append(
            {
                "exchange": connection_id,
                "label": str(
                    connection.get("label")
                    or connection.get("exchange")
                    or connection_id
                ),
                "id": str(connection.get("exchange") or ""),
                "market_type": str(connection.get("market_type") or "spot"),
                "symbols": symbols,
                "auth": {
                    "configured": bool(connection.get("credentials_configured")),
                    "private_checks_enabled": True,
                    "missing_env": [],
                    "storage": "encrypted",
                },
                "status": status,
                "warnings": (
                    []
                    if status == "ok"
                    else ["connection test is required or no longer fresh"]
                ),
                "errors": (
                    ["workspace account connection check failed"]
                    if status == "error"
                    else []
                ),
                "balance": {
                    "checked": checked_at is not None,
                    "skipped_reason": (
                        None if checked_at is not None else "connection not tested"
                    ),
                    "currencies": balances,
                    "open_order_reserves": {
                        "currencies": {},
                        "open_order_count": open_order_count,
                        "warnings": [],
                    },
                },
                "markets": [
                    {
                        "exchange": connection_id,
                        "symbol": str(row.get("symbol") or ""),
                        "status": (
                            "ok" if row.get("connection_status") == "healthy" else status
                        ),
                        "market": {
                            "found": bool(row.get("symbol")),
                            "symbol": str(row.get("symbol") or ""),
                        },
                        "error": None,
                    }
                    for row in markets
                ],
                "source": "user_workspace",
                "workspace_connection_id": connection_id,
                "runtime_keys": sorted(runtime_keys),
                "live_enabled": bool(connection.get("live_enabled")),
                "latency_ms": _number_or_none(connection.get("latency_ms")),
                "checked_at": checked_at,
            }
        )

    accounts.extend(workspace_accounts)
    workspace_errors = [
        str(error)
        for account in workspace_accounts
        for error in account.get("errors", []) or []
    ]
    workspace_warnings = [
        str(warning)
        for account in workspace_accounts
        for warning in account.get("warnings", []) or []
    ]
    errors = (
        [
            str(error)
            for account in accounts
            for error in account.get("errors", []) or []
        ]
        if full_accounts_available
        else [*[str(item) for item in merged.get("errors", []) or []], *workspace_errors]
    )
    warnings = (
        [
            str(warning)
            for account in accounts
            for warning in account.get("warnings", []) or []
        ]
        if full_accounts_available
        else [
            *[str(item) for item in merged.get("warnings", []) or []],
            *workspace_warnings,
        ]
    )
    if full_accounts_available:
        totals = _aggregate_account_balance_totals(accounts)
        checked_account_count = sum(
            1 for account in accounts if account.get("balance", {}).get("checked")
        )
        total_account_count = len(accounts)
        status = _account_balance_status(accounts)
    else:
        totals_by_currency: dict[str, dict[str, Any]] = {}
        for row in [
            *(merged.get("totals", []) or []),
            *_aggregate_account_balance_totals(workspace_accounts),
        ]:
            if not isinstance(row, dict):
                continue
            currency = str(row.get("currency") or "").upper()
            if not currency:
                continue
            target = totals_by_currency.setdefault(
                currency,
                {
                    "currency": currency,
                    "free": 0.0,
                    "used": 0.0,
                    "total": 0.0,
                    "open_order_reserved": 0.0,
                },
            )
            for field in ("free", "used", "total", "open_order_reserved"):
                value = _number_or_none(row.get(field))
                if value is not None:
                    target[field] += value
        preferred = {"ACS": 0, "USDC": 1, "USDT": 2, "USD": 3, "KRW": 4}
        totals = sorted(
            totals_by_currency.values(),
            key=lambda row: (preferred.get(row["currency"], 99), row["currency"]),
        )
        checked_account_count = int(merged.get("checked_account_count") or 0) + sum(
            1
            for account in workspace_accounts
            if account.get("balance", {}).get("checked")
        )
        total_account_count = int(merged.get("total_account_count") or 0) + len(
            workspace_accounts
        )
        base_status = str(merged.get("status") or "")
        workspace_status = (
            _account_balance_status(workspace_accounts) if workspace_accounts else ""
        )
        status = (
            "error"
            if "error" in {base_status, workspace_status}
            else "warning"
            if "warning" in {base_status, workspace_status}
            else base_status or workspace_status
        )
    last_finished = max(
        [
            float(value)
            for value in [
                _number_or_none(merged.get("last_finished")),
                *[row.get("checked_at") for row in workspace_accounts],
            ]
            if value is not None
        ]
        or [0.0]
    )
    return {
        **merged,
        "status": status,
        "accounts": accounts,
        "totals": totals,
        "checked_account_count": checked_account_count,
        "total_account_count": total_account_count,
        "last_finished": last_finished or None,
        "errors": errors,
        "warnings": warnings,
    }

def _sync_portfolio_with_account_balances(
    portfolio: dict[str, Any] | None,
    account_balances: dict[str, Any] | None,
    *,
    quote_rates: dict[str, float] | None = None,
) -> dict[str, Any]:
    payload = dict(portfolio or {})
    totals = {
        str(row.get("currency") or "").upper(): float(row.get("total") or 0.0)
        for row in (account_balances or {}).get("totals", []) or []
        if isinstance(row, dict) and row.get("currency")
    }
    if not totals or not payload:
        return payload

    positions = [
        dict(row)
        for row in payload.get("positions", []) or []
        if isinstance(row, dict) and row.get("asset")
    ]
    if not positions and payload.get("asset"):
        positions = [
            {
                "asset": str(payload.get("asset") or "").upper(),
                "position_base": payload.get("position_base", 0.0),
                "average_entry_price": payload.get("average_entry_price", 0.0),
                "mark_price": payload.get("mark_price"),
                "position_value": payload.get("position_value"),
            }
        ]

    accounts = [
        row
        for row in (account_balances or {}).get("accounts", []) or []
        if isinstance(row, dict)
    ]
    rates = {str(key).upper(): float(value) for key, value in (quote_rates or {}).items()}
    quote_currency = str(payload.get("quote_currency") or "USD").upper()
    rates.setdefault(quote_currency, 1.0)
    for stable_currency in (
        "USD",
        "USDT",
        "USDC",
        "FDUSD",
        "BUSD",
        "TUSD",
        "USDP",
        "DAI",
        "PYUSD",
        "USDE",
        "USDS",
        "USD1",
        "RLUSD",
        "GUSD",
        "FRAX",
        "LUSD",
    ):
        rates.setdefault(stable_currency, 1.0)
    dynamic_value_sums: dict[str, float] = {}
    dynamic_amount_sums: dict[str, float] = {}
    for account in accounts:
        for row in account.get("balance", {}).get("currencies", []) or []:
            if not isinstance(row, dict):
                continue
            currency = str(row.get("currency") or "").upper()
            valuation_quote = str(row.get("valuation_quote") or "").upper()
            amount = abs(_number_or_none(row.get("total")) or 0.0)
            valuation_price = _number_or_none(row.get("valuation_price"))
            quote_rate = rates.get(valuation_quote)
            if (
                currency
                and amount > 0
                and valuation_price is not None
                and valuation_price > 0
                and quote_rate is not None
                and quote_rate > 0
            ):
                dynamic_value_sums[currency] = dynamic_value_sums.get(currency, 0.0) + (
                    amount * valuation_price * quote_rate
                )
                dynamic_amount_sums[currency] = (
                    dynamic_amount_sums.get(currency, 0.0) + amount
                )
    for currency, value_sum in dynamic_value_sums.items():
        amount_sum = dynamic_amount_sums[currency]
        if amount_sum > 0:
            rates.setdefault(currency, value_sum / amount_sum)
    for account in accounts:
        for row in account.get("balance", {}).get("currencies", []) or []:
            if not isinstance(row, dict):
                continue
            currency = str(row.get("currency") or "").upper()
            rate = rates.get(currency)
            amount = _number_or_none(row.get("total"))
            if rate is not None and amount is not None:
                row["price_common"] = rate
                row["value_common"] = amount * rate

    position_assets: set[str] = set()
    position_values: list[float] = []
    position_missing_rates: list[str] = []
    for position in positions:
        asset = str(position.get("asset") or "").upper()
        if not asset:
            continue
        position_assets.add(asset)
        position_base = totals.get(asset, float(position.get("position_base") or 0.0))
        mark_price = _number_or_none(position.get("mark_price")) or rates.get(asset)
        position["position_base"] = position_base
        position["position_value"] = (
            position_base * mark_price if mark_price is not None else None
        )
        if position["position_value"] is not None:
            position_values.append(float(position["position_value"]))
        elif position_base != 0.0:
            position_missing_rates.append(asset)
        breakdown: list[dict[str, Any]] = []
        for account in accounts:
            for row in account.get("balance", {}).get("currencies", []) or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("currency") or "").upper() != asset:
                    continue
                amount = _number_or_none(row.get("total")) or 0.0
                if amount == 0.0:
                    continue
                breakdown.append(
                    {
                        "account": str(
                            account.get("label") or account.get("exchange") or ""
                        ),
                        "exchange": str(account.get("id") or ""),
                        "wallet": str(row.get("wallet") or "trading"),
                        "amount": amount,
                        "tradable": bool(row.get("tradable", True)),
                    }
                )
        if breakdown:
            position["account_breakdown"] = breakdown

    payload["positions"] = positions
    if positions:
        payload["asset"] = positions[0]["asset"]
        payload["position_base"] = positions[0]["position_base"]
        payload["mark_price"] = positions[0].get("mark_price")
    payload["position_value"] = sum(position_values) if position_values else None

    cash_balances = {
        currency: amount
        for currency, amount in sorted(totals.items())
        if currency not in position_assets
    }
    old_cash = payload.get("cash_balances", {}) or {}
    old_common = payload.get("cash_balances_common", {}) or {}
    for currency, amount in old_cash.items():
        numeric_amount = _number_or_none(amount)
        common_amount = _number_or_none(old_common.get(currency))
        if numeric_amount not in {None, 0.0} and common_amount is not None:
            rates.setdefault(str(currency).upper(), common_amount / numeric_amount)
    cash_common: dict[str, float] = {}
    cash_missing: list[str] = []
    for currency, amount in cash_balances.items():
        rate = rates.get(currency)
        if rate is None:
            cash_missing.append(currency)
        else:
            cash_common[currency] = amount * rate
    payload["cash_balances"] = cash_balances
    payload["cash_balances_common"] = cash_common
    payload["cash_value"] = sum(cash_common.values())
    payload["cash_missing_rates"] = cash_missing
    total_asset_missing_rates = sorted(
        set([*position_missing_rates, *cash_missing])
    )
    valued_component_count = len(position_values) + len(cash_common)
    payload["total_asset_value"] = (
        sum(position_values) + sum(cash_common.values())
        if valued_component_count > 0
        else None
    )
    payload["total_asset_currency"] = quote_currency
    payload["total_asset_missing_rates"] = total_asset_missing_rates
    payload["balance_source"] = "merged_live_accounts"
    payload["balance_status"] = (account_balances or {}).get("status")
    payload["balance_observed_at"] = (account_balances or {}).get("last_finished")
    return payload

def build_strategy_center_payload(
    cfg: BotConfig,
    store: StrategyCenterStore | None = None,
    *,
    user: WebUser | None = None,
) -> dict[str, Any]:
    if not cfg.strategy_center.enabled:
        return {
            "status": "disabled",
            "updated_at": None,
            "strategy_instances": [],
            "user_api_accounts": [],
            "funding_arbitrage": FundingArbitrageSettings().to_dict(),
            "signal_bot": SignalBotSettings().to_dict(),
            "signals": [],
            "summary": {
                "strategy_count": 0,
                "enabled_count": 0,
                "live_count": 0,
                "api_account_count": 0,
                "recent_signal_count": 0,
                "pnl_quote": 0.0,
                "open_order_count": 0,
            },
            "path": default_strategy_center_path(cfg),
        }
    active_store = store or StrategyCenterStore(
        default_strategy_center_path(cfg),
        max_recent_signals=cfg.strategy_center.max_recent_signals,
    )
    try:
        payload = active_store.read()
        result = build_strategy_center_public_payload(
            payload,
            current_user_email=user.email if user else "",
            current_user_role=user.role if user else "admin",
            allowed_assets=user.allowed_assets if user else [],
        )
        result["path"] = str(active_store.path)
        return result
    except ValueError as exc:
        return {
            "status": "error",
            "updated_at": None,
            "strategy_instances": [],
            "user_api_accounts": [],
            "funding_arbitrage": FundingArbitrageSettings().to_dict(),
            "signal_bot": SignalBotSettings().to_dict(),
            "signals": [],
            "summary": {
                "strategy_count": 0,
                "enabled_count": 0,
                "live_count": 0,
                "api_account_count": 0,
                "recent_signal_count": 0,
                "pnl_quote": 0.0,
                "open_order_count": 0,
            },
            "path": str(active_store.path),
            "error": str(exc),
        }
