from __future__ import annotations

import time
from typing import Any




from ...asset_ledger import AssetLedgerStore
from ...config import (
    BotConfig,
)


from .shared import (
    _all_account_exchanges,
    _market_maker_fill_source,
    _risk_account_enabled,
)

def _owner_live_market_maker_order_activity(
    cfg: BotConfig,
    workspace_payload: dict[str, Any],
) -> dict[str, Any]:
    strategies = [
        row
        for row in workspace_payload.get("strategies", [])
        if isinstance(row, dict)
        and row.get("mode") == "live"
        and row.get("strategy_type") == "market_maker"
    ]
    sources = [
        _market_maker_fill_source(str(row.get("runtime_instance_id") or ""))
        for row in strategies
        if row.get("runtime_instance_id")
    ]
    fills = AssetLedgerStore(cfg.asset_ledger).recent_fills(
        observation_sources=sources,
        limit=100,
    )
    open_orders: list[dict[str, Any]] = []
    for strategy in strategies:
        runtime = (
            strategy.get("live_runtime")
            if isinstance(strategy.get("live_runtime"), dict)
            else {}
        )
        runtime_config = (
            runtime.get("config") if isinstance(runtime.get("config"), dict) else {}
        )
        runtime_orders = {
            str(row.get("id") or ""): row
            for row in runtime.get("open_orders", []) or []
            if isinstance(row, dict) and row.get("id")
        }
        order_ids = [
            str(order_id)
            for order_id in runtime.get("open_order_ids", []) or []
            if order_id
        ]
        if runtime.get("open_order_details_complete") is True:
            order_ids = [order_id for order_id in order_ids if order_id in runtime_orders]
        for order_id in order_ids:
            if not order_id:
                continue
            detail = runtime_orders.get(order_id, {})
            timestamp = detail.get("timestamp")
            if timestamp is None:
                updated_at = runtime.get("updated_at")
                timestamp = float(updated_at) * 1000.0 if updated_at else None
            open_orders.append(
                {
                    "exchange": detail.get("exchange")
                    or runtime_config.get("exchange")
                    or "",
                    "label": detail.get("label")
                    or strategy.get("name")
                    or "Market Maker",
                    "id": str(order_id),
                    "client_order_id": detail.get("client_order_id") or "",
                    "symbol": detail.get("symbol")
                    or runtime_config.get("symbol")
                    or "",
                    "side": detail.get("side") or "",
                    "type": detail.get("type") or "limit",
                    "status": detail.get("status") or "open",
                    "source": "market_maker",
                    "strategy_instance_id": strategy.get("runtime_instance_id") or "",
                    "price": detail.get("price"),
                    "average": detail.get("average"),
                    "amount": detail.get("amount"),
                    "filled": detail.get("filled"),
                    "remaining": detail.get("remaining"),
                    "cost": detail.get("cost"),
                    "fee": detail.get("fee"),
                    "timestamp": timestamp,
                    "datetime": detail.get("datetime"),
                }
            )
    account_rows: dict[str, dict[str, Any]] = {}
    for row in [*open_orders, *fills]:
        exchange = str(row.get("exchange") or row.get("account_key") or "")
        account = account_rows.setdefault(
            exchange,
            {
                "exchange": exchange,
                "label": row.get("label") or exchange,
                "status": "ok",
                "warnings": [],
                "errors": [],
                "symbols": [],
                "open_orders": [],
                "closed_orders": [],
                "recent_trades": [],
            },
        )
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in account["symbols"]:
            account["symbols"].append(symbol)
        target = "open_orders" if row in open_orders else "recent_trades"
        account[target].append(row)
    for account in account_rows.values():
        account["open_order_count"] = len(account["open_orders"])
        account["closed_order_count"] = 0
        account["recent_trade_count"] = len(account["recent_trades"])
    latest_timestamp_ms = max(
        (float(row.get("timestamp") or 0.0) for row in fills),
        default=0.0,
    )
    return {
        "status": "ok" if cfg.asset_ledger.enabled else "disabled",
        "accounts": list(account_rows.values()),
        "open_orders": open_orders,
        "closed_orders": [],
        "recent_trades": fills,
        "pnl_summary": {
            "currency": cfg.common_quote_currency,
            "window": "recent_fills",
        },
        "daily_pnl": {
            "currency": cfg.common_quote_currency,
            "trade_count": len(fills),
            "total_realized_pnl": 0.0,
            "total_fees": 0.0,
            "total_notional": 0.0,
            "sources": {},
        },
        "open_order_count": len(open_orders),
        "closed_order_count": 0,
        "recent_trade_count": len(fills),
        "checked_account_count": len(account_rows),
        "total_account_count": len(strategies),
        "last_finished": latest_timestamp_ms / 1000.0 if latest_timestamp_ms else None,
        "errors": [],
        "warnings": [],
        "owner_scoped": True,
        "reconciliation": {
            "status": "ok",
            "issue_count": 0,
            "notice_count": 0,
            "total_item_count": 0,
            "level_counts": {"error": 0, "warning": 0, "info": 0},
            "issues": [],
        },
    }

