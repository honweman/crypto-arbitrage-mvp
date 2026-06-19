from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import ipaddress
import json
import os
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from aiohttp import web

from .render_payloads import STATE_VIEW_IDS, state_payload_for_view

from ..account_check import _auth_env_status, _balance_currencies, _summarize_balance
from ..alerts import AlertService
from ..auto_buy_sell_task import (
    AutoBuySellTaskService,
    default_task_store_path,
    validate_task_config,
)
from ..config import (
    BotConfig,
    CashAndCarryPair,
    ExchangeConfig,
    MarketMakerConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
    load_config,
)
from ..exchanges import ExchangeManager, limit_order_features
from ..fill_store import load_daily_pnl_summary, persist_fill_pnl
from ..main import (
    StrategyName,
    _quote_rates_from_sources,
    _symbols_for_configured_spot_markets,
    scan_with_manager,
)
from ..market_making import MarketMakerPlan, build_symmetric_market_maker_plan
from ..market_maker import (
    cancel_order_ids as cancel_market_maker_order_ids,
    market_maker_quote_conversion,
    order_book_market_data,
    run_cycle as run_market_maker_cycle,
)
from ..models import OrderBookSnapshot, Opportunity
from ..orderbook_cache import OrderBookCache
from ..order_reconciliation import (
    RECONCILIATION_AUTO_STOP_WARMUP_SECONDS,
    _monitor_auto_stop_decision,
    _monitor_reconciliation_warmup_active,
    build_order_reconciliation_payload,
)
from ..pnl import build_portfolio_pnl
from ..portfolio_metrics import (
    _base_currency_from_symbol,
    _portfolio_position_for_symbol,
    _trade_attribution,
    build_market_maker_quality_payload,
    build_order_attribution_map,
    build_synced_portfolio_pnl,
    enrich_recent_trades_with_pnl,
)
from ..risk import (
    RiskMarketContext,
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)
from ..slow_execution import build_slow_execution_plan
from ..solana import (
    SolanaTokenClient,
    fetch_top_token_owners,
    load_cached_holder_snapshot,
    update_holder_history,
)
from ..spot_arbitrage_executor import run_spot_arbitrage_execution_cycle
from ..strategy_timeline import (
    read_recent_strategy_timeline_entries,
    strategy_timeline_event_from_payload,
    strategy_timeline_fingerprint,
    summarize_strategy_timeline_entries,
    write_strategy_timeline_from_payload,
)
from ..strategies.spot_spread import find_converted_spot_spread_opportunities
from ..trade_log import (
    read_recent_trade_entries,
    summarize_trade_entries,
    write_trade_event,
)
from ..web_config import (
    _cash_and_carry_pairs_from_payload,
    _market_maker_overrides_from_payload,
    _market_maker_symbols_by_exchange,
    _risk_overrides_from_payload,
    _slow_execution_overrides_from_payload,
    _spot_markets_from_payload,
    _spot_symbols_by_exchange,
    cash_and_carry_pairs_to_list,
    exchange_configs_to_list,
    market_maker_config_to_dict,
    risk_config_to_dict,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_markets_to_list,
)