def _owner_live_trading_console(
    cfg: BotConfig,
    workspace_payload: dict[str, Any],
    order_activity: dict[str, Any],
    auto_buy_sell_payload: dict[str, Any],
) -> dict[str, Any]:
    open_counts: dict[str, int] = {}
    for order in order_activity.get("open_orders", []) or []:
        exchange_key = str(order.get("exchange") or "")
        if exchange_key:
            open_counts[exchange_key] = open_counts.get(exchange_key, 0) + 1

    exchanges = _all_account_exchanges(cfg)
    exchange_labels = {
        exchange.key: exchange.display_label or exchange.label or exchange.key
        for exchange in exchanges
    }
    accounts = [
        {
            "key": exchange.key,
            "label": exchange_labels[exchange.key],
            "id": exchange.id,
            "market_type": exchange.market_type,
            "enabled": _risk_account_enabled(cfg, exchange.key),
            "open_order_count": open_counts.get(exchange.key, 0),
        }
        for exchange in exchanges
    ]

    strategies: list[dict[str, Any]] = []
    for strategy in workspace_payload.get("strategies", []) or []:
        if (
            not isinstance(strategy, dict)
            or strategy.get("strategy_type") != "market_maker"
        ):
            continue
        runtime = (
            strategy.get("live_runtime")
            if isinstance(strategy.get("live_runtime"), dict)
            else {}
        )
        runtime_config = (
            runtime.get("config") if isinstance(runtime.get("config"), dict) else {}
        )
        strategy_accounts = [
            row for row in strategy.get("accounts", []) or [] if isinstance(row, dict)
        ]
        first_account = strategy_accounts[0] if strategy_accounts else {}
        exchange_key = str(
            runtime_config.get("exchange") or first_account.get("exchange") or ""
        )
        symbol = str(
            runtime_config.get("symbol") or first_account.get("symbol") or ""
        )
        enabled = bool(strategy.get("enabled"))
        runtime_mode = str(runtime.get("mode") or "live")
        runtime_status = str(
            runtime.get("status") or ("starting" if enabled else "paused")
        )
        strategies.append(
            {
                "id": f"owner_market_maker:{strategy.get('id') or ''}",
                "owner_strategy_id": str(strategy.get("id") or ""),
                "label": strategy.get("name") or "Market Maker",
                "configured": True,
                "exchange": exchange_key,
                "exchange_label": exchange_labels.get(
                    exchange_key,
                    first_account.get("label") or exchange_key,
                ),
                "symbol": symbol,
                "paused": not enabled,
                "live": enabled and runtime_mode == "live",
                "mode": "paused" if not enabled else runtime_mode,
                "status": runtime_status,
                "strategy_allowed": bool(strategy.get("effective_enabled")),
                "account_enabled": _risk_account_enabled(cfg, exchange_key),
                "live_ready": runtime_status not in {"blocked", "error"},
            }
        )

    terminal_statuses = {
        "complete",
        "stopped",
        "below_min_order_quote",
    }
    task_snapshot = auto_buy_sell_payload.get("tasks")
    task_rows = (
        task_snapshot.get("tasks", []) if isinstance(task_snapshot, dict) else []
    )
    for task in task_rows:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "")
        if status in terminal_statuses:
            continue
        config = task.get("config") if isinstance(task.get("config"), dict) else {}
        exchange_key = str(config.get("exchange") or "")
        strategies.append(
            {
                "id": f"owner_auto_buy_sell:{task.get('id') or ''}",
                "owner_auto_task_id": str(task.get("id") or ""),
                "label": "Auto Buy/Sell",
                "configured": True,
                "exchange": exchange_key,
                "exchange_label": exchange_labels.get(exchange_key, exchange_key),
                "symbol": str(config.get("symbol") or ""),
                "paused": status == "paused",
                "live": status != "paused",
                "mode": "paused" if status == "paused" else "live",
                "status": status or "running",
                "strategy_allowed": cfg.risk.allow_slow_execution,
                "account_enabled": _risk_account_enabled(cfg, exchange_key),
                "live_ready": status not in {"blocked_by_risk", "error"},
            }
        )

    live_base = (
        cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading
    )
    return {
        "status": "ok",
        "owner_scoped": True,
        "cancel_allowed": True,
        "cancel_scope": "owner",
        "live_trading": live_base,
        "strategies": strategies,
        "accounts": accounts,
        "open_order_count": int(order_activity.get("open_order_count") or 0),
        "recent_trade_count": int(order_activity.get("recent_trade_count") or 0),
        "updated_at": time.time(),
    }