ACCOUNT_BALANCE_POLL_SECONDS = 10.0
ORDER_ACTIVITY_POLL_SECONDS = 5.0
ORDER_ACTIVITY_LIMIT = 20
SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS = 10.0
STRATEGY_IDS = {
    "market_maker",
    "slow_execution",
    "spot_spread",
    "cash_and_carry",
}
SESSION_COOKIE = "crypto_arb_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Arbitrage Login</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f5f6f2;
      color: #17211b;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    form {
      width: min(360px, calc(100% - 32px));
      display: grid;
      gap: 12px;
      padding: 22px;
      border: 1px solid #d8ded8;
      border-radius: 8px;
      background: #ffffff;
    }
    h1 { margin: 0 0 4px; font-size: 20px; }
    label { color: #66736b; font-size: 12px; font-weight: 700; text-transform: uppercase; }
    input {
      width: 100%;
      min-height: 40px;
      padding: 8px 10px;
      border: 1px solid #d8ded8;
      border-radius: 6px;
      font: inherit;
      box-sizing: border-box;
    }
    button {
      min-height: 40px;
      border: 1px solid #101828;
      border-radius: 6px;
      background: #101828;
      color: #ffffff;
      font-weight: 700;
      cursor: pointer;
    }
    .error { min-height: 18px; color: #b33b2e; font-size: 13px; }
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Crypto Arbitrage</h1>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Sign In</button>
    <div class="error">__ERROR__</div>
  </form>
</body>
</html>
"""


WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _read_web_asset(path: Path) -> str:
    return path.read_text(encoding="utf-8")


HTML = _read_web_asset(TEMPLATE_DIR / "index.html")
APP_JS = _read_web_asset(STATIC_DIR / "app.js")
STYLES_CSS = _read_web_asset(STATIC_DIR / "styles.css")



def _top_level(book: OrderBookSnapshot | None, side: str) -> tuple[float | None, float | None]:
    if book is None:
        return (None, None)
    levels = book.bids if side == "bid" else book.asks
    if not levels:
        return (None, None)
    return (levels[0].price, levels[0].amount)


def build_market_rows(
    markets: Iterable[SpotMarketConfig],
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market in markets:
        book = books.get((market.exchange, market.symbol))
        rate = quote_rates.get(market.quote_currency)
        bid, bid_size = _top_level(book, "bid")
        ask, ask_size = _top_level(book, "ask")
        rows.append(
            {
                "asset": market.asset,
                "exchange": market.exchange,
                "symbol": market.symbol,
                "quote_currency": market.quote_currency,
                "status": "ok" if book is not None and rate is not None else "missing",
                "bid": bid,
                "ask": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "bid_common": bid * rate if bid is not None and rate is not None else None,
                "ask_common": ask * rate if ask is not None and rate is not None else None,
                "timestamp_ms": book.timestamp_ms if book is not None else None,
            }
        )
    return rows


def _compact_trade_log_entry(entry: Any) -> dict[str, Any]:
    row = entry.to_dict()
    return {
        "event_id": row.get("event_id", ""),
        "logged_at": row.get("logged_at"),
        "event_type": row.get("event_type", ""),
        "strategy": row.get("strategy", ""),
        "mode": row.get("mode", ""),
        "status": row.get("status", ""),
        "exchange": row.get("exchange", ""),
        "symbol": row.get("symbol", ""),
        "side": row.get("side", ""),
        "order_count": row.get("order_count", 0),
        "total_quote_notional": row.get("total_quote_notional", 0.0),
        "placed_count": row.get("placed_count", 0),
        "canceled_count": row.get("canceled_count", 0),
        "risk_level": row.get("risk_level", ""),
        "risk_approved": row.get("risk_approved"),
        "reason": row.get("reason", ""),
    }


def _compact_strategy_timeline_entry(entry: Any) -> dict[str, Any]:
    row = entry.to_dict()
    return {
        "event_id": row.get("event_id", ""),
        "logged_at": row.get("logged_at"),
        "strategy": row.get("strategy", ""),
        "mode": row.get("mode", ""),
        "status": row.get("status", ""),
        "action": row.get("action", ""),
        "event_type": row.get("event_type", ""),
        "accounts": row.get("accounts", []),
        "symbols": row.get("symbols", []),
        "reason": row.get("reason", ""),
        "reasons": row.get("reasons", []),
        "warnings": row.get("warnings", []),
        "risk_triggers": row.get("risk_triggers", []),
        "metrics": row.get("metrics", {}),
        "source": row.get("source", ""),
    }


def build_operations_payload(cfg: BotConfig) -> dict[str, Any]:
    try:
        recent_entries = read_recent_trade_entries(cfg.trade_log)
        trade_log_error = None
    except OSError as exc:
        recent_entries = []
        trade_log_error = str(exc)
    compact_entries = [
        _compact_trade_log_entry(entry) for entry in recent_entries
    ]
    trade_log_payload = asdict(cfg.trade_log)
    trade_log_payload["recent_entries"] = compact_entries
    trade_log_payload["recent_events"] = compact_entries
    trade_log_payload["summary"] = summarize_trade_entries(recent_entries)
    trade_log_payload["error"] = trade_log_error
    try:
        timeline_entries = read_recent_strategy_timeline_entries(
            cfg.strategy_timeline
        )
        timeline_error = None
    except OSError as exc:
        timeline_entries = []
        timeline_error = str(exc)
    compact_timeline_entries = [
        _compact_strategy_timeline_entry(entry) for entry in timeline_entries
    ]
    timeline_payload = asdict(cfg.strategy_timeline)
    timeline_payload["recent_entries"] = compact_timeline_entries
    timeline_payload["recent_events"] = compact_timeline_entries
    timeline_payload["summary"] = summarize_strategy_timeline_entries(
        timeline_entries
    )
    timeline_payload["error"] = timeline_error
    audit_path = default_web_audit_path(cfg)
    try:
        audit_events = read_recent_web_audit_events(cfg)
        audit_error = None
    except OSError as exc:
        audit_events = []
        audit_error = str(exc)
    try:
        daily_pnl = load_daily_pnl_summary(
            cfg.pnl_store,
            currency=cfg.common_quote_currency,
        )
        pnl_error = None
    except Exception as exc:  # noqa: BLE001
        daily_pnl = {
            "enabled": cfg.pnl_store.enabled,
            "path": cfg.pnl_store.path,
            "day": None,
            "currency": cfg.common_quote_currency,
            "trade_count": 0,
            "total_realized_pnl": 0.0,
            "sources": {},
        }
        pnl_error = str(exc)
    daily_pnl["error"] = pnl_error
    return {
        "risk": asdict(cfg.risk),
        "alerts": asdict(cfg.alerts),
        "trade_log": trade_log_payload,
        "strategy_timeline": timeline_payload,
        "web_audit": {
            "enabled": True,
            "path": audit_path,
            "recent_events": audit_events,
            "event_count": len(audit_events),
            "error": audit_error,
        },
        "daily_pnl": daily_pnl,
    }


def _risk_strategy_enabled(cfg: BotConfig, strategy_id: str) -> bool:
    return cfg.risk.strategy_enabled.get(strategy_id, True)


def _risk_account_enabled(cfg: BotConfig, exchange_key: str) -> bool:
    return cfg.risk.account_enabled.get(exchange_key, True)


def build_trading_console_payload(
    cfg: BotConfig,
    exec_cfg: SlowExecutionConfig | None = None,
    *,
    strategy_paused: dict[str, bool] | None = None,
    order_activity: dict[str, Any] | None = None,
    auto_buy_sell_tasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    strategy_paused = strategy_paused or {}
    open_orders = (order_activity or {}).get("open_orders", [])
    open_counts: dict[str, int] = {}
    for order in open_orders:
        exchange = str(order.get("exchange") or "")
        if exchange:
            open_counts[exchange] = open_counts.get(exchange, 0) + 1

    live_base = cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading
    accounts = [
        {
            "key": exchange.key,
            "label": exchange.label or exchange.key,
            "id": exchange.id,
            "market_type": exchange.market_type,
            "enabled": _risk_account_enabled(cfg, exchange.key),
            "open_order_count": open_counts.get(exchange.key, 0),
        }
        for exchange in _all_account_exchanges(cfg)
    ]

    def strategy_row(
        *,
        strategy_id: str,
        label: str,
        configured: bool,
        exchange: str,
        symbol: str,
        strategy_allowed: bool,
        live_ready: bool = True,
        mode: str = "dry_run",
    ) -> dict[str, Any]:
        paused = bool(strategy_paused.get(strategy_id, False))
        account_enabled = not exchange or _risk_account_enabled(cfg, exchange)
        live = (
            live_base
            and configured
            and strategy_allowed
            and live_ready
            and account_enabled
            and not paused
        )
        return {
            "id": strategy_id,
            "label": label,
            "configured": configured,
            "exchange": exchange,
            "symbol": symbol,
            "paused": paused,
            "live": live,
            "mode": "paused" if paused else ("live" if live else mode),
            "strategy_allowed": strategy_allowed,
            "account_enabled": account_enabled,
            "live_ready": live_ready,
        }

    auto_tasks = [
        task
        for task in (auto_buy_sell_tasks or {}).get("tasks", [])
        if task.get("status")
        not in {"complete", "stopped_by_price", "below_min_order_quote"}
    ]
    first_auto_task = auto_tasks[0] if auto_tasks else {}
    first_auto_config = (
        first_auto_task.get("config")
        if isinstance(first_auto_task.get("config"), dict)
        else {}
    )
    slow_exchange = str(first_auto_config.get("exchange") or exec_cfg.exchange)
    slow_symbol = str(first_auto_config.get("symbol") or exec_cfg.symbol)
    if len(auto_tasks) > 1:
        slow_symbol = f"{len(auto_tasks)} tasks"

    strategies = [
        strategy_row(
            strategy_id="market_maker",
            label="Market Maker",
            configured=cfg.market_maker.enabled,
            exchange=cfg.market_maker.exchange,
            symbol=cfg.market_maker.symbol,
            strategy_allowed=cfg.risk.allow_market_maker
            and _risk_strategy_enabled(cfg, "market_maker"),
            live_ready=cfg.market_maker.live_enabled,
        ),
        strategy_row(
            strategy_id="slow_execution",
            label="Auto Buy/Sell",
            configured=exec_cfg.enabled or bool(auto_tasks),
            exchange=slow_exchange,
            symbol=slow_symbol,
            strategy_allowed=cfg.risk.allow_slow_execution
            and _risk_strategy_enabled(cfg, "slow_execution"),
        ),
        strategy_row(
            strategy_id="spot_spread",
            label="Spot Arbitrage",
            configured=bool(cfg.spot_markets),
            exchange="",
            symbol=",".join(sorted({market.asset for market in cfg.spot_markets})),
            strategy_allowed=_risk_strategy_enabled(cfg, "spot_spread"),
            mode="scan",
        ),
        strategy_row(
            strategy_id="cash_and_carry",
            label="Cash & Carry",
            configured=bool(cfg.cash_and_carry_pairs and cfg.derivative_exchanges),
            exchange="",
            symbol=",".join(
                sorted({pair.spot_symbol for pair in cfg.cash_and_carry_pairs})
            ),
            strategy_allowed=_risk_strategy_enabled(cfg, "cash_and_carry"),
            mode="scan",
        ),
    ]
    return {
        "live_trading": live_base,
        "strategies": strategies,
        "accounts": accounts,
        "open_order_count": len(open_orders),
        "recent_trade_count": (order_activity or {}).get("recent_trade_count", 0),
        "updated_at": time.time(),
    }


def _exchange_balance_symbols(
    cfg: BotConfig,
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for market in cfg.spot_markets:
        symbols.setdefault(market.exchange, set()).add(market.symbol)

    for pair in cfg.cash_and_carry_pairs:
        for exchange in cfg.spot_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.spot_symbol)
        for exchange in cfg.derivative_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.derivative_symbol)

    if cfg.market_maker.exchange and cfg.market_maker.symbol:
        symbols.setdefault(cfg.market_maker.exchange, set()).add(cfg.market_maker.symbol)

    runtime_exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    if runtime_exec_cfg.exchange and runtime_exec_cfg.symbol:
        symbols.setdefault(runtime_exec_cfg.exchange, set()).add(runtime_exec_cfg.symbol)

    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _account_payload_by_exchange(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(account.get("exchange") or ""): account
        for account in (payload or {}).get("accounts", []) or []
        if isinstance(account, dict) and account.get("exchange")
    }


def _account_payload_messages(account: dict[str, Any]) -> list[str]:
    messages = [
        str(message)
        for message in [
            *list(account.get("errors", []) or []),
            *list(account.get("warnings", []) or []),
        ]
        if message
    ]
    balance = account.get("balance") if isinstance(account.get("balance"), dict) else {}
    skipped = balance.get("skipped_reason")
    if skipped and skipped not in messages:
        messages.append(str(skipped))
    error = balance.get("error")
    if error and error not in messages:
        messages.append(str(error))
    return messages


def _readiness_message_key(message: str) -> str:
    normalized = " ".join(str(message or "").lower().split())
    if "api env" in normalized:
        if "not configured" in normalized:
            return "api:not_configured"
        if "missing" in normalized or "not set" in normalized:
            return "api:missing"
    if "no symbols configured" in normalized:
        return "market:no_symbols"
    if "account disabled by risk" in normalized:
        return "risk:account_disabled"
    if "global live trading disabled" in normalized:
        return "risk:global_live_disabled"
    return normalized


def _dedupe_readiness_messages(messages: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for message in messages:
        text_value = str(message or "").strip()
        if not text_value:
            continue
        key = _readiness_message_key(text_value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text_value)
    return deduped


def _readiness_action(
    *,
    priority: str,
    scope: str,
    action: str,
    status: str,
    detail: str = "",
    exchange: str = "",
    strategy: str = "",
) -> dict[str, Any]:
    return {
        "priority": priority,
        "scope": scope,
        "action": action,
        "status": status,
        "detail": detail,
        "exchange": exchange,
        "strategy": strategy,
    }


def _readiness_strategy_reasons(
    cfg: BotConfig,
    strategy: dict[str, Any],
    *,
    account_statuses: dict[str, dict[str, Any]],
    market_maker: dict[str, Any] | None,
    slow_execution: dict[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    strategy_id = str(strategy.get("id") or "")
    exchange = str(strategy.get("exchange") or "")
    if not strategy.get("configured"):
        reasons.append("not configured")
    if strategy.get("paused"):
        reasons.append("paused")
    if not cfg.risk.enabled:
        reasons.append("risk engine disabled")
    elif not cfg.risk.trading_enabled:
        reasons.append("risk trading switch disabled")
    elif not cfg.risk.allow_live_trading:
        reasons.append("global live trading disabled")
    if not strategy.get("strategy_allowed", True):
        reasons.append("strategy disabled by risk")
    if not strategy.get("account_enabled", True):
        reasons.append("account disabled by risk")
    if not strategy.get("live_ready", True):
        reasons.append("strategy live switch disabled")

    account = account_statuses.get(exchange)
    if exchange and account and account.get("status") in {"blocked", "warning"}:
        account_reason = (account.get("reasons") or [account["status"]])[0]
        reasons.append(f"account {account['status']}: {account_reason}")

    if strategy_id == "market_maker" and isinstance(market_maker, dict):
        safety = market_maker.get("safety") if isinstance(market_maker.get("safety"), dict) else {}
        if market_maker.get("status") == "error" and market_maker.get("error"):
            reasons.append(str(market_maker["error"]))
        for message in list(safety.get("reasons", []) or [])[:2]:
            if message:
                reasons.append(str(message))
    if strategy_id == "slow_execution" and isinstance(slow_execution, dict):
        if slow_execution.get("status") == "error" and slow_execution.get("error"):
            reasons.append(str(slow_execution["error"]))

    return _dedupe_readiness_messages(reasons)


def build_readiness_payload(
    cfg: BotConfig,
    *,
    account_balances: dict[str, Any] | None = None,
    order_activity: dict[str, Any] | None = None,
    trading_console: dict[str, Any] | None = None,
    market_maker: dict[str, Any] | None = None,
    slow_execution: dict[str, Any] | None = None,
    markets: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    account_balances = account_balances or {}
    order_activity = order_activity or {}
    trading_console = trading_console or build_trading_console_payload(cfg)
    market_maker = market_maker or {}
    slow_execution = slow_execution or {}
    symbols_by_exchange = _exchange_balance_symbols(cfg)
    balance_by_exchange = _account_payload_by_exchange(account_balances)
    order_by_exchange = _account_payload_by_exchange(order_activity)
    checking_statuses = {"starting", "checking", "pending"}

    account_rows: list[dict[str, Any]] = []
    for exchange in _all_account_exchanges(cfg):
        symbols = symbols_by_exchange.get(exchange.key, [])
        used = bool(symbols)
        auth = _auth_env_status(exchange)
        balance = balance_by_exchange.get(exchange.key, {})
        orders = order_by_exchange.get(exchange.key, {})
        balance_status = str(
            balance.get("status")
            or (account_balances.get("status") if account_balances.get("accounts") else "starting")
            or "starting"
        )
        order_status = str(
            orders.get("status")
            or (order_activity.get("status") if order_activity.get("accounts") else "starting")
            or "starting"
        )
        risk_enabled = _risk_account_enabled(cfg, exchange.key)
        reasons: list[str] = []
        if not used:
            reasons.append("no symbols configured")
        if used and not auth["configured"]:
            reasons.append("API env vars are not configured")
        elif used and auth["missing_env"]:
            reasons.append("one or more API env vars are not set")
        if used and not risk_enabled:
            reasons.append("account disabled by risk")
        if used and balance_status == "error":
            reasons.extend(_account_payload_messages(balance) or ["balance check failed"])
        elif used and balance_status == "warning":
            reasons.extend(_account_payload_messages(balance) or ["balance check warning"])
        if used and order_status == "error":
            reasons.extend(_account_payload_messages(orders) or ["order activity failed"])
        elif used and order_status == "warning":
            reasons.extend(_account_payload_messages(orders) or ["order activity warning"])

        if not used:
            status = "idle"
        elif balance_status in checking_statuses or order_status in checking_statuses:
            status = "checking"
        elif (
            not auth["private_checks_enabled"]
            or not risk_enabled
            or balance_status == "error"
            or order_status == "error"
        ):
            status = "blocked"
        elif balance_status == "warning" or order_status == "warning":
            status = "warning"
        else:
            status = "ready"

        deduped = _dedupe_readiness_messages(reasons)
        account_rows.append(
            {
                "key": exchange.key,
                "label": exchange.label or exchange.key,
                "id": exchange.id,
                "market_type": exchange.market_type,
                "symbols": symbols,
                "symbol_count": len(symbols),
                "used": used,
                "api_ready": auth["private_checks_enabled"],
                "api_status": (
                    "ready"
                    if auth["private_checks_enabled"]
                    else "missing env"
                    if auth["configured"]
                    else "not configured"
                ),
                "balance_status": balance_status,
                "order_status": order_status,
                "risk_enabled": risk_enabled,
                "status": status,
                "reasons": deduped[:6],
            }
        )

    account_statuses = {row["key"]: row for row in account_rows}
    strategy_rows: list[dict[str, Any]] = []
    for strategy in trading_console.get("strategies", []) or []:
        if not isinstance(strategy, dict):
            continue
        reasons = _readiness_strategy_reasons(
            cfg,
            strategy,
            account_statuses=account_statuses,
            market_maker=market_maker,
            slow_execution=slow_execution,
        )
        if strategy.get("live"):
            status = "live"
        elif not strategy.get("configured"):
            status = "idle"
        elif strategy.get("paused"):
            status = "paused"
        elif not cfg.risk.allow_live_trading:
            status = "guarded"
        elif reasons:
            status = "blocked"
        else:
            status = "standby"
        strategy_rows.append(
            {
                **strategy,
                "status": status,
                "reasons": reasons[:6],
            }
        )

    reconciliation = (
        order_activity.get("reconciliation")
        if isinstance(order_activity.get("reconciliation"), dict)
        else {}
    )
    market_missing_count = sum(
        1 for row in markets or [] if isinstance(row, dict) and row.get("status") != "ok"
    )
    ready_accounts = sum(1 for row in account_rows if row["status"] == "ready")
    used_accounts = sum(1 for row in account_rows if row["used"])
    checking_accounts = sum(1 for row in account_rows if row["status"] == "checking")
    blocked_accounts = sum(1 for row in account_rows if row["status"] == "blocked")
    warning_accounts = sum(1 for row in account_rows if row["status"] == "warning")
    live_strategies = sum(1 for row in strategy_rows if row["status"] == "live")
    configured_strategies = sum(1 for row in strategy_rows if row.get("configured"))
    blocked_strategies = sum(1 for row in strategy_rows if row["status"] == "blocked")
    warning_count = (
        warning_accounts
        + market_missing_count
        + (1 if reconciliation.get("status") == "warning" else 0)
        + (1 if order_activity.get("status") == "warning" else 0)
        + (1 if account_balances.get("status") == "warning" else 0)
    )

    account_checks_status = str(account_balances.get("status") or "starting")
    order_checks_status = str(order_activity.get("status") or "starting")
    if order_activity.get("status") == "error" or account_balances.get("status") == "error":
        status = "error"
    elif (
        checking_accounts
        or account_checks_status in checking_statuses
        or order_checks_status in checking_statuses
    ):
        status = "checking"
    elif not (cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading):
        status = "guarded"
    elif blocked_accounts or blocked_strategies:
        status = "blocked"
    elif warning_count:
        status = "warning"
    else:
        status = "ready"

    next_actions: list[dict[str, Any]] = []
    for row in account_rows:
        if not row["used"]:
            if row["api_status"] != "ready":
                next_actions.append(
                    _readiness_action(
                        priority="low",
                        scope=row["label"],
                        action="Add market symbols or leave account idle",
                        status=row["status"],
                        detail="This account has no configured symbols, so API readiness does not affect current trading.",
                        exchange=row["key"],
                    )
                )
            continue
        if not row["api_ready"]:
            next_actions.append(
                _readiness_action(
                    priority="high",
                    scope=row["label"],
                    action="Configure API environment variables",
                    status=row["status"],
                    detail=(row["reasons"] or ["API credentials are not ready"])[0],
                    exchange=row["key"],
                )
            )
        if not row["risk_enabled"]:
            next_actions.append(
                _readiness_action(
                    priority="high",
                    scope=row["label"],
                    action="Enable account in Risk Controls",
                    status=row["status"],
                    detail="The account switch is off, so live strategies cannot use it.",
                    exchange=row["key"],
                )
            )
        if row["balance_status"] == "error":
            next_actions.append(
                _readiness_action(
                    priority="high",
                    scope=row["label"],
                    action="Fix balance check error",
                    status=row["balance_status"],
                    detail="Private balance reads are failing for this account.",
                    exchange=row["key"],
                )
            )
        if row["order_status"] == "error":
            next_actions.append(
                _readiness_action(
                    priority="high",
                    scope=row["label"],
                    action="Fix order activity error",
                    status=row["order_status"],
                    detail="Open order or fill reads are failing for this account.",
                    exchange=row["key"],
                )
            )

    for row in strategy_rows:
        if row["status"] != "blocked":
            continue
        next_actions.append(
            _readiness_action(
                priority="medium",
                scope=row.get("label") or row.get("id") or "strategy",
                action="Resolve strategy blocker",
                status=row["status"],
                detail=(row.get("reasons") or ["strategy is blocked"])[0],
                exchange=str(row.get("exchange") or ""),
                strategy=str(row.get("id") or ""),
            )
        )

    if market_missing_count:
        next_actions.append(
            _readiness_action(
                priority="high",
                scope="Market Data",
                action="Fix missing order books or quote rates",
                status="warning",
                detail=f"{market_missing_count} configured market(s) are missing usable market data.",
            )
        )
    if order_activity.get("status") in {"warning", "error"}:
        next_actions.append(
            _readiness_action(
                priority="medium" if order_activity.get("status") == "warning" else "high",
                scope="Orders",
                action="Review order activity warnings",
                status=str(order_activity.get("status")),
                detail="Some configured accounts could not return orders or fills.",
            )
        )
    if int(reconciliation.get("issue_count") or 0) > 0:
        next_actions.append(
            _readiness_action(
                priority="medium",
                scope="Reconciliation",
                action="Review order/fill attribution",
                status=str(reconciliation.get("status") or "warning"),
                detail=(
                    f"{reconciliation.get('issue_count')} actionable "
                    "reconciliation issue(s)."
                ),
            )
        )

    action_priority = {"high": 0, "medium": 1, "low": 2}
    action_seen: set[tuple[str, str, str]] = set()
    unique_actions: list[dict[str, Any]] = []
    for action in sorted(
        next_actions,
        key=lambda item: (
            action_priority.get(str(item.get("priority") or "low"), 9),
            str(item.get("scope") or ""),
            str(item.get("action") or ""),
        ),
    ):
        key = (
            str(action.get("priority") or ""),
            str(action.get("scope") or ""),
            str(action.get("action") or ""),
        )
        if key in action_seen:
            continue
        action_seen.add(key)
        unique_actions.append(action)

    return {
        "status": status,
        "risk_enabled": cfg.risk.enabled,
        "trading_enabled": cfg.risk.trading_enabled,
        "live_trading": (
            cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading
        ),
        "accounts": account_rows,
        "strategies": strategy_rows,
        "balance_checks": {
            "status": account_balances.get("status") or "starting",
            "checked_account_count": account_balances.get("checked_account_count", 0),
            "total_account_count": account_balances.get("total_account_count", len(account_rows)),
        },
        "order_checks": {
            "status": order_activity.get("status") or "starting",
            "open_order_count": order_activity.get("open_order_count", 0),
            "recent_trade_count": order_activity.get("recent_trade_count", 0),
            "reconciliation_status": reconciliation.get("status") or "starting",
            "reconciliation_issue_count": reconciliation.get("issue_count", 0),
            "reconciliation_notice_count": reconciliation.get("notice_count", 0),
        },
        "market_checks": {
            "market_count": len(markets or []),
            "missing_count": market_missing_count,
        },
        "summary": {
            "used_accounts": used_accounts,
            "ready_accounts": ready_accounts,
            "blocked_accounts": blocked_accounts,
            "warning_accounts": warning_accounts,
            "idle_accounts": sum(1 for row in account_rows if row["status"] == "idle"),
            "checking_accounts": checking_accounts,
            "configured_strategies": configured_strategies,
            "live_strategies": live_strategies,
            "blocked_strategies": blocked_strategies,
            "paused_strategies": sum(1 for row in strategy_rows if row["status"] == "paused"),
            "blocked_count": blocked_accounts + blocked_strategies,
            "warning_count": warning_count,
            "warning_messages": list(warnings or [])[:6],
            "action_count": len(unique_actions),
        },
        "next_actions": unique_actions[:12],
        "checked_at": time.time(),
    }


def _all_account_exchanges(cfg: BotConfig) -> list[ExchangeConfig]:
    return [*cfg.spot_exchanges, *cfg.derivative_exchanges]


def _account_balance_status(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
        return "warning"
    if any(account["status"] == "error" for account in accounts):
        return "error"
    if any(account["status"] == "warning" for account in accounts):
        return "warning"
    return "ok"


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    if "/" not in symbol:
        return "", ""
    base, quote = symbol.split("/", 1)
    return base.upper(), quote.split(":", 1)[0].upper()


def _open_order_remaining_amount(raw: dict[str, Any]) -> float | None:
    remaining = _number_or_none(raw.get("remaining"))
    if remaining is not None:
        return max(0.0, remaining)
    amount = _number_or_none(raw.get("amount"))
    filled = _number_or_none(raw.get("filled"))
    if amount is not None and filled is not None:
        return max(0.0, amount - filled)
    if amount is not None:
        return max(0.0, amount)
    return None


def _open_order_price(raw: dict[str, Any]) -> float | None:
    price = _number_or_none(raw.get("price"))
    if price is not None and price > 0:
        return price
    average = _number_or_none(raw.get("average"))
    if average is not None and average > 0:
        return average
    return None


def _add_reserve(reserves: dict[str, float], currency: str, amount: float | None) -> None:
    if not currency or amount is None or amount <= 0:
        return
    reserves[currency] = reserves.get(currency, 0.0) + float(amount)


async def _fetch_open_order_reserves(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: Iterable[str],
) -> dict[str, Any]:
    fetcher = getattr(manager, "fetch_open_orders", None)
    if fetcher is None:
        return {"currencies": {}, "open_order_count": 0, "warnings": []}
    reserves: dict[str, float] = {}
    warnings: list[str] = []
    open_order_count = 0
    for symbol in sorted({item for item in symbols if item}):
        base, quote = _symbol_base_quote(symbol)
        try:
            open_orders = await fetcher(exchange, symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"{symbol} open order reserve check failed: {exc.__class__.__name__}: {exc}"
            )
            continue
        for raw in open_orders:
            if not isinstance(raw, dict):
                continue
            open_order_count += 1
            side = str(raw.get("side") or "").lower()
            remaining = _open_order_remaining_amount(raw)
            if side == "sell":
                _add_reserve(reserves, base, remaining)
            elif side == "buy":
                price = _open_order_price(raw)
                _add_reserve(
                    reserves,
                    quote,
                    remaining * price if remaining is not None and price is not None else None,
                )
    return {
        "currencies": dict(sorted(reserves.items())),
        "open_order_count": open_order_count,
        "warnings": warnings,
    }


def _apply_open_order_reserves_to_balance(
    currencies: list[dict[str, Any]],
    reserves: dict[str, float],
) -> list[dict[str, Any]]:
    rows = {
        str(row.get("currency") or "").upper(): dict(row)
        for row in currencies
        if row.get("currency")
    }
    for currency, reserved in reserves.items():
        currency = str(currency or "").upper()
        if not currency or reserved <= 0:
            continue
        row = rows.setdefault(
            currency,
            {"currency": currency, "free": None, "used": None, "total": None},
        )
        raw_free = _number_or_none(row.get("free"))
        raw_used = _number_or_none(row.get("used"))
        raw_total = _number_or_none(row.get("total"))
        if raw_total is None and (raw_free is not None or raw_used is not None):
            raw_total = float(raw_free or 0.0) + float(raw_used or 0.0)
        adjusted_used = max(float(raw_used or 0.0), float(reserved))
        raw_total_matches_free = (
            raw_total is not None
            and raw_free is not None
            and abs(float(raw_total) - float(raw_free)) <= 1e-9
        )
        reserve_is_hidden_from_exchange_used = float(raw_used or 0.0) <= 1e-9
        if raw_total_matches_free and reserve_is_hidden_from_exchange_used:
            adjusted_free = float(raw_free or 0.0)
            adjusted_total = adjusted_free + adjusted_used
            reserve_adjustment = "added_to_total"
        else:
            adjusted_total = max(float(raw_total or 0.0), adjusted_used)
            adjusted_free = max(0.0, adjusted_total - adjusted_used)
            reserve_adjustment = "within_total"
        row["open_order_reserved"] = float(reserved)
        row["open_order_reserve_adjustment"] = reserve_adjustment
        row["exchange_free"] = raw_free
        row["exchange_used"] = raw_used
        row["exchange_total"] = raw_total
        row["used"] = adjusted_used
        row["total"] = adjusted_total
        row["free"] = adjusted_free

    return sorted(rows.values(), key=lambda row: str(row.get("currency") or ""))


async def _fetch_exchange_balance_payload(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
) -> dict[str, Any]:
    auth = _auth_env_status(exchange)
    account: dict[str, Any] = {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "symbols": symbols,
        "auth": {
            "configured": auth["configured"],
            "private_checks_enabled": auth["private_checks_enabled"],
            "missing_env": auth["missing_env"],
        },
        "status": "ok",
        "warnings": [],
        "errors": [],
        "balance": {
            "checked": False,
            "skipped_reason": None,
            "currencies": [],
        },
    }

    if not symbols:
        account["status"] = "idle"
        account["balance"]["skipped_reason"] = "no configured symbols"
        return account
    if not auth["configured"]:
        account["status"] = "warning"
        account["warnings"].append("API env vars are not configured")
        account["balance"]["skipped_reason"] = "api env vars not configured"
        return account
    if auth["missing_env"]:
        account["status"] = "warning"
        account["warnings"].append("one or more configured API env vars are not set")
        account["balance"]["skipped_reason"] = "api env vars missing"
        return account

    try:
        balance = await manager.fetch_balance(exchange)
    except Exception as exc:  # noqa: BLE001
        message = f"{exc.__class__.__name__}: {exc}"
        account["status"] = "error"
        account["errors"].append(message)
        account["balance"] = {
            "checked": True,
            "error": message,
            "currencies": [],
        }
        return account

    currencies = _summarize_balance(
        balance,
        _balance_currencies(symbols),
        include_zero=False,
    )
    reserve_payload = await _fetch_open_order_reserves(manager, exchange, symbols)
    reserve_warnings = reserve_payload.get("warnings") or []
    if reserve_warnings:
        account["warnings"].extend(reserve_warnings)
        if account["status"] == "ok":
            account["status"] = "warning"
    currencies = _apply_open_order_reserves_to_balance(
        currencies,
        reserve_payload.get("currencies", {}),
    )
    account["balance"] = {
        "checked": True,
        "currencies": currencies,
        "open_order_reserves": reserve_payload,
    }
    return account


def _aggregate_account_balance_totals(
    accounts: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if not account.get("balance", {}).get("checked"):
            continue
        for row in account.get("balance", {}).get("currencies", []):
            currency = str(row["currency"]).upper()
            total_row = totals.setdefault(
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
                value = row.get(field)
                if value is not None:
                    total_row[field] += float(value)

    preferred = {"ACS": 0, "USDC": 1, "USDT": 2, "USD": 3, "KRW": 4}
    return sorted(
        totals.values(),
        key=lambda row: (preferred.get(row["currency"], 99), row["currency"]),
    )


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_fee_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    fee = raw.get("fee")
    if not isinstance(fee, dict):
        return None
    cost = _number_or_none(fee.get("cost"))
    currency = fee.get("currency")
    if cost is None and currency is None:
        return None
    return {
        "cost": cost,
        "currency": str(currency) if currency is not None else "",
    }


def _normalize_order(
    exchange: ExchangeConfig,
    raw: dict[str, Any],
    fallback_symbol: str,
) -> dict[str, Any]:
    price = _number_or_none(raw.get("price"))
    amount = _number_or_none(raw.get("amount"))
    filled = _number_or_none(raw.get("filled"))
    remaining = _number_or_none(raw.get("remaining"))
    cost = _number_or_none(raw.get("cost"))
    if cost is None and price is not None and amount is not None:
        cost = price * amount
    return {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": str(raw.get("id", "")),
        "client_order_id": str(
            raw.get("clientOrderId") or raw.get("clientOrderID") or ""
        ),
        "symbol": str(raw.get("symbol") or fallback_symbol),
        "side": str(raw.get("side") or ""),
        "type": str(raw.get("type") or ""),
        "status": str(raw.get("status") or ""),
        "price": price,
        "average": _number_or_none(raw.get("average")),
        "amount": amount,
        "filled": filled,
        "remaining": remaining,
        "cost": cost,
        "fee": _order_fee_payload(raw),
        "timestamp": _number_or_none(raw.get("timestamp")),
        "datetime": raw.get("datetime"),
    }


def _normalize_trade(
    exchange: ExchangeConfig,
    raw: dict[str, Any],
    fallback_symbol: str,
) -> dict[str, Any]:
    return {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": str(raw.get("id", "")),
        "order_id": str(raw.get("order") or ""),
        "symbol": str(raw.get("symbol") or fallback_symbol),
        "side": str(raw.get("side") or ""),
        "type": str(raw.get("type") or ""),
        "price": _number_or_none(raw.get("price")),
        "amount": _number_or_none(raw.get("amount")),
        "cost": _number_or_none(raw.get("cost")),
        "fee": _order_fee_payload(raw),
        "timestamp": _number_or_none(raw.get("timestamp")),
        "datetime": raw.get("datetime"),
    }


def _activity_status(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
        return "warning"
    if any(account["status"] == "error" for account in accounts):
        return "error"
    if any(account["status"] == "warning" for account in accounts):
        return "warning"
    return "ok"


async def _fetch_exchange_order_activity(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
    *,
    limit: int,
) -> dict[str, Any]:
    auth = _auth_env_status(exchange)
    account: dict[str, Any] = {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "symbols": symbols,
        "status": "ok",
        "warnings": [],
        "errors": [],
        "open_orders": [],
        "closed_orders": [],
        "recent_trades": [],
    }
    if not symbols:
        account["status"] = "idle"
        account["skipped_reason"] = "no configured symbols"
        account["open_order_count"] = 0
        account["closed_order_count"] = 0
        account["recent_trade_count"] = 0
        return account
    if not auth["configured"]:
        account["status"] = "warning"
        account["warnings"].append("API env vars are not configured")
        return account
    if auth["missing_env"]:
        account["status"] = "warning"
        account["warnings"].append("one or more configured API env vars are not set")
        return account

    for symbol in symbols:
        try:
            open_orders = await manager.fetch_open_orders(exchange, symbol=symbol)
            account["open_orders"].extend(
                _normalize_order(exchange, order, symbol) for order in open_orders
            )
        except Exception as exc:  # noqa: BLE001
            account["errors"].append(
                f"{symbol} open orders failed: {exc.__class__.__name__}: {exc}"
            )

        try:
            closed_orders = await manager.fetch_closed_orders(
                exchange,
                symbol=symbol,
                limit=limit,
            )
            account["closed_orders"].extend(
                _normalize_order(exchange, order, symbol) for order in closed_orders
            )
        except Exception as exc:  # noqa: BLE001
            account["warnings"].append(
                f"{symbol} closed orders unavailable: {exc.__class__.__name__}: {exc}"
            )

        try:
            trades = await manager.fetch_my_trades(
                exchange,
                symbol=symbol,
                limit=limit,
            )
            account["recent_trades"].extend(
                _normalize_trade(exchange, trade, symbol) for trade in trades
            )
        except Exception as exc:  # noqa: BLE001
            account["warnings"].append(
                f"{symbol} fills unavailable: {exc.__class__.__name__}: {exc}"
            )

    if account["errors"]:
        account["status"] = "error"
    elif account["warnings"]:
        account["status"] = "warning"
    account["open_order_count"] = len(account["open_orders"])
    account["closed_order_count"] = len(account["closed_orders"])
    account["recent_trade_count"] = len(account["recent_trades"])
    return account


def _sort_activity_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: float(row.get("timestamp") or 0),
        reverse=True,
    )


async def fetch_order_activity_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    exec_cfg: SlowExecutionConfig | None = None,
    *,
    limit: int = ORDER_ACTIVITY_LIMIT,
    quote_rates: dict[str, float] | None = None,
    books: dict[tuple[str, str], OrderBookSnapshot] | None = None,
    market_maker_runtime: dict[str, Any] | None = None,
    auto_buy_sell_tasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quote_rates = cfg.quote_rates if quote_rates is None else quote_rates
    books = {} if books is None else books
    try:
        recent_log_entries = read_recent_trade_entries(cfg.trade_log)
        attribution_warnings: list[str] = []
    except OSError as exc:
        recent_log_entries = []
        attribution_warnings = [f"trade log attribution unavailable: {exc}"]
    order_attribution = build_order_attribution_map(recent_log_entries)
    symbols_by_exchange = _exchange_balance_symbols(cfg, exec_cfg)
    exchanges = _all_account_exchanges(cfg)
    accounts = await asyncio.gather(
        *[
            _fetch_exchange_order_activity(
                manager,
                exchange,
                symbols_by_exchange.get(exchange.key, []),
                limit=limit,
            )
            for exchange in exchanges
        ]
    )
    open_orders = _sort_activity_rows(
        order for account in accounts for order in account["open_orders"]
    )
    closed_orders = _sort_activity_rows(
        order for account in accounts for order in account["closed_orders"]
    )[:limit]
    recent_trades = _sort_activity_rows(
        trade for account in accounts for trade in account["recent_trades"]
    )[:limit]
    open_orders = [
        {
            **order,
            "attribution": _trade_attribution(
                {
                    "exchange": order["exchange"],
                    "symbol": order["symbol"],
                    "order_id": order["id"],
                },
                order_attribution,
            ),
        }
        for order in open_orders
    ]
    recent_trades, pnl_summary = enrich_recent_trades_with_pnl(
        cfg,
        recent_trades,
        quote_rates=quote_rates,
        books=books,
        attribution=order_attribution,
    )
    try:
        pnl_store_payload = persist_fill_pnl(
            cfg.pnl_store,
            recent_trades,
            currency=cfg.common_quote_currency,
        )
        pnl_store_warnings: list[str] = []
    except Exception as exc:  # noqa: BLE001
        pnl_store_payload = {
            "enabled": cfg.pnl_store.enabled,
            "path": cfg.pnl_store.path,
            "stored_fill_count": 0,
            "daily": {
                "enabled": cfg.pnl_store.enabled,
                "path": cfg.pnl_store.path,
                "day": None,
                "currency": cfg.common_quote_currency,
                "trade_count": 0,
                "total_realized_pnl": 0.0,
                "total_fees": 0.0,
                "total_notional": 0.0,
                "sources": {},
                "updated_at": None,
            },
            "error": str(exc),
        }
        pnl_store_warnings = [f"fill P/L store unavailable: {exc}"]
    errors = [
        f"{account['exchange']}: {error}"
        for account in accounts
        for error in account.get("errors", [])
    ]
    warnings = [
        f"{account['exchange']}: {warning}"
        for account in accounts
        for warning in account.get("warnings", [])
    ]
    warnings.extend(attribution_warnings)
    warnings.extend(pnl_store_warnings)
    checked_accounts = sum(
        1
        for account in accounts
        if account.get("status") != "idle"
        and account.get("open_order_count") is not None
        and not account.get("errors")
    )
    base_payload = {
        "status": _activity_status(accounts),
        "accounts": accounts,
        "open_orders": open_orders,
        "closed_orders": closed_orders,
        "recent_trades": recent_trades,
        "pnl_summary": pnl_summary,
        "pnl_store": pnl_store_payload,
        "daily_pnl": pnl_store_payload.get("daily"),
        "open_order_count": len(open_orders),
        "closed_order_count": len(closed_orders),
        "recent_trade_count": len(recent_trades),
        "checked_account_count": checked_accounts,
        "total_account_count": len(accounts),
        "last_finished": time.time(),
        "errors": errors,
        "warnings": warnings,
    }
    base_payload["reconciliation"] = build_order_reconciliation_payload(
        base_payload,
        market_maker_runtime=market_maker_runtime,
        auto_buy_sell_tasks=auto_buy_sell_tasks,
    )
    return {
        **base_payload,
    }


def _find_exchange_by_key(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in _all_account_exchanges(cfg):
        if exchange.key == key:
            return exchange
    raise ValueError(f"unknown exchange account: {key}")


async def cancel_order_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    payload: dict[str, Any],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    exchange_key = str(payload.get("exchange", "")).strip()
    symbol = str(payload.get("symbol", "")).strip()
    order_id = str(payload.get("order_id", "")).strip()
    if not exchange_key:
        raise ValueError("exchange is required")
    if not symbol:
        raise ValueError("symbol is required")
    if not order_id:
        raise ValueError("order_id is required")

    exchange = _find_exchange_by_key(cfg, exchange_key)
    allowed_symbols = set(_exchange_balance_symbols(cfg, exec_cfg).get(exchange.key, []))
    if symbol not in allowed_symbols:
        raise ValueError(f"symbol is not configured for account: {symbol}")
    auth = _auth_env_status(exchange)
    if not auth["configured"]:
        raise ValueError("API env vars are not configured for this exchange")
    if auth["missing_env"]:
        raise ValueError("one or more configured API env vars are not set")

    canceled = await manager.cancel_order(
        exchange,
        symbol=symbol,
        order_id=order_id,
    )
    cancel_summary = (
        _normalize_order(exchange, canceled, symbol)
        if isinstance(canceled, dict)
        else {"id": order_id, "status": str(canceled), "symbol": symbol}
    )
    event = write_trade_event(
        cfg.trade_log,
        {
            "type": "manual_order_cancel",
            "strategy": "manual",
            "mode": "live",
            "status": "canceled",
            "plan": {
                "exchange": exchange.key,
                "symbol": symbol,
                "side": "",
            },
            "execution": {
                "canceled_count": 1,
                "placed_count": 0,
                "placed_order_ids": [],
                "canceled_order_ids": [order_id],
            },
            "risk": {
                "approved": True,
                "level": "manual",
                "reasons": [],
                "warnings": [],
                "order_count": 0,
                "total_quote_notional": 0.0,
            },
            "cancel_result": cancel_summary,
        },
    )
    write_strategy_timeline_from_payload(
        cfg.strategy_timeline,
        event,
        source="manual",
    )
    return {
        "ok": True,
        "exchange": exchange.key,
        "symbol": symbol,
        "order_id": order_id,
        "canceled": cancel_summary,
        "event": event,
    }


async def cancel_bulk_orders_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    payload: dict[str, Any],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    scope = str(payload.get("scope", "all")).strip().lower()
    exchange_key = str(payload.get("exchange", "")).strip()
    if scope not in {"all", "account"}:
        raise ValueError("scope must be all or account")
    if scope == "account" and not exchange_key:
        raise ValueError("exchange is required for account scope")

    allowed_symbols = _exchange_balance_symbols(cfg, exec_cfg)
    exchanges_by_key = {exchange.key: exchange for exchange in _all_account_exchanges(cfg)}
    if exchange_key and exchange_key not in exchanges_by_key:
        raise ValueError(f"unknown exchange account: {exchange_key}")

    current_activity = await fetch_order_activity_payload(
        cfg,
        manager,
        exec_cfg,
    )
    candidates = [
        order
        for order in current_activity.get("open_orders", [])
        if scope == "all" or order.get("exchange") == exchange_key
    ]
    canceled = []
    errors = []
    for order in candidates:
        order_id = str(order.get("id") or "").strip()
        order_exchange = str(order.get("exchange") or "")
        symbol = str(order.get("symbol") or "")
        if not order_id:
            errors.append({"order": order, "error": "order id is missing"})
            continue
        if symbol not in allowed_symbols.get(order_exchange, []):
            errors.append({"order": order, "error": f"symbol is not configured: {symbol}"})
            continue
        try:
            exchange = exchanges_by_key[order_exchange]
            raw = await manager.cancel_order(
                exchange,
                symbol=symbol,
                order_id=order_id,
            )
            canceled.append(
                _normalize_order(exchange, raw, symbol)
                if isinstance(raw, dict)
                else {
                    "exchange": order_exchange,
                    "symbol": symbol,
                    "id": order_id,
                    "status": str(raw),
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "exchange": order_exchange,
                    "symbol": symbol,
                    "order_id": order_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    event = write_trade_event(
        cfg.trade_log,
        {
            "type": "manual_bulk_cancel",
            "strategy": "manual",
            "mode": "live",
            "status": "canceled" if not errors else "partial",
            "plan": {
                "exchange": exchange_key if scope == "account" else "all",
                "symbol": "configured_open_orders",
                "side": "",
            },
            "execution": {
                "canceled_count": len(canceled),
                "placed_count": 0,
                "placed_order_ids": [],
                "canceled_order_ids": [
                    str(order.get("id") or "") for order in canceled
                ],
            },
            "risk": {
                "approved": True,
                "level": "manual",
                "reasons": [],
                "warnings": [item["error"] for item in errors],
                "order_count": len(candidates),
                "total_quote_notional": sum(
                    float(order.get("cost") or 0.0) for order in candidates
                ),
            },
            "cancel_errors": errors,
        },
    )
    write_strategy_timeline_from_payload(
        cfg.strategy_timeline,
        event,
        source="manual",
    )
    return {
        "ok": len(errors) == 0,
        "scope": scope,
        "exchange": exchange_key,
        "requested_count": len(candidates),
        "canceled_count": len(canceled),
        "error_count": len(errors),
        "canceled": canceled,
        "errors": errors,
        "event": event,
    }


async def fetch_account_balances_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    symbols_by_exchange = _exchange_balance_symbols(cfg, exec_cfg)
    exchanges = _all_account_exchanges(cfg)
    accounts = await asyncio.gather(
        *[
            _fetch_exchange_balance_payload(
                manager,
                exchange,
                symbols_by_exchange.get(exchange.key, []),
            )
            for exchange in exchanges
        ]
    )
    errors = [
        f"{account['exchange']}: {error}"
        for account in accounts
        for error in account.get("errors", [])
    ]
    return {
        "status": _account_balance_status(accounts),
        "accounts": accounts,
        "totals": _aggregate_account_balance_totals(accounts),
        "checked_account_count": sum(
            1 for account in accounts if account.get("balance", {}).get("checked")
        ),
        "total_account_count": len(accounts),
        "last_finished": time.time(),
        "errors": errors,
    }


def default_runtime_store_path(cfg: BotConfig) -> str:
    return str(Path(cfg.trade_log.path).with_name("web_runtime_overrides.json"))


def default_web_audit_path(cfg: BotConfig) -> str:
    return str(Path(cfg.trade_log.path).with_name("web_audit_events.jsonl"))


def _sanitize_audit_payload(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if any(
                marker in key_lower
                for marker in ("api_key", "secret", "password", "token", "cookie")
            ):
                clean[key_text] = "[redacted]"
            else:
                clean[key_text] = _sanitize_audit_payload(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_audit_payload(item) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _audit_event_id(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def read_recent_web_audit_events(
    cfg: BotConfig,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    path = Path(default_web_audit_path(cfg))
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in reversed(lines[-max(limit * 3, limit) :]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
        if len(events) >= limit:
            break
    return events


def write_web_audit_event(
    cfg: BotConfig,
    request: web.Request,
    *,
    action: str,
    status: str = "ok",
    target: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return write_system_web_audit_event(
        cfg,
        action=action,
        status=status,
        target=target,
        detail=detail,
        payload=payload,
        error=error,
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )


def write_system_web_audit_event(
    cfg: BotConfig,
    *,
    action: str,
    status: str = "ok",
    target: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    actor_ip: str = "system",
    path: str = "system",
    method: str = "SYSTEM",
    user_agent: str = "system",
) -> dict[str, Any]:
    event = {
        "logged_at": time.time(),
        "action": action,
        "status": status,
        "target": target,
        "detail": detail,
        "actor_ip": actor_ip,
        "path": path,
        "method": method,
        "user_agent": user_agent[:160],
        "payload": _sanitize_audit_payload(payload or {}),
    }
    if error:
        event["error"] = error
    event["event_id"] = _audit_event_id(event)
    path = Path(default_web_audit_path(cfg))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True))
            handle.write("\n")
    except OSError as exc:
        return {
            **event,
            "status": "error",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    return event


def _dataclass_overrides(raw: Any, model: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = {field.name for field in fields(model)}
    return {key: value for key, value in raw.items() if key in allowed}


def _load_runtime_overrides(path: Path, cfg: BotConfig) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"loaded": False, "path": str(path), "data": {}}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "loaded": False,
            "path": str(path),
            "error": f"{exc.__class__.__name__}: {exc}",
            "data": {},
        }
    if not isinstance(raw, dict):
        return {
            "loaded": False,
            "path": str(path),
            "error": "runtime override store must be a JSON object",
            "data": {},
        }

    data: dict[str, Any] = {
        "risk_overrides": _dataclass_overrides(
            raw.get("risk_overrides"),
            cfg.risk,
        ),
        "market_maker_overrides": _dataclass_overrides(
            raw.get("market_maker_overrides"),
            cfg.market_maker,
        ),
        "slow_execution_overrides": _dataclass_overrides(
            raw.get("slow_execution_overrides"),
            cfg.slow_execution,
        ),
        "strategy_paused": {
            key: bool(value)
            for key, value in (raw.get("strategy_paused") or {}).items()
            if key in STRATEGY_IDS
        },
    }
    program = raw.get("program")
    if isinstance(program, dict):
        program_state: dict[str, Any] = {}
        if isinstance(program.get("running"), bool):
            program_state["running"] = program["running"]
        if isinstance(program.get("auto_stopped"), bool):
            program_state["auto_stopped"] = program["auto_stopped"]
        if isinstance(program.get("updated_at"), (int, float)):
            program_state["updated_at"] = float(program["updated_at"])
        if isinstance(program.get("stopped_at"), (int, float)):
            program_state["stopped_at"] = float(program["stopped_at"])
        if program.get("stop_reason") is None or isinstance(
            program.get("stop_reason"),
            str,
        ):
            program_state["stop_reason"] = program.get("stop_reason")
        if program_state:
            data["program"] = program_state

    allowed_spot_exchanges = {exchange.key for exchange in cfg.spot_exchanges}
    if raw.get("spot_markets") is not None:
        try:
            data["spot_markets"] = spot_markets_to_list(
                _spot_markets_from_payload(
                    {"spot_markets": raw.get("spot_markets")},
                    allowed_exchanges=allowed_spot_exchanges,
                )
            )
        except (TypeError, ValueError) as exc:
            return {
                "loaded": False,
                "path": str(path),
                "error": f"invalid spot_markets in runtime store: {exc}",
                "data": {},
            }
    if raw.get("cash_and_carry_pairs") is not None:
        try:
            data["cash_and_carry_pairs"] = cash_and_carry_pairs_to_list(
                _cash_and_carry_pairs_from_payload(
                    {"cash_and_carry_pairs": raw.get("cash_and_carry_pairs")}
                )
            )
        except (TypeError, ValueError) as exc:
            return {
                "loaded": False,
                "path": str(path),
                "error": f"invalid cash_and_carry_pairs in runtime store: {exc}",
                "data": {},
            }

    return {
        "loaded": True,
        "path": str(path),
        "updated_at": raw.get("updated_at"),
        "data": data,
    }


def _save_runtime_overrides(path: Path, payload: dict[str, Any]) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError as exc:
        return f"{exc.__class__.__name__}: {exc}"
    return None


def _build_initial_payload(cfg: BotConfig, poll_seconds: float) -> dict[str, Any]:
    return {
        "status": "starting",
        "config": {
            "poll_seconds": poll_seconds,
            "notional_quote": cfg.notional_quote,
            "min_profit_quote": cfg.min_profit_quote,
            "min_profit_bps": cfg.min_profit_bps,
            "common_quote_currency": cfg.common_quote_currency,
            "spot_markets": spot_markets_to_list(cfg.spot_markets),
            "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                cfg.cash_and_carry_pairs
            ),
            "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
            "derivative_exchanges": exchange_configs_to_list(
                cfg.derivative_exchanges
            ),
        },
        "scan": {
            "count": 0,
            "elapsed_ms": None,
            "last_started": None,
            "last_finished": None,
        },
        "markets": [],
        "quote_rates": cfg.quote_rates,
        "opportunities": [],
        "recent_opportunities": [],
        "account_balances": {
            "status": "starting",
            "accounts": [],
            "totals": [],
            "checked_account_count": 0,
            "total_account_count": len(_all_account_exchanges(cfg)),
            "last_finished": None,
            "errors": [],
        },
        "order_activity": {
            "status": "starting",
            "accounts": [],
            "open_orders": [],
            "closed_orders": [],
            "recent_trades": [],
            "pnl_summary": {
                "currency": cfg.common_quote_currency,
                "window": "recent_fills",
                "trade_count": 0,
                "attributed_trade_count": 0,
                "unattributed_trade_count": 0,
                "total_realized_pnl": 0.0,
                "total_fees": 0.0,
                "total_notional": 0.0,
                "sources": {},
                "missing_cost_basis": [],
                "missing_quote_rates": [],
                "missing_fee_rates": [],
                "observed_at": None,
            },
            "pnl_store": {
                "enabled": cfg.pnl_store.enabled,
                "path": cfg.pnl_store.path,
                "stored_fill_count": 0,
                "daily": None,
            },
            "daily_pnl": {
                "enabled": cfg.pnl_store.enabled,
                "path": cfg.pnl_store.path,
                "day": None,
                "currency": cfg.common_quote_currency,
                "trade_count": 0,
                "total_realized_pnl": 0.0,
                "total_fees": 0.0,
                "total_notional": 0.0,
                "sources": {},
                "updated_at": None,
            },
            "open_order_count": 0,
            "closed_order_count": 0,
            "recent_trade_count": 0,
            "reconciliation": {
                "status": "starting",
                "tracked_order_count": 0,
                "matched_open_count": 0,
                "matched_fill_count": 0,
                "untracked_open_count": 0,
                "unattributed_fill_count": 0,
                "issue_count": 0,
                "notice_count": 0,
                "total_item_count": 0,
                "level_counts": {"error": 0, "warning": 0, "info": 0},
                "critical_issue_count": 0,
                "auto_stop_recommended": False,
                "auto_stop_reasons": [],
                "issues": [],
                "checked_at": None,
            },
            "checked_account_count": 0,
            "total_account_count": len(_all_account_exchanges(cfg)),
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "trading_console": build_trading_console_payload(cfg),
        "readiness": build_readiness_payload(cfg),
        "runtime_store": {
            "enabled": False,
            "path": "",
            "loaded": False,
            "saved_at": None,
            "error": None,
        },
        "onchain": {
            "status": "disabled",
            "label": cfg.onchain_monitor.label,
            "mint": cfg.onchain_monitor.token_mint,
            "holders": [],
            "history": {
                "enabled": cfg.onchain_monitor.enabled,
                "path": cfg.onchain_monitor.history_path,
                "baseline_at": None,
                "updated_at": None,
                "event_count": 0,
                "new_event_count": 0,
                "recent_events": [],
            },
            "rpc": {
                "active_url": cfg.onchain_monitor.rpc_url,
                "endpoint_count": len(cfg.onchain_monitor.rpc_urls or []),
                "env": cfg.onchain_monitor.rpc_url_env,
            },
            "last_finished": None,
            "error": None,
        },
        "market_maker": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": market_maker_config_to_dict(cfg.market_maker),
            "accounts": slow_execution_accounts(
                _all_account_exchanges(cfg),
                _market_maker_symbols_by_exchange(cfg),
            ),
            "quote_conversion": market_maker_quote_conversion(
                cfg,
                cfg.market_maker.symbol,
            )
            if cfg.market_maker.symbol
            else {
                "quote_currency": "",
                "common_quote_currency": cfg.common_quote_currency,
                "quote_to_common_rate": None,
                "available": False,
            },
            "safety": build_market_maker_safety_payload(
                cfg,
                None,
                (
                    market_maker_quote_conversion(cfg, cfg.market_maker.symbol)
                    if cfg.market_maker.symbol
                    else {
                        "quote_currency": "",
                        "common_quote_currency": cfg.common_quote_currency,
                        "quote_to_common_rate": None,
                        "available": False,
                    }
                ),
            ),
            "runtime": {},
            "quality": {},
            "error": None,
        },
        "slow_execution": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": slow_execution_config_to_dict(cfg.slow_execution),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _spot_symbols_by_exchange(cfg),
            ),
            "tasks": {
                "status": "ok",
                "path": default_task_store_path(cfg),
                "tasks": [],
                "task_count": 0,
                "active_count": 0,
                "updated_at": time.time(),
            },
            "error": None,
        },
        "spot_arbitrage": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "risk": None,
            "execution": None,
            "error": None,
            "cooldown_seconds": SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
        },
        "portfolio": {
            "status": "disabled",
            "asset": cfg.portfolio.asset,
            "quote_currency": cfg.common_quote_currency,
            "position_base": cfg.portfolio.position_base,
            "average_entry_price": cfg.portfolio.average_entry_price,
            "positions": [
                {
                    "asset": position.asset,
                    "position_base": position.position_base,
                    "average_entry_price": position.average_entry_price,
                    "mark_price": None,
                    "mark_source_count": 0,
                    "position_value": None,
                    "price_move_pnl": 0.0,
                    "status": "starting",
                }
                for position in cfg.portfolio.positions
            ],
            "position_missing_marks": [],
            "cash_balances": cfg.portfolio.cash_balances,
            "cash_balances_common": {},
            "cash_value": 0.0,
            "cash_missing_rates": [],
            "mark_price": None,
            "mark_source_count": 0,
            "position_value": None,
            "total_pnl": 0.0,
            "sources": {
                "market_maker": 0.0,
                "arbitrage": 0.0,
                "auto_buy_sell": 0.0,
                "manual": 0.0,
                "unattributed": 0.0,
                "price_move": 0.0,
            },
            "observed_at": None,
        },
        "program": {
            "running": True,
            "updated_at": time.time(),
            "auto_stopped": False,
            "stop_reason": None,
            "stopped_at": None,
        },
        "operations": build_operations_payload(cfg),
        "warnings": ["Waiting for first scan"],
    }


class MonitorState:
    def __init__(
        self,
        cfg: BotConfig,
        poll_seconds: float,
        *,
        runtime_store_path: str | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._program_running = True
        self._program_updated_at = time.time()
        self._auto_stopped = False
        self._auto_stop_reason: str | None = None
        self._auto_stopped_at: float | None = None
        self._runtime_store_path = Path(runtime_store_path) if runtime_store_path else None
        self._runtime_store_loaded = False
        self._runtime_store_updated_at: float | None = None
        self._runtime_store_saved_at: float | None = None
        self._runtime_store_error: str | None = None
        store_data: dict[str, Any] = {}
        if self._runtime_store_path is not None:
            loaded = _load_runtime_overrides(self._runtime_store_path, cfg)
            self._runtime_store_loaded = bool(loaded.get("loaded"))
            self._runtime_store_updated_at = (
                float(loaded["updated_at"]) if loaded.get("updated_at") else None
            )
            self._runtime_store_error = loaded.get("error")
            store_data = loaded.get("data", {})
        program = store_data.get("program") if isinstance(store_data, dict) else {}
        if isinstance(program, dict):
            self._program_running = bool(program.get("running", True))
            if isinstance(program.get("updated_at"), (int, float)):
                self._program_updated_at = float(program["updated_at"])
            self._auto_stopped = bool(program.get("auto_stopped", False))
            self._auto_stop_reason = (
                str(program["stop_reason"])
                if isinstance(program.get("stop_reason"), str)
                else None
            )
            self._auto_stopped_at = (
                float(program["stopped_at"])
                if isinstance(program.get("stopped_at"), (int, float))
                else None
            )
            if self._auto_stopped:
                self._program_running = False
        self._risk_overrides: dict[str, Any] = dict(
            store_data.get("risk_overrides", {})
        )
        self._market_maker_overrides: dict[str, Any] = dict(
            store_data.get("market_maker_overrides", {})
        )
        self._slow_execution_overrides: dict[str, Any] = dict(
            store_data.get("slow_execution_overrides", {})
        )
        self._spot_markets_override: list[SpotMarketConfig] | None = (
            _spot_markets_from_payload(
                {"spot_markets": store_data["spot_markets"]},
                allowed_exchanges={exchange.key for exchange in cfg.spot_exchanges},
            )
            if "spot_markets" in store_data
            else None
        )
        self._cash_and_carry_pairs_override: list[CashAndCarryPair] | None = (
            _cash_and_carry_pairs_from_payload(
                {"cash_and_carry_pairs": store_data["cash_and_carry_pairs"]}
            )
            if "cash_and_carry_pairs" in store_data
            else None
        )
        self._strategy_paused: dict[str, bool] = {
            strategy_id: False for strategy_id in STRATEGY_IDS
        }
        self._strategy_paused.update(store_data.get("strategy_paused", {}))
        runtime_cfg = self._runtime_config_unlocked(cfg)
        self._payload = _build_initial_payload(runtime_cfg, poll_seconds)
        if not self._program_running:
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                self._payload["warnings"] = [
                    self._auto_stop_reason or "Program auto-stopped"
                ]
            else:
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused"]
        self._payload["program"] = self._program_payload_unlocked()
        self._payload["runtime_store"] = self._runtime_store_status_unlocked()
        self._market_maker_runtime: dict[str, Any] = self._payload["market_maker"][
            "runtime"
        ]
        self._auto_buy_sell_tasks = self._payload["slow_execution"]["tasks"]
        self._recent_opportunities: deque[dict[str, Any]] = deque(maxlen=100)

    def _runtime_store_status_unlocked(self) -> dict[str, Any]:
        return {
            "enabled": self._runtime_store_path is not None,
            "path": str(self._runtime_store_path or ""),
            "loaded": self._runtime_store_loaded,
            "updated_at": self._runtime_store_updated_at,
            "saved_at": self._runtime_store_saved_at,
            "error": self._runtime_store_error,
        }

    def _runtime_store_payload_unlocked(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": time.time(),
            "risk_overrides": self._risk_overrides,
            "market_maker_overrides": self._market_maker_overrides,
            "slow_execution_overrides": self._slow_execution_overrides,
            "strategy_paused": self._strategy_paused,
            "program": self._program_payload_unlocked(),
        }
        if self._spot_markets_override is not None:
            payload["spot_markets"] = spot_markets_to_list(
                self._spot_markets_override
            )
        if self._cash_and_carry_pairs_override is not None:
            payload["cash_and_carry_pairs"] = cash_and_carry_pairs_to_list(
                self._cash_and_carry_pairs_override
            )
        return payload

    def _save_runtime_store_unlocked(self) -> None:
        if self._runtime_store_path is None:
            return
        payload = self._runtime_store_payload_unlocked()
        error = _save_runtime_overrides(self._runtime_store_path, payload)
        self._runtime_store_error = error
        if error is None:
            self._runtime_store_loaded = True
            self._runtime_store_updated_at = float(payload["updated_at"])
            self._runtime_store_saved_at = time.time()
        if "runtime_store" in self._payload:
            self._payload["runtime_store"] = self._runtime_store_status_unlocked()

    def _program_payload_unlocked(self) -> dict[str, Any]:
        return {
            "running": self._program_running,
            "updated_at": self._program_updated_at,
            "auto_stopped": self._auto_stopped,
            "stop_reason": self._auto_stop_reason,
            "stopped_at": self._auto_stopped_at,
        }

    def _runtime_config_unlocked(self, cfg: BotConfig) -> BotConfig:
        return replace(
            cfg,
            spot_markets=(
                self._spot_markets_override
                if self._spot_markets_override is not None
                else cfg.spot_markets
            ),
            cash_and_carry_pairs=(
                self._cash_and_carry_pairs_override
                if self._cash_and_carry_pairs_override is not None
                else cfg.cash_and_carry_pairs
            ),
            risk=replace(cfg.risk, **self._risk_overrides),
            market_maker=replace(
                cfg.market_maker,
                **self._market_maker_overrides,
            ),
            slow_execution=replace(
                cfg.slow_execution,
                **self._slow_execution_overrides,
            ),
        )

    async def get(self, view: str | None = None) -> dict[str, Any]:
        async with self._lock:
            payload = state_payload_for_view(self._payload, view)
            return json.loads(json.dumps(payload))

    async def portfolio_payload(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._payload.get("portfolio", {})))

    async def is_running(self) -> bool:
        async with self._lock:
            return self._program_running

    async def program_updated_at(self) -> float:
        async with self._lock:
            return self._program_updated_at

    async def slow_execution_config(
        self,
        base_config: SlowExecutionConfig,
    ) -> SlowExecutionConfig:
        async with self._lock:
            return replace(base_config, **self._slow_execution_overrides)

    async def market_maker_config(
        self,
        base_config: MarketMakerConfig,
    ) -> MarketMakerConfig:
        async with self._lock:
            return replace(base_config, **self._market_maker_overrides)

    async def set_market_maker_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
    ) -> dict[str, Any]:
        async with self._lock:
            self._market_maker_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "market_maker" in self._payload:
                current_config = self._payload["market_maker"].get("config", {})
                current_config.update(overrides)
                self._payload["market_maker"]["config"] = current_config
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    _market_maker_symbols_by_exchange(runtime_cfg),
                )
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
            return json.loads(
                json.dumps(
                    {
                        "config": market_maker_config_to_dict(
                            runtime_cfg.market_maker
                        ),
                        "market_maker": self._payload.get("market_maker", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def risk_config(
        self,
        base_config: RiskConfig,
    ) -> RiskConfig:
        async with self._lock:
            return replace(base_config, **self._risk_overrides)

    async def runtime_config(self, cfg: BotConfig) -> BotConfig:
        async with self._lock:
            return self._runtime_config_unlocked(cfg)

    async def set_slow_execution_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig | None = None,
    ) -> None:
        async with self._lock:
            self._slow_execution_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg) if cfg else None
            if "slow_execution" in self._payload:
                current_config = self._payload["slow_execution"].get("config", {})
                current_config.update(overrides)
                self._payload["slow_execution"]["config"] = current_config
                if runtime_cfg is not None:
                    self._payload["slow_execution"]["accounts"] = slow_execution_accounts(
                        runtime_cfg.spot_exchanges,
                        _spot_symbols_by_exchange(runtime_cfg),
                    )
            self._save_runtime_store_unlocked()

    async def set_spot_markets(
        self,
        markets: list[SpotMarketConfig],
        *,
        cfg: BotConfig,
    ) -> dict[str, Any]:
        async with self._lock:
            self._spot_markets_override = markets
            runtime_cfg = self._runtime_config_unlocked(cfg)
            symbols_by_exchange = _spot_symbols_by_exchange(runtime_cfg)
            if "config" in self._payload:
                self._payload["config"]["spot_markets"] = spot_markets_to_list(
                    runtime_cfg.spot_markets
                )
                self._payload["config"]["spot_exchanges"] = exchange_configs_to_list(
                    runtime_cfg.spot_exchanges
                )
            if "market_maker" in self._payload:
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    _market_maker_symbols_by_exchange(runtime_cfg),
                )
            if "slow_execution" in self._payload:
                self._payload["slow_execution"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    symbols_by_exchange,
                )
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
            return json.loads(
                json.dumps(
                    {
                        "ok": True,
                        "spot_markets": spot_markets_to_list(runtime_cfg.spot_markets),
                        "market_maker": self._payload.get("market_maker", {}),
                        "slow_execution": self._payload.get("slow_execution", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_cash_and_carry_pairs(
        self,
        pairs: list[CashAndCarryPair],
        *,
        cfg: BotConfig,
    ) -> dict[str, Any]:
        async with self._lock:
            self._cash_and_carry_pairs_override = pairs
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "config" in self._payload:
                self._payload["config"]["cash_and_carry_pairs"] = (
                    cash_and_carry_pairs_to_list(runtime_cfg.cash_and_carry_pairs)
                )
                self._payload["config"]["derivative_exchanges"] = (
                    exchange_configs_to_list(runtime_cfg.derivative_exchanges)
                )
            if "market_maker" in self._payload:
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    _market_maker_symbols_by_exchange(runtime_cfg),
                )
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
            return json.loads(
                json.dumps(
                    {
                        "ok": True,
                        "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                            runtime_cfg.cash_and_carry_pairs
                        ),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_risk_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
    ) -> dict[str, Any]:
        async with self._lock:
            current_risk = self._runtime_config_unlocked(cfg).risk
            for field in ("account_enabled", "strategy_enabled"):
                if field in overrides:
                    merged = dict(getattr(current_risk, field))
                    merged.update(overrides[field])
                    overrides[field] = merged
            self._risk_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
            return json.loads(
                json.dumps(
                    {
                        "risk": risk_config_to_dict(runtime_cfg.risk),
                        "trading_console": self._payload["trading_console"],
                        "operations": self._payload["operations"],
                    }
                )
            )

    async def strategy_pauses(self) -> dict[str, bool]:
        async with self._lock:
            return dict(self._strategy_paused)

    async def set_strategy_paused(
        self,
        strategy_id: str,
        paused: bool,
        *,
        cfg: BotConfig,
    ) -> dict[str, Any]:
        if strategy_id not in STRATEGY_IDS:
            raise ValueError(f"unknown strategy: {strategy_id}")
        async with self._lock:
            self._strategy_paused[strategy_id] = paused
            runtime_cfg = self._runtime_config_unlocked(cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
            return json.loads(json.dumps(self._payload["trading_console"]))

    async def set_running(self, running: bool) -> dict[str, Any]:
        async with self._lock:
            self._program_running = running
            self._program_updated_at = time.time()
            if running:
                self._auto_stopped = False
                self._auto_stop_reason = None
                self._auto_stopped_at = None
                self._payload["status"] = "starting"
                self._payload["warnings"] = ["Resuming scans"]
            else:
                self._auto_stopped = False
                self._auto_stop_reason = None
                self._auto_stopped_at = None
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused"]
            self._payload["program"] = self._program_payload_unlocked()
            self._save_runtime_store_unlocked()
            return json.loads(json.dumps(self._payload))

    async def set_auto_stopped(
        self,
        *,
        reason: str,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._program_running = False
            self._program_updated_at = time.time()
            self._auto_stopped = True
            self._auto_stop_reason = reason
            self._auto_stopped_at = self._program_updated_at
            self._payload["status"] = "auto_stopped"
            self._payload["program"] = self._program_payload_unlocked()
            self._payload["warnings"] = list(warnings or [reason])
            self._save_runtime_store_unlocked()
            return json.loads(json.dumps(self._payload))

    async def set_paused(self) -> None:
        async with self._lock:
            self._payload["program"] = self._program_payload_unlocked()
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                if self._auto_stop_reason:
                    self._payload["warnings"] = [self._auto_stop_reason]
                return
            self._payload["status"] = "paused"
            self._payload["warnings"] = ["Program paused"]

    async def set_order_activity(self, order_activity: dict[str, Any]) -> None:
        async with self._lock:
            self._payload["order_activity"] = order_activity

    async def set_readonly_health(
        self,
        *,
        cfg: BotConfig,
        exec_cfg: SlowExecutionConfig,
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
        warnings: list[str] | None = None,
    ) -> None:
        async with self._lock:
            warning_messages = list(warnings or [])
            trading_console = build_trading_console_payload(
                cfg,
                exec_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=order_activity,
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._payload["account_balances"] = account_balances
            self._payload["order_activity"] = order_activity
            self._payload["trading_console"] = trading_console
            self._payload["readiness"] = build_readiness_payload(
                cfg,
                account_balances=account_balances,
                order_activity=order_activity,
                trading_console=trading_console,
                market_maker=self._payload.get("market_maker", {}),
                slow_execution=self._payload.get("slow_execution", {}),
                markets=self._payload.get("markets", []),
                warnings=warning_messages,
            )
            self._payload["program"] = self._program_payload_unlocked()
            self._payload["runtime_store"] = self._runtime_store_status_unlocked()
            self._payload["operations"] = build_operations_payload(cfg)
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                self._payload["warnings"] = [
                    item
                    for item in [
                        self._auto_stop_reason or "Program auto-stopped",
                        *warning_messages,
                    ]
                    if item
                ]
            elif not self._program_running:
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused", *warning_messages]

    async def set_market_maker_runtime(self, runtime: dict[str, Any]) -> None:
        async with self._lock:
            self._market_maker_runtime = runtime
            if "market_maker" in self._payload:
                self._payload["market_maker"]["runtime"] = runtime
                if isinstance(runtime.get("last_plan"), dict):
                    self._payload["market_maker"]["plan"] = runtime["last_plan"]
                if runtime.get("mode"):
                    self._payload["market_maker"]["mode"] = runtime["mode"]
                if runtime.get("status"):
                    self._payload["market_maker"]["status"] = runtime["status"]
                self._payload["market_maker"]["error"] = runtime.get("last_error")

    async def market_maker_runtime(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._market_maker_runtime))

    async def set_auto_buy_sell_tasks(self, tasks: dict[str, Any]) -> None:
        async with self._lock:
            self._auto_buy_sell_tasks = tasks
            if "slow_execution" in self._payload:
                self._payload["slow_execution"]["tasks"] = tasks

    async def auto_buy_sell_tasks(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._auto_buy_sell_tasks))

    async def set_scan_result(
        self,
        *,
        cfg: BotConfig,
        poll_seconds: float,
        scan_count: int,
        started_at: float,
        elapsed_ms: int,
        markets: list[dict[str, Any]],
        quote_rates: dict[str, float],
        opportunities: list[Opportunity],
        warnings: list[str],
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
        onchain: dict[str, Any],
        market_maker: dict[str, Any],
        slow_execution: dict[str, Any],
        spot_arbitrage: dict[str, Any],
        trading_console: dict[str, Any],
        portfolio: dict[str, Any],
    ) -> None:
        opportunity_dicts = [item.to_dict() for item in opportunities]
        for item in opportunity_dicts:
            self._recent_opportunities.appendleft(item)

        status = "running" if not warnings else "degraded"
        async with self._lock:
            slow_execution["tasks"] = self._auto_buy_sell_tasks
            market_maker["runtime"] = self._market_maker_runtime
            if isinstance(self._market_maker_runtime.get("last_plan"), dict):
                market_maker["plan"] = self._market_maker_runtime["last_plan"]
            if self._market_maker_runtime.get("mode"):
                market_maker["mode"] = self._market_maker_runtime["mode"]
            if self._market_maker_runtime.get("status"):
                market_maker["status"] = self._market_maker_runtime["status"]
            if self._market_maker_runtime.get("last_error"):
                market_maker["error"] = self._market_maker_runtime["last_error"]
            market_maker["quality"] = build_market_maker_quality_payload(
                order_activity,
                market_maker,
                portfolio,
            )
            self._payload = {
                "status": status,
                "config": {
                    "poll_seconds": poll_seconds,
                    "notional_quote": cfg.notional_quote,
                    "min_profit_quote": cfg.min_profit_quote,
                    "min_profit_bps": cfg.min_profit_bps,
                    "common_quote_currency": cfg.common_quote_currency,
                    "spot_markets": spot_markets_to_list(cfg.spot_markets),
                    "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                        cfg.cash_and_carry_pairs
                    ),
                    "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
                    "derivative_exchanges": exchange_configs_to_list(
                        cfg.derivative_exchanges
                    ),
                },
                "scan": {
                    "count": scan_count,
                    "elapsed_ms": elapsed_ms,
                    "last_started": started_at,
                    "last_finished": time.time(),
                },
                "markets": markets,
                "quote_rates": quote_rates,
                "opportunities": opportunity_dicts,
                "recent_opportunities": list(self._recent_opportunities),
                "account_balances": account_balances,
                "order_activity": order_activity,
                "onchain": onchain,
                "market_maker": market_maker,
                "slow_execution": slow_execution,
                "spot_arbitrage": spot_arbitrage,
                "trading_console": trading_console,
                "readiness": build_readiness_payload(
                    cfg,
                    account_balances=account_balances,
                    order_activity=order_activity,
                    trading_console=trading_console,
                    market_maker=market_maker,
                    slow_execution=slow_execution,
                    markets=markets,
                    warnings=warnings,
                ),
                "portfolio": portfolio,
                "program": self._program_payload_unlocked(),
                "runtime_store": self._runtime_store_status_unlocked(),
                "operations": build_operations_payload(cfg),
                "warnings": warnings,
            }

    async def set_error(
        self,
        *,
        cfg: BotConfig,
        poll_seconds: float,
        scan_count: int,
        started_at: float,
        elapsed_ms: int,
        error: str,
    ) -> None:
        async with self._lock:
            self._payload.update(
                {
                    "status": "error",
                    "config": {
                        "poll_seconds": poll_seconds,
                        "notional_quote": cfg.notional_quote,
                        "min_profit_quote": cfg.min_profit_quote,
                        "min_profit_bps": cfg.min_profit_bps,
                        "common_quote_currency": cfg.common_quote_currency,
                        "spot_markets": spot_markets_to_list(cfg.spot_markets),
                        "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                            cfg.cash_and_carry_pairs
                        ),
                        "spot_exchanges": exchange_configs_to_list(
                            cfg.spot_exchanges
                        ),
                        "derivative_exchanges": exchange_configs_to_list(
                            cfg.derivative_exchanges
                        ),
                    },
                    "scan": {
                        "count": scan_count,
                        "elapsed_ms": elapsed_ms,
                        "last_started": started_at,
                        "last_finished": time.time(),
                    },
                    "warnings": [error],
                    "readiness": build_readiness_payload(cfg, warnings=[error]),
                    "program": self._program_payload_unlocked(),
                    "operations": build_operations_payload(cfg),
                }
            )


def _missing_market_warnings(rows: Iterable[dict[str, Any]]) -> list[str]:
    return [
        f"Missing {row['exchange']} {row['symbol']}"
        for row in rows
        if row["status"] != "ok"
    ]


def build_market_maker_safety_payload(
    cfg: BotConfig,
    plan: MarketMakerPlan | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    limits = {
        "max_order_quote": cfg.risk.max_order_quote,
        "max_cycle_quote": cfg.risk.max_cycle_quote,
        "max_orders_per_cycle": cfg.risk.max_orders_per_cycle,
        "max_open_orders": cfg.risk.max_open_orders,
        "max_cancels_per_cycle": cfg.risk.max_cancels_per_cycle,
        "min_seconds_between_cancels": cfg.risk.min_seconds_between_cancels,
        "max_daily_loss_quote": cfg.risk.max_daily_loss_quote,
        "max_exposure_quote": cfg.risk.max_exposure_quote,
        "min_order_book_depth_quote": cfg.risk.min_order_book_depth_quote,
        "max_slippage_bps": cfg.risk.max_slippage_bps,
        "max_order_book_age_seconds": cfg.risk.max_order_book_age_seconds,
        "max_order_book_gap_bps": cfg.risk.max_order_book_gap_bps,
        "max_price_jump_bps": cfg.risk.max_price_jump_bps,
    }
    base_payload: dict[str, Any] = {
        "approved": False,
        "level": "blocked" if error else "disabled",
        "currency": cfg.common_quote_currency,
        "quote_conversion": conversion,
        "limits": limits,
        "order_count": 0,
        "buy_order_count": 0,
        "sell_order_count": 0,
        "total_quote_notional": 0.0,
        "max_order_quote_notional": 0.0,
        "min_order_quote_notional": 0.0,
        "reasons": [error] if error else [],
        "warnings": [],
        "risk": None,
    }
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    quote_values = [
        order.quote_notional * quote_rate_for_risk for order in plan.orders
    ]
    risk_orders = [
        RiskOrder(
            strategy="market_maker",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price * quote_rate_for_risk,
            quote_notional=order.quote_notional * quote_rate_for_risk,
            distance_bps=order.distance_bps,
        )
        for order in plan.orders
    ]
    market = RiskMarketContext(
        exchange=plan.exchange,
        symbol=plan.symbol,
        best_bid=plan.best_bid * quote_rate_for_risk,
        best_ask=plan.best_ask * quote_rate_for_risk,
        mid_price=plan.mid_price * quote_rate_for_risk,
        bid_depth_quote=plan.bid_depth_quote * quote_rate_for_risk,
        ask_depth_quote=plan.ask_depth_quote * quote_rate_for_risk,
        max_level_gap_bps=plan.max_level_gap_bps,
        order_book_timestamp_ms=plan.order_book_timestamp_ms,
        order_book_received_at=plan.order_book_received_at,
    )
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="market_maker",
        live=True,
        existing_spread_bps=plan.existing_spread_bps,
        plan_observed_at=plan.observed_at,
        market=market,
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        post_only=cfg.market_maker.post_only,
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    approved = len(reasons) == 0
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(plan.orders),
        "buy_order_count": sum(1 for order in plan.orders if order.side == "buy"),
        "sell_order_count": sum(1 for order in plan.orders if order.side == "sell"),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": reasons,
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": reasons,
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "existing_spread_bps": plan.existing_spread_bps,
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }


def build_market_maker_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    accounts = slow_execution_accounts(
        _all_account_exchanges(cfg),
        _market_maker_symbols_by_exchange(cfg),
    )
    config_payload = market_maker_config_to_dict(maker_cfg)
    conversion = (
        market_maker_quote_conversion(cfg, maker_cfg.symbol)
        if maker_cfg.symbol
        else {
            "quote_currency": "",
            "common_quote_currency": cfg.common_quote_currency,
            "quote_to_common_rate": None,
            "available": False,
        }
    )
    exchange_cfg = next(
        (
            exchange
            for exchange in _all_account_exchanges(cfg)
            if exchange.key == maker_cfg.exchange
        ),
        None,
    )
    exchange_features = (
        limit_order_features(exchange_cfg).to_dict() if exchange_cfg else {}
    )
    if not maker_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "exchange_features": exchange_features,
            "safety": build_market_maker_safety_payload(cfg, None, conversion),
            "market_data": None,
            "runtime": {},
            "error": None,
        }

    book = books.get((maker_cfg.exchange, maker_cfg.symbol))
    if book is None:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "exchange_features": exchange_features,
            "safety": build_market_maker_safety_payload(
                cfg,
                None,
                conversion,
                error=f"Missing {maker_cfg.exchange} {maker_cfg.symbol}",
            ),
            "market_data": None,
            "runtime": {},
            "error": f"Missing {maker_cfg.exchange} {maker_cfg.symbol}",
        }

    try:
        inventory_base = portfolio_positions_base(cfg.portfolio).get(
            _base_currency_from_symbol(maker_cfg.symbol),
        )
        plan = build_symmetric_market_maker_plan(
            book,
            maker_cfg,
            inventory_base=inventory_base,
        )
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "exchange_features": exchange_features,
            "safety": build_market_maker_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "market_data": order_book_market_data(book),
            "runtime": {},
            "error": str(exc),
        }

    safety = build_market_maker_safety_payload(cfg, plan, conversion)
    return {
        "status": "planned",
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "exchange_features": exchange_features,
        "safety": safety,
        "market_data": order_book_market_data(book),
        "runtime": {},
        "error": None,
    }


def build_slow_execution_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    config_payload = slow_execution_config_to_dict(exec_cfg)
    accounts = slow_execution_accounts(cfg.spot_exchanges, _spot_symbols_by_exchange(cfg))
    if not exec_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": None,
        }

    book = books.get((exec_cfg.exchange, exec_cfg.symbol))
    if book is None:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": f"Missing {exec_cfg.exchange} {exec_cfg.symbol}",
        }

    try:
        plan = build_slow_execution_plan(book, exec_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "error": None,
    }


async def fetch_onchain_payload(
    cfg: BotConfig,
    client: SolanaTokenClient | None,
) -> dict[str, Any]:
    onchain_cfg = cfg.onchain_monitor
    if not onchain_cfg.enabled:
        return {
            "status": "disabled",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "holders": [],
            "history": {
                "enabled": False,
                "path": onchain_cfg.history_path,
                "event_count": 0,
                "recent_events": [],
            },
            "last_finished": None,
            "rpc": {
                "active_url": onchain_cfg.rpc_url,
                "endpoint_count": len(onchain_cfg.rpc_urls or []),
                "env": onchain_cfg.rpc_url_env,
            },
            "error": None,
        }
    if onchain_cfg.network.lower() != "solana":
        return {
            "status": "error",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "holders": [],
            "history": {
                "enabled": False,
                "path": onchain_cfg.history_path,
                "event_count": 0,
                "recent_events": [],
            },
            "last_finished": time.time(),
            "rpc": {
                "active_url": onchain_cfg.rpc_url,
                "endpoint_count": len(onchain_cfg.rpc_urls or []),
                "env": onchain_cfg.rpc_url_env,
            },
            "error": f"Unsupported network: {onchain_cfg.network}",
        }
    if client is None:
        return {
            "status": "error",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "holders": [],
            "history": {
                "enabled": False,
                "path": onchain_cfg.history_path,
                "event_count": 0,
                "recent_events": [],
            },
            "last_finished": time.time(),
            "rpc": {
                "active_url": onchain_cfg.rpc_url,
                "endpoint_count": len(onchain_cfg.rpc_urls or []),
                "env": onchain_cfg.rpc_url_env,
            },
            "error": "Solana client is not configured",
        }

    data = await fetch_top_token_owners(
        client,
        onchain_cfg.token_mint,
        top_n=onchain_cfg.top_n,
    )
    holders = data["holders"]
    labels = onchain_cfg.address_labels
    for holder in holders:
        label = labels.get(holder["owner"])
        holder["label"] = label or "Unknown"
        holder["is_labeled"] = label is not None

    observed_at = time.time()
    history = update_holder_history(
        path=onchain_cfg.history_path,
        mint=onchain_cfg.token_mint,
        label=onchain_cfg.label,
        holders=holders,
        address_labels=labels,
        observed_at=observed_at,
    )
    return {
        "status": "running",
        "label": onchain_cfg.label,
        "mint": onchain_cfg.token_mint,
        "supply": data["supply"],
        "decimals": data["decimals"],
        "holders": holders,
        "history": history,
        "source_account_count": data["source_account_count"],
        "last_finished": observed_at,
        "rpc": {
            "active_url": client.active_rpc_url,
            "endpoint_count": len(client.rpc_urls),
            "env": onchain_cfg.rpc_url_env,
        },
        "error": None,
    }


def _cached_onchain_payload(
    cfg: BotConfig,
    *,
    status: str = "cached",
    error: str | None = None,
) -> dict[str, Any] | None:
    onchain_cfg = cfg.onchain_monitor
    if not onchain_cfg.enabled:
        return None
    snapshot = load_cached_holder_snapshot(
        path=onchain_cfg.history_path,
        mint=onchain_cfg.token_mint,
        label=onchain_cfg.label,
        address_labels=onchain_cfg.address_labels,
        top_n=onchain_cfg.top_n,
    )
    if snapshot is None:
        return None
    return {
        **snapshot,
        "status": status,
        "error": error,
        "rpc": {
            "active_url": onchain_cfg.rpc_url,
            "endpoint_count": len(onchain_cfg.rpc_urls or []),
            "env": onchain_cfg.rpc_url_env,
        },
        "stale": status != "running",
    }


def _onchain_error_payload(
    cfg: BotConfig,
    previous_payload: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    error = str(exc)
    cached = _cached_onchain_payload(cfg, status="error", error=error)
    if cached is not None:
        return cached
    return {
        **previous_payload,
        "status": "error",
        "label": cfg.onchain_monitor.label,
        "mint": cfg.onchain_monitor.token_mint,
        "holders": previous_payload.get("holders", []),
        "history": previous_payload.get(
            "history",
            {
                "enabled": True,
                "path": cfg.onchain_monitor.history_path,
                "event_count": 0,
                "recent_events": [],
            },
        ),
        "last_finished": previous_payload.get("last_finished") or time.time(),
        "error": error,
        "stale": bool(previous_payload.get("holders")),
    }


def _global_scan_health_warnings(
    *,
    onchain_payload: dict[str, Any] | None = None,
    account_balances_payload: dict[str, Any] | None = None,
    order_activity_payload: dict[str, Any] | None = None,
) -> list[str]:
    warnings: list[str] = []
    # On-chain holder monitoring is informational and can be rate-limited by
    # public RPC providers. Keep its error inside the On-chain panel without
    # degrading the trading dashboard's global status.
    _ = onchain_payload
    if (account_balances_payload or {}).get("status") == "error":
        errors = (account_balances_payload or {}).get("errors") or ["unavailable"]
        warnings.append(f"Account balances: {errors[0]}")
    if (order_activity_payload or {}).get("status") == "error":
        errors = (order_activity_payload or {}).get("errors") or ["unavailable"]
        warnings.append(f"Orders: {errors[0]}")
    return warnings


def _parse_daily_report_time(value: str) -> tuple[int, int]:
    hour_text, _, minute_text = value.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text or "0")
    except ValueError:
        return (23, 59)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return (23, 59)
    return (hour, minute)


def _daily_report_due(
    cfg: BotConfig,
    *,
    last_report_day: str | None,
    now: float | None = None,
) -> tuple[bool, str]:
    now = time.time() if now is None else now
    local = time.localtime(now)
    day = time.strftime("%Y-%m-%d", local)
    hour, minute = _parse_daily_report_time(cfg.alerts.daily_report_time)
    due = (
        cfg.alerts.daily_report_enabled
        and day != last_report_day
        and (local.tm_hour, local.tm_min) >= (hour, minute)
    )
    return due, day


def build_daily_report_message(
    cfg: BotConfig,
    *,
    scan_count: int,
    order_activity: dict[str, Any],
    account_balances: dict[str, Any],
    trading_console: dict[str, Any],
    auto_buy_sell_tasks: dict[str, Any],
    warnings: list[str],
) -> str:
    daily = order_activity.get("daily_pnl") or {}
    sources = daily.get("sources") or {}
    source_lines = []
    for source, row in sorted(sources.items()):
        if isinstance(row, dict):
            source_lines.append(
                f"- {source}: P/L {row.get('realized_pnl', 0.0):.8f}, "
                f"trades {row.get('trade_count', 0)}"
            )
    if not source_lines:
        source_lines.append("- no realized fills")

    return "\n".join(
        [
            f"Daily trading report ({time.strftime('%Y-%m-%d')})",
            f"Status scans: {scan_count}",
            f"Daily P/L: {daily.get('total_realized_pnl', 0.0):.8f} {cfg.common_quote_currency}",
            f"Daily trades: {daily.get('trade_count', 0)}",
            f"Open orders: {order_activity.get('open_order_count', 0)}",
            f"Recent fills: {order_activity.get('recent_trade_count', 0)}",
            f"Accounts checked: {account_balances.get('checked_account_count', 0)}/{account_balances.get('total_account_count', 0)}",
            f"Live trading: {trading_console.get('live_trading', False)}",
            f"Auto Buy/Sell tasks: {auto_buy_sell_tasks.get('active_count', 0)} active / {auto_buy_sell_tasks.get('task_count', 0)} total",
            f"Warnings: {len(warnings)}",
            "",
            "P/L by source:",
            *source_lines,
        ]
    )


async def monitor_loop(
    cfg: BotConfig,
    strategy: StrategyName,
    state: MonitorState,
    poll_seconds: float,
) -> None:
    manager = ExchangeManager()
    solana_client = (
        SolanaTokenClient(cfg.onchain_monitor.rpc_urls or cfg.onchain_monitor.rpc_url)
        if cfg.onchain_monitor.enabled
        else None
    )
    onchain_payload = (
        _cached_onchain_payload(cfg)
        or _build_initial_payload(cfg, poll_seconds)["onchain"]
    )
    account_balances_payload = _build_initial_payload(cfg, poll_seconds)[
        "account_balances"
    ]
    order_activity_payload = _build_initial_payload(cfg, poll_seconds)[
        "order_activity"
    ]
    trading_console_payload = _build_initial_payload(cfg, poll_seconds)[
        "trading_console"
    ]
    market_maker_payload = _build_initial_payload(cfg, poll_seconds)["market_maker"]
    slow_execution_payload = _build_initial_payload(cfg, poll_seconds)[
        "slow_execution"
    ]
    spot_arbitrage_payload = _build_initial_payload(cfg, poll_seconds)[
        "spot_arbitrage"
    ]
    portfolio_payload = _build_initial_payload(cfg, poll_seconds)["portfolio"]
    alert_service = AlertService(cfg.alerts)
    next_onchain_scan = 0.0
    next_balance_scan = 0.0
    next_order_activity_scan = 0.0
    consecutive_problem_cycles = 0
    last_daily_report_day: str | None = None
    last_spot_arbitrage_execution_at = 0.0
    last_spot_arbitrage_timeline_fingerprint = ""
    scan_count = 0
    loop_started_monotonic = time.monotonic()
    try:
        while True:
            if not await state.is_running():
                now = time.monotonic()
                if now >= next_balance_scan or now >= next_order_activity_scan:
                    runtime_cfg = await state.runtime_config(cfg)
                    runtime_slow_execution = runtime_cfg.slow_execution
                    readonly_warnings: list[str] = []
                    if now >= next_balance_scan:
                        try:
                            account_balances_payload = await fetch_account_balances_payload(
                                runtime_cfg,
                                manager,
                                runtime_slow_execution,
                            )
                        except Exception as exc:  # noqa: BLE001
                            account_balances_payload = {
                                "status": "error",
                                "accounts": [],
                                "totals": [],
                                "checked_account_count": 0,
                                "total_account_count": len(
                                    _all_account_exchanges(runtime_cfg)
                                ),
                                "last_finished": time.time(),
                                "errors": [str(exc)],
                            }
                        next_balance_scan = now + ACCOUNT_BALANCE_POLL_SECONDS
                    if now >= next_order_activity_scan:
                        try:
                            auto_tasks_snapshot = await state.auto_buy_sell_tasks()
                            market_maker_runtime_snapshot = (
                                await state.market_maker_runtime()
                            )
                            order_activity_payload = await fetch_order_activity_payload(
                                runtime_cfg,
                                manager,
                                runtime_slow_execution,
                                quote_rates=runtime_cfg.quote_rates,
                                books={},
                                market_maker_runtime=market_maker_runtime_snapshot,
                                auto_buy_sell_tasks=auto_tasks_snapshot,
                            )
                        except Exception as exc:  # noqa: BLE001
                            order_activity_payload = _build_initial_payload(
                                runtime_cfg,
                                poll_seconds,
                            )["order_activity"]
                            order_activity_payload.update(
                                {
                                    "status": "error",
                                    "last_finished": time.time(),
                                    "errors": [str(exc)],
                                }
                            )
                            order_activity_payload["reconciliation"] = {
                                **order_activity_payload.get("reconciliation", {}),
                                "status": "error",
                                "issue_count": 1,
                                "notice_count": 0,
                                "total_item_count": 1,
                                "level_counts": {"error": 1, "warning": 0, "info": 0},
                                "critical_issue_count": 1,
                                "auto_stop_recommended": True,
                                "auto_stop_reasons": [
                                    f"order_activity_error: {str(exc)}"
                                ],
                                "issues": [
                                    {
                                        "level": "error",
                                        "type": "order_activity_error",
                                        "strategy": "",
                                        "exchange": "",
                                        "symbol": "",
                                        "order_id": "",
                                        "source_id": "",
                                        "message": str(exc),
                                    }
                                ],
                                "checked_at": time.time(),
                            }
                        next_order_activity_scan = (
                            now + ORDER_ACTIVITY_POLL_SECONDS
                        )
                    if account_balances_payload.get("status") == "error":
                        errors = account_balances_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Account balances: {errors[0]}")
                    if order_activity_payload.get("status") == "error":
                        errors = order_activity_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Orders: {errors[0]}")
                    await state.set_readonly_health(
                        cfg=runtime_cfg,
                        exec_cfg=runtime_slow_execution,
                        account_balances=account_balances_payload,
                        order_activity=order_activity_payload,
                        warnings=readonly_warnings,
                    )
                else:
                    await state.set_paused()
                await asyncio.sleep(0.5)
                continue

            monotonic_started = time.monotonic()
            started_at = time.time()
            scan_count += 1
            runtime_cfg = cfg
            try:
                runtime_cfg = await state.runtime_config(cfg)
                runtime_slow_execution = runtime_cfg.slow_execution
                strategy_pauses = await state.strategy_pauses()
                spot_arbitrage_payload = {
                    "type": "spot_spread_execution",
                    "strategy": "spot_spread",
                    "status": "disabled",
                    "mode": "dry_run",
                    "plan": None,
                    "risk": None,
                    "execution": None,
                    "error": None,
                    "cooldown_seconds": SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
                }
                portfolio_books: dict[tuple[str, str], OrderBookSnapshot] = {}
                if strategy in {"all", "spot-spread"} and runtime_cfg.spot_markets:
                    symbols_by_exchange = _symbols_for_configured_spot_markets(
                        runtime_cfg
                    )
                    if (
                        runtime_slow_execution.enabled
                        and runtime_slow_execution.exchange
                        and runtime_slow_execution.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_slow_execution.exchange,
                            set(),
                        ).add(runtime_slow_execution.symbol)
                    books = await manager.fetch_order_books(
                        runtime_cfg.spot_exchanges,
                        symbols_by_exchange,
                        runtime_cfg.order_book_depth,
                    )
                    derivative_keys = {
                        exchange.key for exchange in runtime_cfg.derivative_exchanges
                    }
                    if (
                        runtime_cfg.market_maker.enabled
                        and runtime_cfg.market_maker.exchange in derivative_keys
                        and runtime_cfg.market_maker.symbol
                    ):
                        derivative_books = await manager.fetch_order_books(
                            runtime_cfg.derivative_exchanges,
                            {
                                runtime_cfg.market_maker.exchange: {
                                    runtime_cfg.market_maker.symbol
                                }
                            },
                            runtime_cfg.order_book_depth,
                        )
                        books.update(derivative_books)
                    portfolio_books = books
                    quote_rates = _quote_rates_from_sources(runtime_cfg, books)
                    rows = build_market_rows(
                        runtime_cfg.spot_markets,
                        books,
                        quote_rates,
                    )
                    if strategy_pauses.get("spot_spread", False):
                        opportunities = []
                    else:
                        opportunities = find_converted_spot_spread_opportunities(
                            books=books,
                            exchanges=runtime_cfg.spot_exchanges,
                            markets=runtime_cfg.spot_markets,
                            notional_quote=runtime_cfg.notional_quote,
                            min_profit_quote=runtime_cfg.min_profit_quote,
                            min_profit_bps=runtime_cfg.min_profit_bps,
                            quote_rates=quote_rates,
                            common_quote_currency=runtime_cfg.common_quote_currency,
                        )
                    extra_warnings: list[str] = []
                    if (
                        strategy == "all"
                        and runtime_cfg.cash_and_carry_pairs
                        and not strategy_pauses.get("cash_and_carry", False)
                    ):
                        try:
                            opportunities.extend(
                                await scan_with_manager(
                                    runtime_cfg,
                                    "cash-and-carry",
                                    manager,
                                )
                            )
                            opportunities.sort(
                                key=lambda item: item.profit_bps,
                                reverse=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            extra_warnings.append(
                                f"Cash & carry scan failed: {exc.__class__.__name__}: {exc}"
                            )
                    warnings = [*_missing_market_warnings(rows), *extra_warnings]
                    if strategy_pauses.get("market_maker", False):
                        market_maker_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "error": None,
                        }
                    else:
                        market_maker_payload = build_market_maker_payload(
                            runtime_cfg,
                            books,
                        )
                    if strategy_pauses.get("slow_execution", False):
                        slow_execution_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "config": slow_execution_config_to_dict(
                                runtime_slow_execution
                            ),
                            "accounts": slow_execution_accounts(
                                runtime_cfg.spot_exchanges,
                                _spot_symbols_by_exchange(runtime_cfg),
                            ),
                            "error": None,
                        }
                    else:
                        slow_execution_payload = build_slow_execution_payload(
                            runtime_cfg,
                            books,
                            runtime_slow_execution,
                        )
                    portfolio_payload = build_portfolio_pnl(
                        runtime_cfg,
                        books,
                        quote_rates,
                    )
                    spot_live_allowed = (
                        runtime_cfg.risk.enabled
                        and runtime_cfg.risk.trading_enabled
                        and runtime_cfg.risk.allow_live_trading
                        and runtime_cfg.risk.strategy_enabled.get("spot_spread", True)
                    )
                    if strategy_pauses.get("spot_spread", False):
                        spot_arbitrage_payload = {
                            **spot_arbitrage_payload,
                            "status": "paused",
                            "mode": "paused",
                        }
                    elif not opportunities:
                        spot_arbitrage_payload = {
                            **spot_arbitrage_payload,
                            "status": "no_opportunity",
                            "mode": "live" if spot_live_allowed else "dry_run",
                        }
                    elif not spot_live_allowed:
                        spot_arbitrage_payload = {
                            **spot_arbitrage_payload,
                            "status": "live_disabled",
                            "mode": "dry_run",
                            "opportunity": opportunities[0].to_dict(),
                        }
                    else:
                        cooldown_remaining = (
                            SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS
                            - (time.monotonic() - last_spot_arbitrage_execution_at)
                        )
                        if cooldown_remaining > 0:
                            spot_arbitrage_payload = {
                                **spot_arbitrage_payload,
                                "status": "cooldown",
                                "mode": "live",
                                "opportunity": opportunities[0].to_dict(),
                                "cooldown_remaining_seconds": cooldown_remaining,
                            }
                        else:
                            spot_arbitrage_payload = await run_spot_arbitrage_execution_cycle(
                                runtime_cfg,
                                manager,
                                opportunities=opportunities,
                                books=books,
                                quote_rates=quote_rates,
                                live=True,
                            )
                            write_trade_event(runtime_cfg.trade_log, spot_arbitrage_payload)
                            if spot_arbitrage_payload.get("status") != "no_opportunity":
                                last_spot_arbitrage_execution_at = time.monotonic()
                    timeline_event = strategy_timeline_event_from_payload(
                        spot_arbitrage_payload,
                        source="monitor",
                    )
                    timeline_fingerprint = strategy_timeline_fingerprint(
                        timeline_event
                    )
                    if timeline_fingerprint != last_spot_arbitrage_timeline_fingerprint:
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            spot_arbitrage_payload,
                            source="monitor",
                        )
                        last_spot_arbitrage_timeline_fingerprint = (
                            timeline_fingerprint
                        )
                else:
                    opportunities = await scan_with_manager(
                        runtime_cfg,
                        strategy,
                        manager,
                    )
                    rows = []
                    quote_rates = runtime_cfg.quote_rates
                    warnings = []
                    market_maker_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "error": None,
                    }
                    slow_execution_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": slow_execution_config_to_dict(
                            runtime_slow_execution
                        ),
                        "accounts": slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _spot_symbols_by_exchange(runtime_cfg),
                        ),
                        "error": None,
                    }
                    spot_arbitrage_payload = {
                        **spot_arbitrage_payload,
                        "status": "disabled",
                    }
                    portfolio_payload = _build_initial_payload(
                        runtime_cfg,
                        poll_seconds,
                    )["portfolio"]

                now = time.monotonic()
                if now >= next_balance_scan:
                    try:
                        account_balances_payload = await fetch_account_balances_payload(
                            runtime_cfg,
                            manager,
                            runtime_slow_execution,
                        )
                    except Exception as exc:  # noqa: BLE001
                        account_balances_payload = {
                            "status": "error",
                            "accounts": [],
                            "totals": [],
                            "checked_account_count": 0,
                            "total_account_count": len(
                                _all_account_exchanges(runtime_cfg)
                            ),
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                        }
                    next_balance_scan = now + ACCOUNT_BALANCE_POLL_SECONDS

                if now >= next_order_activity_scan:
                    try:
                        auto_tasks_snapshot = await state.auto_buy_sell_tasks()
                        market_maker_runtime_snapshot = await state.market_maker_runtime()
                        order_activity_payload = await fetch_order_activity_payload(
                            runtime_cfg,
                            manager,
                            runtime_slow_execution,
                            quote_rates=quote_rates,
                            books=portfolio_books,
                            market_maker_runtime=market_maker_runtime_snapshot,
                            auto_buy_sell_tasks=auto_tasks_snapshot,
                        )
                    except Exception as exc:  # noqa: BLE001
                        order_activity_payload = {
                            "status": "error",
                            "accounts": [],
                            "open_orders": [],
                            "closed_orders": [],
                            "recent_trades": [],
                            "pnl_summary": {
                                "currency": runtime_cfg.common_quote_currency,
                                "window": "recent_fills",
                                "trade_count": 0,
                                "attributed_trade_count": 0,
                                "unattributed_trade_count": 0,
                                "total_realized_pnl": 0.0,
                                "total_fees": 0.0,
                                "total_notional": 0.0,
                                "sources": {},
                                "missing_cost_basis": [],
                                "missing_quote_rates": [],
                                "missing_fee_rates": [],
                                "observed_at": None,
                            },
                            "pnl_store": {
                                "enabled": runtime_cfg.pnl_store.enabled,
                                "path": runtime_cfg.pnl_store.path,
                                "stored_fill_count": 0,
                                "daily": None,
                            },
                            "daily_pnl": {
                                "enabled": runtime_cfg.pnl_store.enabled,
                                "path": runtime_cfg.pnl_store.path,
                                "day": None,
                                "currency": runtime_cfg.common_quote_currency,
                                "trade_count": 0,
                                "total_realized_pnl": 0.0,
                                "total_fees": 0.0,
                                "total_notional": 0.0,
                                "sources": {},
                                "updated_at": None,
                            },
                            "open_order_count": 0,
                            "closed_order_count": 0,
                            "recent_trade_count": 0,
                            "reconciliation": {
                                "status": "error",
                                "tracked_order_count": 0,
                                "matched_open_count": 0,
                                "matched_fill_count": 0,
                                "untracked_open_count": 0,
                                "unattributed_fill_count": 0,
                                "issue_count": 1,
                                "notice_count": 0,
                                "total_item_count": 1,
                                "level_counts": {"error": 1, "warning": 0, "info": 0},
                                "critical_issue_count": 1,
                                "auto_stop_recommended": True,
                                "auto_stop_reasons": [
                                    f"order_activity_error: {str(exc)}"
                                ],
                                "issues": [
                                    {
                                        "level": "error",
                                        "type": "order_activity_error",
                                        "strategy": "",
                                        "exchange": "",
                                        "symbol": "",
                                        "order_id": "",
                                        "source_id": "",
                                        "message": str(exc),
                                    }
                                ],
                                "checked_at": time.time(),
                            },
                            "checked_account_count": 0,
                            "total_account_count": len(
                                _all_account_exchanges(runtime_cfg)
                            ),
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                            "warnings": [],
                        }
                    next_order_activity_scan = now + ORDER_ACTIVITY_POLL_SECONDS

                if portfolio_books:
                    portfolio_payload = build_synced_portfolio_pnl(
                        runtime_cfg,
                        portfolio_books,
                        quote_rates,
                        account_balances_payload,
                        order_activity_payload,
                    )

                trading_console_payload = build_trading_console_payload(
                    runtime_cfg,
                    runtime_slow_execution,
                    strategy_paused=strategy_pauses,
                    order_activity=order_activity_payload,
                    auto_buy_sell_tasks=await state.auto_buy_sell_tasks(),
                )

                if runtime_cfg.onchain_monitor.enabled and now >= next_onchain_scan:
                    try:
                        onchain_payload = await fetch_onchain_payload(
                            runtime_cfg,
                            solana_client,
                        )
                    except Exception as exc:  # noqa: BLE001
                        onchain_payload = _onchain_error_payload(
                            runtime_cfg,
                            onchain_payload,
                            exc,
                        )
                    next_onchain_scan = (
                        now + max(1.0, runtime_cfg.onchain_monitor.poll_seconds)
                    )

                warnings = [
                    *warnings,
                    *_global_scan_health_warnings(
                        onchain_payload=onchain_payload,
                        account_balances_payload=account_balances_payload,
                        order_activity_payload=order_activity_payload,
                    ),
                ]
                reconciliation_payload = (
                    order_activity_payload.get("reconciliation")
                    if isinstance(order_activity_payload.get("reconciliation"), dict)
                    else {}
                )
                reconciliation_stop_requested = bool(
                    reconciliation_payload.get("auto_stop_recommended")
                )
                reconciliation_reasons = [
                    str(reason)
                    for reason in reconciliation_payload.get("auto_stop_reasons", [])
                    if reason
                ]
                program_updated_at = await state.program_updated_at()
                reconciliation_warmup_active = (
                    reconciliation_stop_requested
                    and _monitor_reconciliation_warmup_active(
                        process_uptime_seconds=(
                            monotonic_started - loop_started_monotonic
                        ),
                        program_age_seconds=started_at - program_updated_at,
                    )
                )
                reconciliation_stop = (
                    reconciliation_stop_requested and not reconciliation_warmup_active
                )
                if reconciliation_warmup_active:
                    reconciliation_payload["auto_stop_warmup_active"] = True
                    reconciliation_payload["auto_stop_suppressed"] = True
                    reconciliation_payload["auto_stop_warmup_seconds"] = (
                        RECONCILIATION_AUTO_STOP_WARMUP_SECONDS
                    )
                if reconciliation_stop:
                    warnings = [
                        *warnings,
                        "Reconciliation: " + reconciliation_reasons[0]
                        if reconciliation_reasons
                        else "Reconciliation has critical order state issues",
                    ]
                if market_maker_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Market maker: {market_maker_payload.get('error')}",
                    ]
                if slow_execution_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Auto Buy/Sell: {slow_execution_payload.get('error')}",
                    ]
                if spot_arbitrage_payload.get("status") in {
                    "blocked_by_plan",
                    "blocked_by_risk",
                    "blocked_by_slippage",
                    "blocked_by_validation",
                    "blocked_by_balance",
                    "execution_error",
                    "hedge_required",
                }:
                    reason = ""
                    risk_payload = spot_arbitrage_payload.get("risk")
                    if isinstance(risk_payload, dict):
                        reasons = risk_payload.get("reasons")
                        if isinstance(reasons, list) and reasons:
                            reason = str(reasons[0])
                    errors = spot_arbitrage_payload.get("errors")
                    if not reason and isinstance(errors, list) and errors:
                        reason = str(errors[0])
                    if not reason:
                        reason = str(spot_arbitrage_payload.get("status"))
                    warnings = [*warnings, f"Spot arbitrage: {reason}"]
                daily_loss_stop = False
                if runtime_cfg.risk.max_daily_loss_quote > 0:
                    daily_pnl_quote = current_daily_pnl_quote(runtime_cfg)
                    if daily_pnl_quote <= -runtime_cfg.risk.max_daily_loss_quote:
                        daily_loss_stop = True
                        warnings = [
                            *warnings,
                            (
                                f"Daily loss {daily_pnl_quote:.8f} exceeds "
                                f"max_daily_loss_quote {runtime_cfg.risk.max_daily_loss_quote:.8f}"
                            ),
                        ]

                consecutive_problem_cycles = (
                    consecutive_problem_cycles + 1 if warnings else 0
                )
                auto_stop_triggered, auto_stop_reason = _monitor_auto_stop_decision(
                    auto_stop_enabled=runtime_cfg.alerts.auto_stop_enabled,
                    auto_stop_consecutive_errors=(
                        runtime_cfg.alerts.auto_stop_consecutive_errors
                    ),
                    daily_loss_stop=daily_loss_stop,
                    reconciliation_stop=reconciliation_stop,
                    consecutive_problem_cycles=consecutive_problem_cycles,
                )
                if auto_stop_triggered:
                    warnings = [
                        *warnings,
                        (
                            "Auto-stop triggered after "
                            f"{consecutive_problem_cycles} problem cycle(s)"
                        ),
                    ]

                elapsed = time.monotonic() - monotonic_started
                if not await state.is_running():
                    await state.set_paused()
                    continue
                await state.set_scan_result(
                    cfg=runtime_cfg,
                    poll_seconds=poll_seconds,
                    scan_count=scan_count,
                    started_at=started_at,
                    elapsed_ms=int(elapsed * 1000),
                    markets=rows,
                    quote_rates=quote_rates,
                    opportunities=opportunities,
                    warnings=warnings,
                    account_balances=account_balances_payload,
                    order_activity=order_activity_payload,
                    onchain=onchain_payload,
                    market_maker=market_maker_payload,
                    slow_execution=slow_execution_payload,
                    spot_arbitrage=spot_arbitrage_payload,
                    trading_console=trading_console_payload,
                    portfolio=portfolio_payload,
                )
                if warnings:
                    await alert_service.send(
                        level="critical" if auto_stop_triggered else "warning",
                        title="Crypto arbitrage monitor warning",
                        message="\n".join(warnings[:6]),
                        key="monitor:warnings:" + "|".join(warnings[:3]),
                        payload={
                            "status": "auto_stopped" if auto_stop_triggered else "degraded",
                            "scan_count": scan_count,
                            "warnings": warnings,
                        },
                    )
                due, report_day = _daily_report_due(
                    runtime_cfg,
                    last_report_day=last_daily_report_day,
                )
                if due:
                    auto_tasks = await state.auto_buy_sell_tasks()
                    await alert_service.send(
                        level="info",
                        title="Daily trading report",
                        message=build_daily_report_message(
                            runtime_cfg,
                            scan_count=scan_count,
                            order_activity=order_activity_payload,
                            account_balances=account_balances_payload,
                            trading_console=trading_console_payload,
                            auto_buy_sell_tasks=auto_tasks,
                            warnings=warnings,
                        ),
                        key=f"daily-report:{report_day}",
                        payload={
                            "daily_pnl": order_activity_payload.get("daily_pnl"),
                            "account_balances": account_balances_payload,
                            "auto_buy_sell_tasks": auto_tasks,
                        },
                        force=True,
                    )
                    last_daily_report_day = report_day
                if auto_stop_triggered:
                    await state.set_auto_stopped(
                        reason=auto_stop_reason,
                        warnings=warnings,
                    )
                    write_system_web_audit_event(
                        runtime_cfg,
                        action="auto_stop",
                        target="program",
                        detail=auto_stop_reason,
                        payload={
                            "scan_count": scan_count,
                            "warnings": warnings,
                            "daily_loss_stop": daily_loss_stop,
                            "reconciliation_stop_requested": (
                                reconciliation_stop_requested
                            ),
                            "reconciliation_stop": reconciliation_stop,
                            "reconciliation_warmup_active": (
                                reconciliation_warmup_active
                            ),
                            "reconciliation_reasons": reconciliation_reasons,
                            "consecutive_problem_cycles": consecutive_problem_cycles,
                        },
                    )
                    await alert_service.send(
                        level="critical",
                        title="Crypto arbitrage auto-stopped",
                        message="\n".join(warnings[:8]),
                        key="monitor:auto-stop",
                        payload={
                            "scan_count": scan_count,
                            "warnings": warnings,
                        },
                        force=True,
                    )
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - monotonic_started
                consecutive_problem_cycles += 1
                await state.set_error(
                    cfg=runtime_cfg,
                    poll_seconds=poll_seconds,
                    scan_count=scan_count,
                    started_at=started_at,
                    elapsed_ms=int(elapsed * 1000),
                    error=str(exc),
                )
                await alert_service.send(
                    level="error",
                    title="Crypto arbitrage monitor error",
                    message=f"{exc.__class__.__name__}: {exc}",
                    key=f"monitor:error:{exc.__class__.__name__}:{exc}",
                    payload={
                        "scan_count": scan_count,
                        "error": str(exc),
                    },
                )
                if (
                    runtime_cfg.alerts.auto_stop_enabled
                    and consecutive_problem_cycles
                    >= max(1, runtime_cfg.alerts.auto_stop_consecutive_errors)
                ):
                    auto_stop_reason = (
                        f"monitor exception after {consecutive_problem_cycles} "
                        f"consecutive error cycle(s)"
                    )
                    await state.set_auto_stopped(
                        reason=auto_stop_reason,
                        warnings=[f"{auto_stop_reason}: {exc}"],
                    )
                    write_system_web_audit_event(
                        runtime_cfg,
                        action="auto_stop",
                        target="program",
                        detail=auto_stop_reason,
                        payload={
                            "scan_count": scan_count,
                            "error": str(exc),
                            "consecutive_problem_cycles": consecutive_problem_cycles,
                        },
                    )
                    await alert_service.send(
                        level="critical",
                        title="Crypto arbitrage auto-stopped",
                        message=(
                            f"Stopped after {consecutive_problem_cycles} "
                            f"consecutive error cycle(s): {exc}"
                        ),
                        key="monitor:auto-stop",
                        payload={"scan_count": scan_count, "error": str(exc)},
                        force=True,
                    )

            sleep_for = max(0.0, poll_seconds - (time.monotonic() - monotonic_started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()
        if solana_client is not None:
            await solana_client.close()


async def auto_buy_sell_task_loop(
    cfg: BotConfig,
    state: MonitorState,
    tasks: AutoBuySellTaskService,
) -> None:
    manager = ExchangeManager()
    try:
        await state.set_auto_buy_sell_tasks(await tasks.snapshot())
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            strategy_pauses = await state.strategy_pauses()
            payload = await tasks.run_due_tasks(
                runtime_cfg,
                manager,
                strategy_paused=strategy_pauses.get("slow_execution", False),
                program_running=await state.is_running(),
            )
            await state.set_auto_buy_sell_tasks(payload)
            await asyncio.sleep(1.0)
    finally:
        await manager.close()


def _raw_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("order") or "")


def _raw_client_order_id(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    candidates = (
        raw.get("clientOrderId"),
        raw.get("client_order_id"),
        raw.get("clientOid"),
        info.get("clientOrderId"),
        info.get("client_order_id"),
        info.get("client_oid"),
    )
    for value in candidates:
        if value:
            return str(value)
    return ""


async def _market_maker_open_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    current_ids: list[str],
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    fallback_ids = sorted({order_id for order_id in current_ids if order_id})
    if not maker_cfg.exchange or not maker_cfg.symbol:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": None,
        }
    exchange = next(
        (
            item
            for item in _all_account_exchanges(cfg)
            if item.key == maker_cfg.exchange
        ),
        None,
    )
    if exchange is None:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"market maker exchange is not configured: {maker_cfg.exchange}",
        }
    try:
        open_orders = await manager.fetch_open_orders(exchange, symbol=maker_cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    order_ids: set[str] = set()
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        order_id = _raw_order_id(order)
        if order_id:
            order_ids.add(order_id)
    return {
        "source": "exchange",
        "order_ids": sorted(order_ids),
        "open_orders": [order for order in open_orders if isinstance(order, dict)],
        "open_order_count": len(open_orders),
        "error": None,
    }


def _market_maker_order_sync_delta(
    previous_order_ids: list[str],
    open_order_snapshot: dict[str, Any],
) -> dict[str, Any]:
    previous_ids = {str(order_id) for order_id in previous_order_ids if order_id}
    snapshot_ids = {
        str(order_id)
        for order_id in open_order_snapshot.get("order_ids", []) or []
        if order_id
    }
    source = str(open_order_snapshot.get("source") or "memory")
    exchange_confirmed = source == "exchange" and not open_order_snapshot.get("error")
    missing_tracked_ids = sorted(previous_ids - snapshot_ids) if exchange_confirmed else []
    new_exchange_ids = sorted(snapshot_ids - previous_ids) if exchange_confirmed else []
    changed = bool(
        exchange_confirmed
        and previous_ids
        and (missing_tracked_ids or new_exchange_ids)
    )
    return {
        "source": source,
        "exchange_confirmed": exchange_confirmed,
        "tracked_before_sync": sorted(previous_ids),
        "exchange_order_ids": sorted(snapshot_ids),
        "missing_tracked_order_ids": missing_tracked_ids,
        "new_exchange_order_ids": new_exchange_ids,
        "changed": changed,
        "checked_at": time.time(),
    }


def _market_maker_force_replace_reason(
    open_order_ids: list[str],
    previous_plan: dict[str, Any] | None,
    *,
    order_sync: dict[str, Any] | None = None,
) -> str | None:
    if order_sync and order_sync.get("changed"):
        return "exchange open orders differ from tracked MM ids; assuming fill/cancel drift"
    if not open_order_ids or not previous_plan:
        return None
    previous_orders = previous_plan.get("orders")
    if not isinstance(previous_orders, list):
        return None
    if len(open_order_ids) != len(previous_orders):
        return "open order count differs from previous MM plan; assuming fill/cancel drift"
    return None


def _market_maker_should_force_replace(
    open_order_ids: list[str],
    previous_plan: dict[str, Any] | None,
    *,
    order_sync: dict[str, Any] | None = None,
) -> bool:
    return (
        _market_maker_force_replace_reason(
            open_order_ids,
            previous_plan,
            order_sync=order_sync,
        )
        is not None
    )



def _market_maker_gate_status(
    cfg: BotConfig,
    *,
    strategy_paused: bool,
    program_running: bool,
) -> tuple[bool, str, str]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.enabled:
        return False, "disabled", "market_maker.enabled is false"
    if not maker_cfg.live_enabled:
        return False, "dry_run", "market_maker.live_enabled is false"
    if not program_running:
        return False, "program_paused", "program is paused"
    if strategy_paused:
        return False, "paused", "market_maker strategy is paused"
    if not cfg.risk.enabled or not cfg.risk.trading_enabled:
        return False, "blocked_by_risk", "risk trading is disabled"
    if not cfg.risk.allow_live_trading:
        return False, "blocked_by_risk", "risk.allow_live_trading is false"
    if not cfg.risk.allow_market_maker or not _risk_strategy_enabled(
        cfg,
        "market_maker",
    ):
        return False, "blocked_by_risk", "market_maker strategy is disabled"
    if maker_cfg.exchange and not _risk_account_enabled(cfg, maker_cfg.exchange):
        return False, "blocked_by_risk", f"{maker_cfg.exchange} account is disabled"
    return True, "live", "live"


def _market_maker_cache_max_age_seconds(cfg: BotConfig) -> float:
    poll_age = max(1.0, cfg.market_maker.poll_seconds * 2)
    risk_age = cfg.risk.max_order_book_age_seconds
    if risk_age > 0:
        return min(risk_age, poll_age)
    return poll_age


async def _cached_market_maker_order_book(
    cfg: BotConfig,
    cache: OrderBookCache,
) -> tuple[OrderBookSnapshot | None, dict[str, Any]]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.exchange or not maker_cfg.symbol:
        return None, {}
    max_age_seconds = _market_maker_cache_max_age_seconds(cfg)
    try:
        exchange_cfg = _find_exchange_by_key(cfg, maker_cfg.exchange)
        depth = max(cfg.order_book_depth, maker_cfg.levels)
        await cache.ensure_watch(exchange_cfg, maker_cfg.symbol, depth)
        snapshot = cache.get(
            maker_cfg.exchange,
            maker_cfg.symbol,
            max_age_seconds=max_age_seconds,
        )
        status = cache.status(
            maker_cfg.exchange,
            maker_cfg.symbol,
            max_age_seconds=max_age_seconds,
        )
        status["using_cached"] = snapshot is not None
        return snapshot, status
    except Exception as exc:  # noqa: BLE001
        return None, {
            "exchange": maker_cfg.exchange,
            "symbol": maker_cfg.symbol,
            "source": None,
            "fresh": False,
            "using_cached": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


async def market_maker_task_loop(
    cfg: BotConfig,
    state: MonitorState,
) -> None:
    manager = ExchangeManager()
    orderbook_cache = OrderBookCache(manager)
    open_order_ids: list[str] = []
    open_order_exchange = ""
    open_order_symbol = ""
    placed_count = 0
    canceled_count = 0
    cycle_count = 0
    last_cancel_at: float | None = None
    previous_mid_price: float | None = None
    previous_plan: dict[str, Any] | None = None
    runtime: dict[str, Any] = {
        "status": "starting",
        "mode": "dry_run",
        "open_order_ids": [],
        "open_order_exchange": "",
        "open_order_symbol": "",
        "open_order_count": 0,
        "placed_count": 0,
        "canceled_count": 0,
        "cycle_count": 0,
        "last_error": None,
        "market_data": None,
        "open_order_sync": None,
        "updated_at": time.time(),
    }
    try:
        await state.set_market_maker_runtime(runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            maker_cfg = runtime_cfg.market_maker
            interval = max(1.0, maker_cfg.poll_seconds)
            started = time.monotonic()
            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            live_allowed, status, reason = _market_maker_gate_status(
                runtime_cfg,
                strategy_paused=strategy_pauses.get("market_maker", False),
                program_running=program_running,
            )
            try:
                current_tracking_key = (maker_cfg.exchange, maker_cfg.symbol)
                previous_tracking_key = (open_order_exchange, open_order_symbol)
                if (
                    open_order_ids
                    and previous_tracking_key != ("", "")
                    and previous_tracking_key != current_tracking_key
                ):
                    cancel_cfg = replace(
                        runtime_cfg,
                        market_maker=replace(
                            runtime_cfg.market_maker,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                    )
                    cancel_payload = await cancel_market_maker_order_ids(
                        cancel_cfg,
                        manager,
                        open_order_ids,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    if cancel_payload.get("canceled_count"):
                        last_cancel_at = time.time()
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="market_maker_task",
                    )
                    open_order_ids = []
                    open_order_exchange = ""
                    open_order_symbol = ""
                    previous_plan = None
                    previous_mid_price = None

                open_order_sync: dict[str, Any] | None = None
                if live_allowed or open_order_ids:
                    tracked_before_sync = list(open_order_ids)
                    open_order_snapshot = await _market_maker_open_order_snapshot(
                        runtime_cfg,
                        manager,
                        open_order_ids,
                    )
                    open_order_sync = _market_maker_order_sync_delta(
                        tracked_before_sync,
                        open_order_snapshot,
                    )
                    open_order_ids = [
                        str(order_id)
                        for order_id in open_order_snapshot.get("order_ids", [])
                        if order_id
                    ]
                    if open_order_ids:
                        open_order_exchange = maker_cfg.exchange
                        open_order_symbol = maker_cfg.symbol
                else:
                    open_order_snapshot = {
                        "source": "memory",
                        "order_ids": open_order_ids,
                        "open_orders": [],
                        "open_order_count": len(open_order_ids),
                        "error": None,
                    }
                    open_order_sync = _market_maker_order_sync_delta(
                        open_order_ids,
                        open_order_snapshot,
                    )
                if not live_allowed:
                    cancel_payload = None
                    if open_order_ids:
                        cancel_payload = await cancel_market_maker_order_ids(
                            runtime_cfg,
                            manager,
                            open_order_ids,
                        )
                        canceled_count += int(
                            cancel_payload.get("canceled_count", 0) or 0
                        )
                        if cancel_payload.get("canceled_count"):
                            last_cancel_at = time.time()
                        write_trade_event(runtime_cfg.trade_log, cancel_payload)
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            cancel_payload,
                            source="market_maker_task",
                        )
                        open_order_ids = []
                        open_order_exchange = ""
                        open_order_symbol = ""
                    runtime = {
                        "status": status,
                        "mode": "paused" if status == "paused" else "dry_run",
                        "reason": reason,
                        "config": market_maker_config_to_dict(maker_cfg),
                        "open_order_ids": open_order_ids,
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(open_order_ids),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": open_order_sync,
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "last_error": None,
                        "last_execution": cancel_payload,
                        "market_data": None,
                        "updated_at": time.time(),
                    }
                    await state.set_market_maker_runtime(runtime)
                else:
                    if open_order_snapshot.get("error"):
                        cycle_count += 1
                        runtime = {
                            "status": "open_order_sync_error",
                            "mode": "live",
                            "reason": "could not confirm current open orders",
                            "config": market_maker_config_to_dict(maker_cfg),
                            "open_order_ids": open_order_ids,
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(open_order_ids),
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync_error": open_order_snapshot.get("error"),
                            "open_order_sync": open_order_sync,
                            "placed_count": placed_count,
                            "canceled_count": canceled_count,
                            "cycle_count": cycle_count,
                            "last_error": open_order_snapshot.get("error"),
                            "market_data": None,
                            "updated_at": time.time(),
                        }
                        await state.set_market_maker_runtime(runtime)
                        sleep_for = max(0.0, interval - (time.monotonic() - started))
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    cycle_count += 1
                    order_book, market_data_status = (
                        await _cached_market_maker_order_book(
                            runtime_cfg,
                            orderbook_cache,
                        )
                    )
                    previous_plan_for_cycle = previous_plan
                    force_replace_reason = _market_maker_force_replace_reason(
                        open_order_ids,
                        previous_plan,
                        order_sync=open_order_sync,
                    )
                    force_replace = force_replace_reason is not None
                    if force_replace:
                        previous_plan_for_cycle = None
                    portfolio_snapshot = await state.portfolio_payload()
                    inventory_base = _portfolio_position_for_symbol(
                        portfolio_snapshot,
                        maker_cfg.symbol,
                        cfg=runtime_cfg,
                    )
                    payload = await run_market_maker_cycle(
                        runtime_cfg,
                        manager,
                        live=True,
                        replace_existing=False,
                        replace_order_ids=open_order_ids,
                        previous_plan=previous_plan_for_cycle,
                        existing_open_orders=open_order_snapshot.get("open_orders"),
                        previous_mid_price=previous_mid_price,
                        last_cancel_at=last_cancel_at,
                        order_book=order_book,
                        inventory_base=inventory_base,
                    )
                    if force_replace:
                        payload["force_replace_reason"] = force_replace_reason
                    market_data = (
                        payload.get("market_data")
                        if isinstance(payload.get("market_data"), dict)
                        else {}
                    )
                    if market_data_status:
                        market_data = {
                            **market_data,
                            "cache": market_data_status,
                        }
                    payload["market_data"] = market_data
                    payload["runtime_strategy"] = "market_maker"
                    write_trade_event(runtime_cfg.trade_log, payload)
                    write_strategy_timeline_from_payload(
                        runtime_cfg.strategy_timeline,
                        payload,
                        source="market_maker_task",
                    )
                    plan_payload = (
                        payload.get("plan")
                        if isinstance(payload.get("plan"), dict)
                        else None
                    )
                    if plan_payload and isinstance(
                        plan_payload.get("mid_price"),
                        (int, float),
                    ) and payload.get("status") in {"placed", "unchanged"}:
                        previous_plan = plan_payload
                        previous_mid_price = float(plan_payload["mid_price"])
                    execution = (
                        payload.get("execution")
                        if isinstance(payload.get("execution"), dict)
                        else {}
                    )
                    placed_count += int(execution.get("placed_count", 0) or 0)
                    canceled_count += int(execution.get("canceled_count", 0) or 0)
                    if int(execution.get("canceled_count", 0) or 0) > 0:
                        last_cancel_at = time.time()
                    open_order_ids = [
                        str(order_id)
                        for order_id in execution.get("placed_order_ids", [])
                        if order_id
                    ] or [
                        str(order_id)
                        for order_id in execution.get("remaining_open_order_ids", [])
                        if order_id
                    ] or open_order_ids
                    if open_order_ids:
                        open_order_exchange = maker_cfg.exchange
                        open_order_symbol = maker_cfg.symbol
                    elif payload.get("status") == "placed":
                        open_order_exchange = ""
                        open_order_symbol = ""
                    runtime = {
                        "status": payload.get("status", "unknown"),
                        "mode": "live",
                        "reason": None,
                        "config": market_maker_config_to_dict(maker_cfg),
                        "open_order_ids": open_order_ids,
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(open_order_ids),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": open_order_sync,
                        "force_replace": force_replace,
                        "force_replace_reason": force_replace_reason,
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "last_plan": payload.get("plan"),
                        "last_risk": payload.get("risk"),
                        "last_execution": execution,
                        "last_error": None,
                        "market_data": payload.get("market_data"),
                        "updated_at": time.time(),
                    }
                    await state.set_market_maker_runtime(runtime)
            except Exception as exc:  # noqa: BLE001
                runtime = {
                    **runtime,
                    "status": "error",
                    "mode": "live" if live_allowed else "dry_run",
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                    "updated_at": time.time(),
                }
                await state.set_market_maker_runtime(runtime)

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await orderbook_cache.close()
        await manager.close()


def _env_optional(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    return value if value else None


def _web_password(cfg: BotConfig) -> str | None:
    return _env_optional(cfg.web_security.password_env)


def _cookie_secret(cfg: BotConfig) -> str:
    return (
        _env_optional(cfg.web_security.cookie_secret_env)
        or _web_password(cfg)
        or "crypto-arbitrage-dev"
    )


def _request_is_https(request: web.Request, cfg: BotConfig) -> bool:
    if request.secure:
        return True
    if not cfg.web_security.trust_proxy_headers:
        return False
    return request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _client_ip(request: web.Request, cfg: BotConfig) -> str:
    if cfg.web_security.trust_proxy_headers:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return request.remote or ""


def _is_local_ip(value: str) -> bool:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    return parsed.is_loopback


def _allowed_ip_specs(cfg: BotConfig) -> list[str]:
    value = _env_optional(cfg.web_security.allowed_ips_env)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _ip_allowed(ip_value: str, allowed_specs: list[str]) -> bool:
    if not allowed_specs:
        return True
    try:
        parsed_ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    for spec in allowed_specs:
        try:
            if "/" in spec:
                if parsed_ip in ipaddress.ip_network(spec, strict=False):
                    return True
            elif parsed_ip == ipaddress.ip_address(spec):
                return True
        except ValueError:
            continue
    return False


def _sign_session(cfg: BotConfig, timestamp: int) -> str:
    secret = _cookie_secret(cfg).encode("utf-8")
    payload = str(timestamp).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _make_session_token(cfg: BotConfig) -> str:
    timestamp = int(time.time())
    raw = f"{timestamp}:{_sign_session(cfg, timestamp)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _session_valid(cfg: BotConfig, token: str | None) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        timestamp_text, signature = raw.split(":", 1)
        timestamp = int(timestamp_text)
    except (ValueError, TypeError):
        return False
    if time.time() - timestamp > SESSION_MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(signature, _sign_session(cfg, timestamp))


def _login_html(error: str = "") -> str:
    return LOGIN_HTML.replace("__ERROR__", html.escape(error))


async def login_get(request: web.Request) -> web.Response:
    return web.Response(
        text=_login_html(),
        content_type="text/html",
    )


async def login_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    password = _web_password(cfg)
    if not password:
        raise web.HTTPFound("/")
    form = await request.post()
    supplied = str(form.get("password", ""))
    if not hmac.compare_digest(supplied, password):
        return web.Response(
            text=_login_html("Invalid password"),
            content_type="text/html",
            status=401,
        )
    response = web.HTTPFound("/")
    response.set_cookie(
        SESSION_COOKIE,
        _make_session_token(cfg),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=cfg.web_security.cookie_secure and _request_is_https(request, cfg),
        samesite="Strict",
    )
    raise response


async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    response.del_cookie(SESSION_COOKIE)
    raise response


def build_security_middleware(cfg: BotConfig) -> web.middleware:
    @web.middleware
    async def security_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        remote = request.remote or ""
        client_ip = _client_ip(request, cfg)
        allowed_specs = _allowed_ip_specs(cfg)
        proxy_ip_present = bool(
            request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
        )
        if (
            allowed_specs
            and not (_is_local_ip(remote) and not proxy_ip_present)
            and not _ip_allowed(client_ip, allowed_specs)
        ):
            return web.Response(text="Forbidden", status=403)

        if request.path in {"/login", "/logout"}:
            return await handler(request)

        password = _web_password(cfg)
        if not password:
            return await handler(request)
        if request.path == "/api/health" and _is_local_ip(remote):
            return await handler(request)
        if not _session_valid(cfg, request.cookies.get(SESSION_COOKIE)):
            if request.path.startswith("/api/"):
                return web.json_response({"error": "authentication required"}, status=401)
            raise web.HTTPFound("/login")
        return await handler(request)

    return security_middleware


async def index(_: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def api_state(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    view = request.query.get("view")
    return web.json_response(await state.get(view=view))


async def api_control(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    running = payload.get("running")
    if not isinstance(running, bool):
        return web.json_response({"error": "running must be a boolean"}, status=400)

    result = await state.set_running(running)
    write_web_audit_event(
        cfg,
        request,
        action="program_control",
        target="program",
        detail="resume scans" if running else "pause scans",
        payload={"running": running},
    )
    return web.json_response(result)


async def api_slow_execution(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _spot_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
        )
        allowed_exchanges = {account["key"] for account in accounts}
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges=allowed_exchanges,
            symbols_by_exchange=symbols_by_exchange,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    await state.set_slow_execution_overrides(overrides, cfg=cfg)
    current_config = await state.slow_execution_config(cfg.slow_execution)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated Auto Buy/Sell defaults",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": slow_execution_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _spot_symbols_by_exchange(runtime_cfg),
            ),
        }
    )


async def api_market_maker(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _market_maker_symbols_by_exchange(runtime_cfg)
        overrides = _market_maker_overrides_from_payload(
            payload,
            allowed_exchanges={
                exchange.key for exchange in _all_account_exchanges(runtime_cfg)
            },
            symbols_by_exchange=symbols_by_exchange,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_market_maker_overrides(overrides, cfg=cfg)
    current_config = await state.market_maker_config(cfg.market_maker)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="market_maker_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated Market Maker config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": market_maker_config_to_dict(current_config),
            **update,
        }
    )


async def api_markets(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        markets = _spot_markets_from_payload(
            payload,
            allowed_exchanges={exchange.key for exchange in cfg.spot_exchanges},
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    result = await state.set_spot_markets(markets, cfg=cfg)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="markets_config",
        target="spot_markets",
        detail=f"set {len(markets)} spot market(s)",
        payload={"spot_markets": spot_markets_to_list(markets)},
    )
    return web.json_response(result)


async def api_cash_and_carry_pairs(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        pairs = _cash_and_carry_pairs_from_payload(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    result = await state.set_cash_and_carry_pairs(pairs, cfg=cfg)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="cash_and_carry_config",
        target="cash_and_carry_pairs",
        detail=f"set {len(pairs)} pair(s)",
        payload={"cash_and_carry_pairs": cash_and_carry_pairs_to_list(pairs)},
    )
    return web.json_response(result)


async def api_create_auto_buy_sell_task(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _spot_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
        )
        allowed_exchanges = {account["key"] for account in accounts}
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges=allowed_exchanges,
            symbols_by_exchange=symbols_by_exchange,
        )
        base_config = await state.slow_execution_config(cfg.slow_execution)
        task_config = replace(base_config, **{**overrides, "enabled": True})
        validate_task_config(task_config)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    try:
        task = await tasks.create_task(task_config)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    await state.set_slow_execution_overrides(
        {
            **overrides,
            "enabled": True,
        },
        cfg=cfg,
    )
    snapshot = await tasks.snapshot()
    await state.set_auto_buy_sell_tasks(snapshot)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_task_create",
        target=f"{task_config.exchange} {task_config.symbol}",
        detail=f"created task {task.get('id', '')}",
        payload={"task_id": task.get("id"), "config": slow_execution_config_to_dict(task_config)},
    )
    return web.json_response(
        {
            "ok": True,
            "task": task,
            "tasks": snapshot,
            "config": slow_execution_config_to_dict(task_config),
        }
    )


async def api_control_auto_buy_sell_task(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    task_id = request.match_info.get("task_id", "")
    try:
        payload = await request.json()
        action = str(payload.get("action", "")).strip().lower()
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("action must be pause, resume, or stop")
        if action == "stop":
            manager = ExchangeManager()
            runtime_cfg = await state.runtime_config(cfg)
            cancel_open_orders = bool(payload.get("cancel_open_orders", True))
            task = await tasks.stop_task(
                task_id,
                runtime_cfg,
                manager,
                cancel_open_orders=cancel_open_orders,
            )
        else:
            task = await tasks.set_paused(task_id, action == "pause")
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    snapshot = await tasks.snapshot()
    await state.set_auto_buy_sell_tasks(snapshot)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_task_control",
        target=task_id,
        detail=f"{action} task",
        payload={"task_id": task_id, "action": action},
    )
    return web.json_response(
        {
            "ok": True,
            "task": task,
            "tasks": snapshot,
        }
    )


async def api_cleanup_auto_buy_sell_tasks(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    try:
        payload = await request.json()
        if not bool(payload.get("terminal_only", True)):
            raise ValueError("only terminal task cleanup is supported")
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    result = await tasks.clear_terminal_tasks()
    await state.set_auto_buy_sell_tasks(result["tasks"])
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_task_cleanup",
        target="terminal_tasks",
        detail=f"removed {result['removed_count']} terminal task(s)",
        payload={
            "removed_count": result["removed_count"],
            "removed_task_ids": result["removed_task_ids"],
        },
    )
    return web.json_response({"ok": True, **result})


async def api_risk(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        allowed_accounts = {
            exchange.key for exchange in _all_account_exchanges(runtime_cfg)
        }
        overrides = _risk_overrides_from_payload(
            payload,
            allowed_accounts=allowed_accounts,
            allowed_strategies=STRATEGY_IDS,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_risk_overrides(overrides, cfg=cfg)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="risk_config",
        target="risk",
        detail="updated risk controls",
        payload=overrides,
    )
    return web.json_response({"ok": True, **update})


async def api_cancel_order(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    manager = ExchangeManager()
    try:
        runtime_cfg = await state.runtime_config(cfg)
        runtime_slow_execution = runtime_cfg.slow_execution
        result = await cancel_order_payload(
            runtime_cfg,
            manager,
            payload,
            runtime_slow_execution,
        )
        order_activity = await fetch_order_activity_payload(
            runtime_cfg,
            manager,
            runtime_slow_execution,
        )
        await state.set_order_activity(order_activity)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"{exc.__class__.__name__}: {exc}"},
            status=500,
        )
    finally:
        await manager.close()

    result["order_activity"] = order_activity
    return web.json_response(result)


async def api_cancel_bulk_orders(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    manager = ExchangeManager()
    try:
        runtime_cfg = await state.runtime_config(cfg)
        runtime_slow_execution = runtime_cfg.slow_execution
        result = await cancel_bulk_orders_payload(
            runtime_cfg,
            manager,
            payload,
            runtime_slow_execution,
        )
        order_activity = await fetch_order_activity_payload(
            runtime_cfg,
            manager,
            runtime_slow_execution,
        )
        await state.set_order_activity(order_activity)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"{exc.__class__.__name__}: {exc}"},
            status=500,
        )
    finally:
        await manager.close()

    result["order_activity"] = order_activity
    return web.json_response(result)


async def api_strategy_control(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        strategy_id = str(payload.get("strategy", "")).strip()
        paused = payload.get("paused")
        if not strategy_id:
            raise ValueError("strategy is required")
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean")
        trading_console = await state.set_strategy_paused(
            strategy_id,
            paused,
            cfg=cfg,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="strategy_control",
        target=strategy_id,
        detail="paused strategy" if paused else "resumed strategy",
        payload={"strategy": strategy_id, "paused": paused},
    )
    return web.json_response(
        {
            "ok": True,
            "strategy": strategy_id,
            "paused": paused,
            "trading_console": trading_console,
        }
    )


async def api_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_app(
    cfg: BotConfig,
    strategy: StrategyName,
    poll_seconds: float | None,
) -> web.Application:
    interval = cfg.poll_seconds if poll_seconds is None else poll_seconds
    app = web.Application(middlewares=[build_security_middleware(cfg)])
    state = MonitorState(
        cfg,
        interval,
        runtime_store_path=default_runtime_store_path(cfg),
    )
    auto_buy_sell_tasks = AutoBuySellTaskService(default_task_store_path(cfg))
    app["monitor_state"] = state
    app["config"] = cfg
    app["auto_buy_sell_tasks"] = auto_buy_sell_tasks

    async def monitor_context(app_: web.Application) -> Any:
        monitor_task = asyncio.create_task(monitor_loop(cfg, strategy, state, interval))
        mm_task = asyncio.create_task(market_maker_task_loop(cfg, state))
        auto_task = asyncio.create_task(
            auto_buy_sell_task_loop(cfg, state, auto_buy_sell_tasks)
        )
        app_["monitor_task"] = monitor_task
        app_["market_maker_task"] = mm_task
        app_["auto_buy_sell_task"] = auto_task
        try:
            yield
        finally:
            monitor_task.cancel()
            mm_task.cancel()
            auto_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            with contextlib.suppress(asyncio.CancelledError):
                await mm_task
            with contextlib.suppress(asyncio.CancelledError):
                await auto_task

    app.cleanup_ctx.append(monitor_context)

    from .routes import register_routes

    register_routes(app)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crypto arbitrage monitor web UI")
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--strategy",
        choices=["all", "spot-spread", "cash-and-carry"],
        default="spot-spread",
        help="Strategy to monitor",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override config poll interval",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    app = create_app(cfg, args.strategy, args.poll_seconds)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
