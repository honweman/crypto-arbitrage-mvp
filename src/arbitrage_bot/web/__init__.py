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
import secrets
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from aiohttp import web

from .render_payloads import (
    STATE_VIEW_IDS,
    state_payload_for_view,
    strategy_center_payload_for_view,
)
from .users import (
    WebUser,
    WebUserStore,
    normalize_assets,
    totp_provisioning_uri,
)
from .user_scope import (
    _assets_from_cash_and_carry_pairs,
    _assets_from_spot_markets,
    _base_asset_from_symbol,
    _configured_assets,
    _filter_state_payload_for_user,
    _require_admin_user,
    _require_user_assets,
)

from ..account_check import (
    _auth_env_status,
    _balance_currencies,
    _market_summary,
    _summarize_balance,
)
from ..alerts import AlertService
from ..auto_buy_sell_task import (
    AutoBuySellTaskService,
    default_task_store_path,
    validate_task_config,
)
from ..backtesting import run_paper_backtest
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
from ..contract_strategies import build_contract_strategies_payload
from ..derivatives import derivative_account_summary, normalize_derivative_position
from ..exchanges import ExchangeManager, limit_order_features
from ..execution_algos import build_execution_algo_plan
from ..fill_store import load_daily_pnl_summary, persist_fill_pnl
from ..funding_basis import funding_basis_payload, funding_settings_from_strategy_center
from ..grid_trading import build_dca_plan, build_spot_grid_plan
from ..jsonl_rotation import rotate_jsonl_log_if_needed
from ..observability import configure_logging, render_prometheus_metrics
from ..main import (
    StrategyName,
    _option_symbols_for_option_combos,
    _quote_rates_from_sources,
    _spot_symbols_for_option_combos,
    _symbols_for_configured_spot_markets,
    scan_with_manager,
)
from ..market_making import MarketMakerPlan, build_symmetric_market_maker_plan
from ..market_maker import (
    cancel_order_ids as cancel_market_maker_order_ids,
    market_maker_quote_conversion,
    market_maker_risk_config,
    order_book_market_data,
    run_cycle as run_market_maker_cycle,
)
from ..models import OrderBookSnapshot, Opportunity
from ..options_monitor import options_arbitrage_payload
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
from ..strategy_center import (
    FundingArbitrageSettings,
    SignalBotSettings,
    SignalEvent,
    StrategyCenterStore,
    StrategyInstance,
    UserApiAccount,
    build_strategy_center_public_payload,
)
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
    _backtest_overrides_from_payload,
    _cash_and_carry_pairs_from_payload,
    _derivative_symbols_by_exchange,
    _dca_overrides_from_payload,
    _execution_algo_overrides_from_payload,
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    _market_maker_overrides_from_payload,
    _market_maker_symbols_by_exchange,
    _risk_overrides_from_payload,
    _slow_execution_overrides_from_payload,
    _spot_grid_overrides_from_payload,
    _spot_markets_from_payload,
    _spot_symbols_by_exchange,
    backtest_config_to_dict,
    cash_and_carry_pairs_to_list,
    contract_strategies_config_to_dict,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    exchange_configs_to_list,
    market_maker_config_to_dict,
    market_maker_config_from_payload,
    market_maker_configs_for_runtime,
    market_maker_configs_from_payload,
    market_maker_configs_to_list,
    market_maker_config_with_id,
    market_maker_symbols_for_accounts,
    risk_config_to_dict,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_grid_config_to_dict,
    spot_markets_to_list,
    strategy_universe_to_dict,
)


ACCOUNT_BALANCE_POLL_SECONDS = 10.0
ORDER_ACTIVITY_POLL_SECONDS = 5.0
ORDER_ACTIVITY_LIMIT = 20
SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS = 10.0
STRATEGY_IDS = {
    "market_maker",
    "slow_execution",
    "spot_grid",
    "dca",
    "execution_algo",
    "backtest",
    "spot_spread",
    "cash_and_carry",
    "triangular_arbitrage",
    "funding_arbitrage",
    "funding_bot",
    "basis_bot",
    "futures_grid",
    "hedge_rebalancer",
    "options_arbitrage",
    "signal_bot",
}
SESSION_COOKIE = "crypto_arb_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Brute-force protection for the login endpoint.
LOGIN_MAX_FAILURES = 8
LOGIN_FAILURE_WINDOW_SECONDS = 300.0
LOGIN_LOCKOUT_SECONDS = 300.0


class LoginRateLimiter:
    """In-memory, per-client throttle that slows down login brute forcing.

    Keyed by client IP rather than account so a flood of guesses for one
    account cannot lock a victim out (which would itself be a denial of
    service). After ``max_failures`` failed attempts inside ``window_seconds``
    the client is locked out for ``lockout_seconds``.
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
        lockout_seconds: float = LOGIN_LOCKOUT_SECONDS,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Return seconds the client must wait, or 0.0 if it may try now."""
        now = time.time() if now is None else now
        locked_until = self._locked_until.get(key)
        if locked_until is None:
            return 0.0
        if locked_until > now:
            return locked_until - now
        self._locked_until.pop(key, None)
        self._failures.pop(key, None)
        return 0.0

    def register_failure(self, key: str, *, now: float | None = None) -> float:
        """Record a failed attempt and return the lockout seconds if triggered."""
        now = time.time() if now is None else now
        bucket = self._failures.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) >= self.max_failures:
            self._locked_until[key] = now + self.lockout_seconds
            bucket.clear()
            return self.lockout_seconds
        return 0.0

    def register_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Trading Login</title>
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
    p { margin: 0; color: #66736b; font-size: 13px; line-height: 1.45; }
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
    a { color: #1d4ed8; font-size: 13px; text-decoration: none; }
    .optional { display: __EMAIL_DISPLAY__; }
    .register { display: __REGISTER_DISPLAY__; }
    .error { min-height: 18px; color: #b33b2e; font-size: 13px; }
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Crypto Trading</h1>
    <p>__LOGIN_HINT__</p>
    <div class="optional">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" autocomplete="username" autofocus>
    </div>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" __PASSWORD_AUTOFOCUS__>
    <div class="optional">
      <label for="totp">Google Authenticator Code</label>
      <input id="totp" name="totp" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*">
    </div>
    <button type="submit">Sign In</button>
    <a class="register" href="/register">Register with email and 2FA</a>
    <div class="error">__ERROR__</div>
  </form>
</body>
</html>
"""


REGISTER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Register Crypto Trading User</title>
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
    form, .panel {
      width: min(440px, calc(100% - 32px));
      display: grid;
      gap: 12px;
      padding: 22px;
      border: 1px solid #d8ded8;
      border-radius: 8px;
      background: #ffffff;
    }
    h1 { margin: 0 0 4px; font-size: 20px; }
    p { margin: 0; color: #66736b; font-size: 13px; line-height: 1.45; }
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
    code {
      display: block;
      padding: 10px;
      border-radius: 6px;
      background: #f1f5f9;
      overflow-wrap: anywhere;
    }
    a { color: #1d4ed8; font-size: 13px; text-decoration: none; }
    .code-field { display: __CODE_DISPLAY__; }
    .error { min-height: 18px; color: #b33b2e; font-size: 13px; }
  </style>
</head>
<body>
  __BODY__
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
            strategy_id="spot_grid",
            label="Spot Grid",
            configured=cfg.spot_grid.enabled,
            exchange=cfg.spot_grid.exchange,
            symbol=cfg.spot_grid.symbol,
            strategy_allowed=_risk_strategy_enabled(cfg, "spot_grid"),
            live_ready=cfg.spot_grid.live_enabled,
        ),
        strategy_row(
            strategy_id="dca",
            label="DCA Bot",
            configured=cfg.dca.enabled,
            exchange=cfg.dca.exchange,
            symbol=cfg.dca.symbol,
            strategy_allowed=_risk_strategy_enabled(cfg, "dca"),
            live_ready=cfg.dca.live_enabled,
        ),
        strategy_row(
            strategy_id="execution_algo",
            label="TWAP/VWAP/POV",
            configured=cfg.execution_algo.enabled,
            exchange=cfg.execution_algo.exchange,
            symbol=cfg.execution_algo.symbol,
            strategy_allowed=_risk_strategy_enabled(cfg, "execution_algo"),
            live_ready=cfg.execution_algo.live_enabled,
        ),
        strategy_row(
            strategy_id="backtest",
            label="Backtest/Paper",
            configured=cfg.backtest.enabled,
            exchange=cfg.backtest.exchange,
            symbol=cfg.backtest.symbol,
            strategy_allowed=_risk_strategy_enabled(cfg, "backtest"),
            live_ready=False,
            mode="research",
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
        strategy_row(
            strategy_id="funding_arbitrage",
            label="Funding Arbitrage",
            configured=cfg.strategy_center.enabled,
            exchange="",
            symbol="strategy center",
            strategy_allowed=_risk_strategy_enabled(cfg, "funding_arbitrage"),
            mode="scan",
        ),
        strategy_row(
            strategy_id="funding_bot",
            label="Funding Bot",
            configured=(
                cfg.contract_strategies.enabled
                and cfg.contract_strategies.funding_bot_enabled
            ),
            exchange=cfg.contract_strategies.derivative_exchange,
            symbol=cfg.contract_strategies.derivative_symbol or "funding pairs",
            strategy_allowed=_risk_strategy_enabled(cfg, "funding_bot"),
            live_ready=False,
            mode="paper",
        ),
        strategy_row(
            strategy_id="basis_bot",
            label="Basis Bot",
            configured=(
                cfg.contract_strategies.enabled
                and cfg.contract_strategies.basis_bot_enabled
            ),
            exchange=cfg.contract_strategies.derivative_exchange,
            symbol=cfg.contract_strategies.derivative_symbol or "basis pairs",
            strategy_allowed=_risk_strategy_enabled(cfg, "basis_bot"),
            live_ready=False,
            mode="paper",
        ),
        strategy_row(
            strategy_id="futures_grid",
            label="Futures Grid",
            configured=(
                cfg.contract_strategies.enabled
                and cfg.contract_strategies.futures_grid_enabled
            ),
            exchange=cfg.contract_strategies.derivative_exchange,
            symbol=cfg.contract_strategies.derivative_symbol,
            strategy_allowed=_risk_strategy_enabled(cfg, "futures_grid"),
            live_ready=False,
            mode="paper",
        ),
        strategy_row(
            strategy_id="hedge_rebalancer",
            label="Hedge Rebalancer",
            configured=(
                cfg.contract_strategies.enabled
                and cfg.contract_strategies.hedge_rebalancer_enabled
            ),
            exchange=cfg.contract_strategies.derivative_exchange,
            symbol=cfg.contract_strategies.derivative_symbol,
            strategy_allowed=_risk_strategy_enabled(cfg, "hedge_rebalancer"),
            live_ready=False,
            mode="paper",
        ),
        strategy_row(
            strategy_id="options_arbitrage",
            label="Options Arbitrage",
            configured=bool(cfg.option_combos),
            exchange="",
            symbol=",".join(sorted({combo.underlying for combo in cfg.option_combos})),
            strategy_allowed=_risk_strategy_enabled(cfg, "options_arbitrage"),
            mode="scan",
        ),
        strategy_row(
            strategy_id="signal_bot",
            label="Signal Bot",
            configured=cfg.strategy_center.enabled,
            exchange="",
            symbol="webhook",
            strategy_allowed=_risk_strategy_enabled(cfg, "signal_bot"),
            mode="trigger",
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

    if cfg.spot_grid.exchange and cfg.spot_grid.symbol:
        symbols.setdefault(cfg.spot_grid.exchange, set()).add(cfg.spot_grid.symbol)

    if cfg.dca.exchange and cfg.dca.symbol:
        symbols.setdefault(cfg.dca.exchange, set()).add(cfg.dca.symbol)

    if cfg.execution_algo.exchange and cfg.execution_algo.symbol:
        symbols.setdefault(cfg.execution_algo.exchange, set()).add(
            cfg.execution_algo.symbol
        )

    if cfg.backtest.exchange and cfg.backtest.symbol:
        symbols.setdefault(cfg.backtest.exchange, set()).add(cfg.backtest.symbol)

    if cfg.contract_strategies.spot_exchange and cfg.contract_strategies.spot_symbol:
        symbols.setdefault(cfg.contract_strategies.spot_exchange, set()).add(
            cfg.contract_strategies.spot_symbol
        )

    if (
        cfg.contract_strategies.derivative_exchange
        and cfg.contract_strategies.derivative_symbol
    ):
        symbols.setdefault(cfg.contract_strategies.derivative_exchange, set()).add(
            cfg.contract_strategies.derivative_symbol
        )

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


def _derivative_account_messages(account: dict[str, Any]) -> list[str]:
    summary = account.get("summary") if isinstance(account.get("summary"), dict) else {}
    messages = [
        str(message)
        for message in [
            *list(account.get("risk_reasons", []) or []),
            *list(summary.get("risk_reasons", []) or []),
            *list(account.get("errors", []) or []),
            *list(account.get("warnings", []) or []),
            account.get("skipped_reason"),
        ]
        if message
    ]
    return _dedupe_readiness_messages(messages)


def _derivatives_readiness_summary(
    derivatives: dict[str, Any],
) -> dict[str, Any]:
    accounts = [
        account
        for account in derivatives.get("accounts", []) or []
        if isinstance(account, dict)
    ]
    blocked_accounts = [
        account for account in accounts if account.get("status") == "blocked"
    ]
    warning_accounts = [
        account for account in accounts if account.get("status") == "warning"
    ]
    error_accounts = [
        account for account in accounts if account.get("status") == "error"
    ]
    reasons: list[str] = []
    for account in [*error_accounts, *blocked_accounts, *warning_accounts]:
        label = account.get("label") or account.get("exchange") or "derivatives"
        messages = _derivative_account_messages(account)
        if messages:
            reasons.append(f"{label}: {messages[0]}")
    reasons.extend(str(item) for item in derivatives.get("warnings", []) or [] if item)
    reasons.extend(str(item) for item in derivatives.get("errors", []) or [] if item)
    return {
        "status": derivatives.get("status") or "disabled",
        "account_count": len(accounts),
        "blocked_account_count": len(blocked_accounts),
        "warning_account_count": len(warning_accounts),
        "error_account_count": len(error_accounts),
        "position_count": int(derivatives.get("position_count") or 0),
        "reasons": _dedupe_readiness_messages(reasons)[:6],
        "has_warnings": bool(derivatives.get("warnings")),
        "has_errors": bool(derivatives.get("errors")),
    }


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
    spot_grid: dict[str, Any] | None = None,
    dca: dict[str, Any] | None = None,
    execution_algo: dict[str, Any] | None = None,
    backtest: dict[str, Any] | None = None,
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
    if (
        strategy_id != "backtest"
        and not strategy.get("live_ready", True)
        and strategy.get("mode") not in {"paper", "research", "scan", "trigger"}
    ):
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
    strategy_payload = {
        "spot_grid": spot_grid,
        "dca": dca,
        "execution_algo": execution_algo,
        "backtest": backtest,
    }.get(strategy_id)
    if isinstance(strategy_payload, dict):
        if strategy_payload.get("status") == "error" and strategy_payload.get("error"):
            reasons.append(str(strategy_payload["error"]))
        safety = (
            strategy_payload.get("safety")
            if isinstance(strategy_payload.get("safety"), dict)
            else {}
        )
        for message in list(safety.get("reasons", []) or [])[:2]:
            if message:
                reasons.append(str(message))

    return _dedupe_readiness_messages(reasons)


def build_readiness_payload(
    cfg: BotConfig,
    *,
    account_balances: dict[str, Any] | None = None,
    order_activity: dict[str, Any] | None = None,
    derivatives: dict[str, Any] | None = None,
    trading_console: dict[str, Any] | None = None,
    market_maker: dict[str, Any] | None = None,
    slow_execution: dict[str, Any] | None = None,
    spot_grid: dict[str, Any] | None = None,
    dca: dict[str, Any] | None = None,
    execution_algo: dict[str, Any] | None = None,
    backtest: dict[str, Any] | None = None,
    execution_protection: dict[str, Any] | None = None,
    markets: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    account_balances = account_balances or {}
    order_activity = order_activity or {}
    derivatives = derivatives or {}
    trading_console = trading_console or build_trading_console_payload(cfg)
    market_maker = market_maker or {}
    slow_execution = slow_execution or {}
    spot_grid = spot_grid or {}
    dca = dca or {}
    execution_algo = execution_algo or {}
    backtest = backtest or {}
    execution_protection = execution_protection or {}
    symbols_by_exchange = _exchange_balance_symbols(cfg)
    balance_by_exchange = _account_payload_by_exchange(account_balances)
    order_by_exchange = _account_payload_by_exchange(order_activity)
    derivative_by_exchange = _account_payload_by_exchange(derivatives)
    derivative_readiness = _derivatives_readiness_summary(derivatives)
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
        derivative_account = derivative_by_exchange.get(exchange.key, {})
        derivative_status = str(derivative_account.get("status") or "")
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
        if used and derivative_status == "error":
            reasons.extend(
                _derivative_account_messages(derivative_account)
                or ["derivatives risk check failed"]
            )
        elif used and derivative_status == "blocked":
            reasons.extend(
                _derivative_account_messages(derivative_account)
                or ["derivatives risk limit breached"]
            )
        elif used and derivative_status == "warning":
            reasons.extend(
                _derivative_account_messages(derivative_account)
                or ["derivatives risk warning"]
            )

        if not used:
            status = "idle"
        elif (
            balance_status in checking_statuses
            or order_status in checking_statuses
            or derivative_status in checking_statuses
        ):
            status = "checking"
        elif (
            not auth["private_checks_enabled"]
            or not risk_enabled
            or balance_status == "error"
            or order_status == "error"
            or derivative_status in {"blocked", "error"}
        ):
            status = "blocked"
        elif (
            balance_status == "warning"
            or order_status == "warning"
            or derivative_status == "warning"
        ):
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
                "derivatives_status": derivative_status or "disabled",
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
            spot_grid=spot_grid,
            dca=dca,
            execution_algo=execution_algo,
            backtest=backtest,
        )
        if (
            strategy.get("mode") in {"paper", "research"}
            and strategy.get("configured")
            and not reasons
        ):
            status = str(strategy.get("mode") or "paper")
        elif strategy.get("id") == "backtest" and strategy.get("configured"):
            status = "research"
        elif strategy.get("live"):
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
    protection_blocked_count = int(execution_protection.get("blocked_count") or 0)
    protection_warning_count = int(execution_protection.get("warning_count") or 0)
    protection_manual_review_count = int(
        execution_protection.get("manual_review_count") or 0
    )
    derivative_blocked_count = int(
        derivative_readiness["blocked_account_count"]
        + derivative_readiness["error_account_count"]
    )
    derivative_warning_count = int(
        derivative_readiness["warning_account_count"]
        + (1 if derivative_readiness["has_warnings"] else 0)
    )
    warning_count = (
        warning_accounts
        + market_missing_count
        + (1 if reconciliation.get("status") == "warning" else 0)
        + (1 if order_activity.get("status") == "warning" else 0)
        + (1 if account_balances.get("status") == "warning" else 0)
        + protection_warning_count
        + protection_manual_review_count
        + (1 if derivative_readiness["has_warnings"] else 0)
    )

    account_checks_status = str(account_balances.get("status") or "starting")
    order_checks_status = str(order_activity.get("status") or "starting")
    derivative_checks_status = str(derivatives.get("status") or "disabled")
    if (
        order_activity.get("status") == "error"
        or account_balances.get("status") == "error"
        or derivative_checks_status == "error"
        or derivative_readiness["has_errors"]
    ):
        status = "error"
    elif (
        checking_accounts
        or account_checks_status in checking_statuses
        or order_checks_status in checking_statuses
        or derivative_checks_status in checking_statuses
    ):
        status = "checking"
    elif not (cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading):
        status = "guarded"
    elif (
        blocked_accounts
        or blocked_strategies
        or protection_blocked_count
        or derivative_blocked_count
    ):
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
    if protection_blocked_count or protection_warning_count or protection_manual_review_count:
        protection_reasons = execution_protection.get("top_reasons") or []
        next_actions.append(
            _readiness_action(
                priority="high" if protection_blocked_count else "medium",
                scope="Execution Protection",
                action="Review multi-leg paper protection",
                status=str(execution_protection.get("status") or "warning"),
                detail=(
                    str(protection_reasons[0])
                    if protection_reasons
                    else "Multi-leg strategy has paper execution protection warnings."
                ),
            )
        )
    if derivative_blocked_count or derivative_warning_count:
        derivative_reasons = derivative_readiness.get("reasons") or []
        next_actions.append(
            _readiness_action(
                priority="high" if derivative_blocked_count else "medium",
                scope="Derivatives Risk",
                action="Review margin and liquidation risk",
                status=str(derivative_readiness.get("status") or "warning"),
                detail=(
                    str(derivative_reasons[0])
                    if derivative_reasons
                    else "Derivative risk checks have warnings."
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
            "execution_protection_blocked_count": protection_blocked_count,
            "execution_protection_warning_count": protection_warning_count,
            "execution_protection_manual_review_count": protection_manual_review_count,
            "derivative_blocked_account_count": derivative_blocked_count,
            "derivative_warning_account_count": derivative_warning_count,
            "derivative_position_count": derivative_readiness["position_count"],
            "blocked_count": blocked_accounts + blocked_strategies + protection_blocked_count,
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


def _derivatives_status(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
        return "disabled"
    if any(account.get("status") == "error" for account in accounts):
        return "error"
    if any(account.get("status") == "blocked" for account in accounts):
        return "blocked"
    if any(account.get("status") == "warning" for account in accounts):
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
        "markets": [],
    }

    if not symbols:
        account["status"] = "idle"
        account["balance"]["skipped_reason"] = "no configured symbols"
        return account

    account["markets"] = await _fetch_exchange_market_limit_payload(
        manager,
        exchange,
        symbols,
    )
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


async def _fetch_exchange_market_limit_payload(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        row: dict[str, Any] = {
            "exchange": exchange.key,
            "symbol": symbol,
            "status": "unknown",
            "market": {"found": False},
            "error": None,
        }
        try:
            market = await manager.fetch_market_info(exchange, symbol=symbol)
            row["market"] = _market_summary(market)
            row["status"] = "ok" if row["market"].get("found") else "missing"
        except Exception as exc:  # noqa: BLE001
            row["status"] = "error"
            row["error"] = f"{exc.__class__.__name__}: {exc}"
        rows.append(row)
    return rows


async def _fetch_derivative_exchange_risk_payload(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
    funding_rates: dict[tuple[str, str], float],
    risk: RiskConfig,
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
        "checked": False,
        "skipped_reason": "",
        "summary": {},
        "positions": [],
        "risk_reasons": [],
        "warnings": [],
        "errors": [],
    }
    if not symbols:
        account["status"] = "idle"
        account["skipped_reason"] = "no configured derivative symbols"
        return account
    if not auth["configured"]:
        account["status"] = "warning"
        account["skipped_reason"] = "api env vars not configured"
        account["warnings"].append("API env vars are not configured")
        return account
    if auth["missing_env"]:
        account["status"] = "warning"
        account["skipped_reason"] = "api env vars missing"
        account["warnings"].append("one or more configured API env vars are not set")
        return account

    try:
        balance = await manager.fetch_balance(exchange)
    except Exception as exc:  # noqa: BLE001
        message = f"{exc.__class__.__name__}: {exc}"
        account["status"] = "error"
        account["errors"].append(message)
        return account

    try:
        raw_positions = await manager.fetch_positions(exchange, symbols)
    except Exception as exc:  # noqa: BLE001
        message = f"{exc.__class__.__name__}: {exc}"
        account["status"] = "error"
        account["errors"].append(message)
        account["checked"] = True
        return account

    symbol_set = set(symbols)
    positions = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            continue
        row = normalize_derivative_position(exchange, raw, risk=risk)
        if row is None:
            continue
        if row.get("symbol") and row["symbol"] not in symbol_set:
            continue
        row["funding_rate"] = funding_rates.get((exchange.key, row.get("symbol", "")))
        positions.append(row)

    margin_currencies = _balance_currencies(symbols)
    summary = derivative_account_summary(
        balance,
        positions,
        currencies=margin_currencies,
        risk=risk,
    )
    account["checked"] = True
    account["summary"] = summary
    account["positions"] = positions
    account["risk_reasons"] = summary.get("risk_reasons", [])
    if summary.get("status") == "blocked":
        account["status"] = "blocked"
    return account


async def fetch_derivatives_risk_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
) -> dict[str, Any]:
    symbols_by_exchange = _derivative_symbols_by_exchange(cfg)
    try:
        funding_rates = await manager.fetch_funding_rates(
            cfg.derivative_exchanges,
            symbols_by_exchange,
        )
        funding_errors: list[str] = []
    except Exception as exc:  # noqa: BLE001
        funding_rates = {}
        funding_errors = [f"funding rate check failed: {exc.__class__.__name__}: {exc}"]
    accounts = await asyncio.gather(
        *[
            _fetch_derivative_exchange_risk_payload(
                manager,
                exchange,
                symbols_by_exchange.get(exchange.key, []),
                funding_rates,
                cfg.risk,
            )
            for exchange in cfg.derivative_exchanges
        ]
    )
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
    warnings.extend(funding_errors)
    return {
        "status": _derivatives_status(accounts),
        "accounts": accounts,
        "position_count": sum(len(account.get("positions", [])) for account in accounts),
        "checked_account_count": sum(1 for account in accounts if account.get("checked")),
        "total_account_count": len(accounts),
        "funding_rate_count": len(funding_rates),
        "limits": {
            "max_derivative_leverage": cfg.risk.max_derivative_leverage,
            "min_liquidation_buffer_pct": cfg.risk.min_liquidation_buffer_pct,
            "max_margin_usage_pct": cfg.risk.max_margin_usage_pct,
        },
        "last_finished": time.time(),
        "errors": errors,
        "warnings": warnings,
    }


def _configured_exchange_keys(exchanges: Iterable[ExchangeConfig]) -> set[str]:
    return {exchange.key for exchange in exchanges}


async def fetch_funding_basis_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    strategy_center_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings_rows = funding_settings_from_strategy_center(strategy_center_payload)
    if not settings_rows:
        return funding_basis_payload(
            [],
            spot_books={},
            derivative_books={},
            funding_rates={},
            notional_quote=cfg.notional_quote,
            risk=cfg.risk,
        )

    spot_symbols: dict[str, set[str]] = {}
    derivative_symbols: dict[str, set[str]] = {}
    warnings: list[str] = []
    spot_exchange_keys = _configured_exchange_keys(cfg.spot_exchanges)
    derivative_exchange_keys = _configured_exchange_keys(cfg.derivative_exchanges)
    for settings in settings_rows:
        if settings.spot_exchange and settings.spot_symbol:
            spot_symbols.setdefault(settings.spot_exchange, set()).add(
                settings.spot_symbol
            )
            if settings.spot_exchange not in spot_exchange_keys:
                warnings.append(
                    f"{settings.pair_id or settings.spot_symbol}: spot exchange "
                    f"{settings.spot_exchange} is not configured"
                )
        if settings.derivative_exchange and settings.derivative_symbol:
            derivative_symbols.setdefault(settings.derivative_exchange, set()).add(
                settings.derivative_symbol
            )
            if settings.derivative_exchange not in derivative_exchange_keys:
                warnings.append(
                    f"{settings.pair_id or settings.derivative_symbol}: derivative "
                    f"exchange {settings.derivative_exchange} is not configured"
                )

    spot_configs = [
        exchange for exchange in cfg.spot_exchanges if exchange.key in spot_symbols
    ]
    derivative_configs = [
        exchange
        for exchange in cfg.derivative_exchanges
        if exchange.key in derivative_symbols
    ]
    spot_task = manager.fetch_order_books(
        spot_configs,
        spot_symbols,
        cfg.order_book_depth,
    )
    derivative_task = manager.fetch_order_books(
        derivative_configs,
        derivative_symbols,
        cfg.order_book_depth,
    )
    funding_task = manager.fetch_funding_rates(
        derivative_configs,
        derivative_symbols,
    )
    spot_result, derivative_result, funding_result = await asyncio.gather(
        spot_task,
        derivative_task,
        funding_task,
        return_exceptions=True,
    )
    errors: list[str] = []
    if isinstance(spot_result, Exception):
        spot_books = {}
        errors.append(
            f"spot order books failed: {spot_result.__class__.__name__}: {spot_result}"
        )
    else:
        spot_books = spot_result
    if isinstance(derivative_result, Exception):
        derivative_books = {}
        errors.append(
            "derivative order books failed: "
            f"{derivative_result.__class__.__name__}: {derivative_result}"
        )
    else:
        derivative_books = derivative_result
    if isinstance(funding_result, Exception):
        funding_rates = {}
        errors.append(
            f"funding rates failed: {funding_result.__class__.__name__}: {funding_result}"
        )
    else:
        funding_rates = funding_result

    payload = funding_basis_payload(
        settings_rows,
        spot_books=spot_books,
        derivative_books=derivative_books,
        funding_rates=funding_rates,
        notional_quote=cfg.notional_quote,
        risk=cfg.risk,
    )
    payload["warnings"] = [*payload.get("warnings", []), *warnings]
    payload["errors"] = [*payload.get("errors", []), *errors]
    if errors and payload["status"] == "disabled":
        payload["status"] = "error"
    return payload


async def fetch_options_arbitrage_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
) -> dict[str, Any]:
    if not cfg.option_combos:
        return options_arbitrage_payload(cfg, spot_books={}, option_books={})
    if not cfg.options_arbitrage.enabled:
        return options_arbitrage_payload(cfg, spot_books={}, option_books={})

    try:
        spot_books, option_books = await asyncio.gather(
            manager.fetch_order_books(
                cfg.spot_exchanges,
                _spot_symbols_for_option_combos(cfg.option_combos),
                cfg.order_book_depth,
            ),
            manager.fetch_order_books(
                cfg.derivative_exchanges,
                _option_symbols_for_option_combos(cfg.option_combos),
                cfg.order_book_depth,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            **options_arbitrage_payload(cfg, spot_books={}, option_books={}),
            "status": "error",
            "last_finished": time.time(),
            "errors": [f"{exc.__class__.__name__}: {exc}"],
        }
    return options_arbitrage_payload(
        cfg,
        spot_books=spot_books,
        option_books=option_books,
    )


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


def default_web_user_store_path(cfg: BotConfig) -> str:
    return cfg.web_security.user_store_path or str(
        Path(cfg.trade_log.path).with_name("web_users.json")
    )


def default_strategy_center_path(cfg: BotConfig) -> str:
    return cfg.strategy_center.path or str(
        Path(cfg.trade_log.path).with_name("strategy_center.sqlite3")
    )


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
        rotate_jsonl_log_if_needed(
            path,
            max_bytes=cfg.trade_log.rotate_max_bytes,
            keep_files=cfg.trade_log.rotate_keep_files,
            compress=cfg.trade_log.rotate_compress,
        )
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
        "spot_grid_overrides": _dataclass_overrides(
            raw.get("spot_grid_overrides"),
            cfg.spot_grid,
        ),
        "dca_overrides": _dataclass_overrides(
            raw.get("dca_overrides"),
            cfg.dca,
        ),
        "execution_algo_overrides": _dataclass_overrides(
            raw.get("execution_algo_overrides"),
            cfg.execution_algo,
        ),
        "backtest_overrides": _dataclass_overrides(
            raw.get("backtest_overrides"),
            cfg.backtest,
        ),
        "strategy_paused": {
            key: bool(value)
            for key, value in (raw.get("strategy_paused") or {}).items()
            if key in STRATEGY_IDS
        },
    }
    if raw.get("market_maker_instances") is not None:
        try:
            data["market_maker_instances"] = market_maker_configs_to_list(
                market_maker_configs_from_payload(
                    raw.get("market_maker_instances"),
                    base_configs=market_maker_configs_for_runtime(cfg),
                )
            )
        except (TypeError, ValueError) as exc:
            return {
                "loaded": False,
                "path": str(path),
                "error": f"invalid market_maker_instances in runtime store: {exc}",
                "data": {},
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
    maker_configs = market_maker_configs_for_runtime(cfg)
    primary_maker = maker_configs[0] if maker_configs else cfg.market_maker
    primary_conversion = (
        market_maker_quote_conversion(cfg, primary_maker.symbol)
        if primary_maker.symbol
        else {
            "quote_currency": "",
            "common_quote_currency": cfg.common_quote_currency,
            "quote_to_common_rate": None,
            "available": False,
        }
    )
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
            "triangular_arbitrage": asdict(cfg.triangular_arbitrage),
            "contract_strategies": contract_strategies_config_to_dict(
                cfg.contract_strategies
            ),
            "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
            "derivative_exchanges": exchange_configs_to_list(
                cfg.derivative_exchanges
            ),
            "strategy_universe": strategy_universe_to_dict(cfg),
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
        "derivatives": {
            "status": "disabled" if not cfg.derivative_exchanges else "starting",
            "accounts": [],
            "position_count": 0,
            "checked_account_count": 0,
            "total_account_count": len(cfg.derivative_exchanges),
            "funding_rate_count": 0,
            "limits": {
                "max_derivative_leverage": cfg.risk.max_derivative_leverage,
                "min_liquidation_buffer_pct": cfg.risk.min_liquidation_buffer_pct,
                "max_margin_usage_pct": cfg.risk.max_margin_usage_pct,
            },
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "funding_basis": {
            "status": "disabled",
            "mode": "paper",
            "rows": [],
            "candidate_count": 0,
            "configured_count": 0,
            "checked_count": 0,
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "options_arbitrage": {
            "status": "disabled" if not cfg.option_combos else "starting",
            "mode": "paper",
            "rows": [],
            "option_chain": [],
            "strategy_candidates": [],
            "risk": {
                "status": "disabled" if not cfg.option_combos else "starting",
                "total_delta": None,
                "total_gamma": None,
                "total_vega": None,
                "total_theta": None,
                "greeks_available_count": 0,
                "chain_option_count": 0,
                "expiry_concentration": [],
                "expiry_reminders": [],
                "blocked_new_open_count": 0,
                "max_loss_quote": None,
                "max_profit_quote": None,
                "break_even_points": [],
                "controls": {
                    "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
                    "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
                    "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
                    "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
                    "paper_mode_only": True,
                    "auto_submit_live_orders": False,
                },
                "updated_at": None,
            },
            "execution_controls": {
                "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
                "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
                "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
                "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
                "paper_mode_only": True,
                "auto_submit_live_orders": False,
            },
            "opportunities": [],
            "candidate_count": 0,
            "parity_candidate_count": 0,
            "enhanced_candidate_count": 0,
            "configured_count": len(cfg.option_combos),
            "checked_count": 0,
            "thresholds": {
                "notional_quote": cfg.options_arbitrage.notional_quote,
                "min_edge_quote": cfg.options_arbitrage.min_edge_quote,
                "min_edge_bps": cfg.options_arbitrage.min_edge_bps,
                "max_contracts": cfg.options_arbitrage.max_contracts,
                "max_days_to_expiry": cfg.options_arbitrage.max_days_to_expiry,
                "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
                "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
                "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
                "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
            },
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "contract_strategies": build_contract_strategies_payload(
            cfg,
            funding_basis={},
            derivatives={},
            market_maker={},
            order_activity={},
        ),
        "execution_protection": {
            "status": "disabled",
            "mode": "paper",
            "protection_count": 0,
            "ok_count": 0,
            "blocked_count": 0,
            "warning_count": 0,
            "manual_review_count": 0,
            "slippage_block_count": 0,
            "stale_block_count": 0,
            "rows": [],
            "top_reasons": [],
            "updated_at": None,
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
        "strategy_center": build_strategy_center_payload(cfg),
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
            "config": market_maker_config_to_dict(primary_maker),
            "instances": market_maker_configs_to_list(maker_configs),
            "accounts": slow_execution_accounts(
                _all_account_exchanges(cfg),
                _market_maker_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": primary_conversion,
            "safety": build_market_maker_safety_payload(
                cfg,
                None,
                primary_conversion,
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
                spot_markets=cfg.spot_markets,
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
        "spot_grid": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": spot_grid_config_to_dict(cfg.spot_grid),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _grid_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.spot_grid.symbol)
                if cfg.spot_grid.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "safety": None,
            "error": None,
        },
        "dca": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": dca_config_to_dict(cfg.dca),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _grid_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.dca.symbol)
                if cfg.dca.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "safety": None,
            "error": None,
        },
        "execution_algo": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": execution_algo_config_to_dict(cfg.execution_algo),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _execution_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.execution_algo.symbol)
                if cfg.execution_algo.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "safety": None,
            "error": None,
        },
        "backtest": {
            "status": "disabled",
            "mode": "research",
            "result": None,
            "config": backtest_config_to_dict(cfg.backtest),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _execution_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.backtest.symbol)
                if cfg.backtest.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
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


from .state import MonitorState

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
    risk_cfg = market_maker_risk_config(cfg)
    limits = {
        "max_order_quote": risk_cfg.max_order_quote,
        "max_cycle_quote": risk_cfg.max_cycle_quote,
        "max_orders_per_cycle": risk_cfg.max_orders_per_cycle,
        "max_open_orders": risk_cfg.max_open_orders,
        "max_cancels_per_cycle": risk_cfg.max_cancels_per_cycle,
        "min_seconds_between_cancels": risk_cfg.min_seconds_between_cancels,
        "max_daily_loss_quote": risk_cfg.max_daily_loss_quote,
        "max_exposure_quote": risk_cfg.max_exposure_quote,
        "min_order_book_depth_quote": risk_cfg.min_order_book_depth_quote,
        "max_slippage_bps": risk_cfg.max_slippage_bps,
        "max_order_book_age_seconds": risk_cfg.max_order_book_age_seconds,
        "max_order_book_gap_bps": risk_cfg.max_order_book_gap_bps,
        "max_price_jump_bps": risk_cfg.max_price_jump_bps,
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
        risk_cfg,
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


def _build_market_maker_instance_payload(
    cfg: BotConfig,
    maker_cfg: MarketMakerConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    instance_cfg = replace(cfg, market_maker=maker_cfg)
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
            "safety": build_market_maker_safety_payload(instance_cfg, None, conversion),
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
                instance_cfg,
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
                instance_cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "market_data": order_book_market_data(book),
            "runtime": {},
            "error": str(exc),
        }

    safety = build_market_maker_safety_payload(instance_cfg, plan, conversion)
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


def build_market_maker_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    *,
    base_cfg: BotConfig | None = None,
) -> dict[str, Any]:
    maker_configs = market_maker_configs_for_runtime(cfg)
    accounts = slow_execution_accounts(
        _all_account_exchanges(cfg),
        market_maker_symbols_for_accounts(cfg, base_cfg=base_cfg),
        spot_markets=cfg.spot_markets,
    )
    instances = [
        _build_market_maker_instance_payload(cfg, maker_cfg, books, accounts)
        for maker_cfg in maker_configs
    ]
    if not instances:
        maker_cfg = market_maker_config_with_id(cfg.market_maker)
        instances = [
            _build_market_maker_instance_payload(cfg, maker_cfg, books, accounts)
        ]
    primary = dict(instances[0])
    primary["instances"] = instances
    primary["instance_count"] = len(instances)
    primary["active_instance_count"] = sum(
        1 for item in instances if item.get("status") not in {"disabled", "paused"}
    )
    return primary


def build_slow_execution_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    config_payload = slow_execution_config_to_dict(exec_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _spot_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
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


def _strategy_quote_conversion(cfg: BotConfig, symbol: str) -> dict[str, Any]:
    if not symbol:
        return {
            "quote_currency": "",
            "common_quote_currency": cfg.common_quote_currency,
            "quote_to_common_rate": None,
            "available": False,
        }
    return market_maker_quote_conversion(cfg, symbol)


def _converted_market_context(
    *,
    exchange: str,
    symbol: str,
    best_bid: float,
    best_ask: float,
    mid_price: float,
    bid_depth_quote: float,
    ask_depth_quote: float,
    max_level_gap_bps: float,
    order_book_timestamp_ms: int | None,
    order_book_received_at: float | None,
    quote_rate_for_risk: float,
) -> RiskMarketContext:
    return RiskMarketContext(
        exchange=exchange,
        symbol=symbol,
        best_bid=best_bid * quote_rate_for_risk,
        best_ask=best_ask * quote_rate_for_risk,
        mid_price=mid_price * quote_rate_for_risk,
        bid_depth_quote=bid_depth_quote * quote_rate_for_risk,
        ask_depth_quote=ask_depth_quote * quote_rate_for_risk,
        max_level_gap_bps=max_level_gap_bps,
        order_book_timestamp_ms=order_book_timestamp_ms,
        order_book_received_at=order_book_received_at,
    )


def _strategy_safety_base(
    cfg: BotConfig,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "approved": False,
        "level": "blocked" if error else "disabled",
        "currency": cfg.common_quote_currency,
        "quote_conversion": conversion,
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


def build_spot_grid_safety_payload(
    cfg: BotConfig,
    plan: Any | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    base_payload = _strategy_safety_base(cfg, conversion, error=error)
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    quote_values = [
        order.quote_notional * quote_rate_for_risk for order in plan.orders
    ]
    risk_orders = [
        RiskOrder(
            strategy="spot_grid",
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
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="spot_grid",
        live=True,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=_converted_market_context(
            exchange=plan.exchange,
            symbol=plan.symbol,
            best_bid=plan.best_bid,
            best_ask=plan.best_ask,
            mid_price=plan.mid_price,
            bid_depth_quote=plan.bid_depth_quote,
            ask_depth_quote=plan.ask_depth_quote,
            max_level_gap_bps=plan.max_level_gap_bps,
            order_book_timestamp_ms=plan.order_book_timestamp_ms,
            order_book_received_at=plan.order_book_received_at,
            quote_rate_for_risk=quote_rate_for_risk,
        ),
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        expected_cancel_count=len(plan.orders),
        post_only=cfg.spot_grid.post_only,
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    if plan.status != "planned":
        reasons.append(plan.reason)
    if cfg.spot_grid.max_position_base > 0:
        base_asset = _base_currency_from_symbol(plan.symbol)
        current_base = portfolio_positions_base(cfg.portfolio).get(base_asset, 0.0)
        buy_base = sum(order.amount for order in plan.orders if order.side == "buy")
        projected_base = current_base + buy_base
        if projected_base > cfg.spot_grid.max_position_base:
            reasons.append(
                f"{base_asset} projected grid position {projected_base:.8f} exceeds "
                f"spot_grid.max_position_base {cfg.spot_grid.max_position_base:.8f}"
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
        "reasons": _dedupe_readiness_messages(reasons),
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": _dedupe_readiness_messages(reasons),
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "grid_step_bps": plan.grid_step_bps,
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }


def build_dca_safety_payload(
    cfg: BotConfig,
    plan: Any | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    base_payload = _strategy_safety_base(cfg, conversion, error=error)
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    order = plan.next_order
    risk_orders = [
        RiskOrder(
            strategy="dca",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price * quote_rate_for_risk,
            quote_notional=order.quote_notional * quote_rate_for_risk,
        )
    ] if order is not None else []
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="dca",
        live=True,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=_converted_market_context(
            exchange=plan.exchange,
            symbol=plan.symbol,
            best_bid=plan.best_bid,
            best_ask=plan.best_ask,
            mid_price=plan.mid_price,
            bid_depth_quote=plan.bid_depth_quote,
            ask_depth_quote=plan.ask_depth_quote,
            max_level_gap_bps=plan.max_level_gap_bps,
            order_book_timestamp_ms=plan.order_book_timestamp_ms,
            order_book_received_at=plan.order_book_received_at,
            quote_rate_for_risk=quote_rate_for_risk,
        ),
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        post_only=plan.price_mode == "maker",
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    if plan.status not in {"ready", "waiting_for_trigger"}:
        reasons.append(plan.reason)
    if cfg.dca.max_position_base > 0 and order is not None:
        base_asset = _base_currency_from_symbol(plan.symbol)
        current_base = portfolio_positions_base(cfg.portfolio).get(base_asset, 0.0)
        projected_base = (
            current_base + order.amount
            if order.side == "buy"
            else max(0.0, current_base - order.amount)
        )
        if projected_base > cfg.dca.max_position_base:
            reasons.append(
                f"{base_asset} projected DCA position {projected_base:.8f} exceeds "
                f"dca.max_position_base {cfg.dca.max_position_base:.8f}"
            )
    if cfg.dca.max_loss_quote > 0 and cfg.dca.average_entry_price > 0:
        base_asset = _base_currency_from_symbol(plan.symbol)
        current_base = portfolio_positions_base(cfg.portfolio).get(base_asset, 0.0)
        unrealized_loss = max(
            0.0,
            (cfg.dca.average_entry_price - plan.mid_price) * current_base,
        ) * quote_rate_for_risk
        if unrealized_loss > cfg.dca.max_loss_quote:
            reasons.append(
                f"DCA unrealized loss {unrealized_loss:.8f} exceeds "
                f"dca.max_loss_quote {cfg.dca.max_loss_quote:.8f}"
            )
    approved = len(reasons) == 0
    quote_values = [
        row["quote_notional"] * quote_rate_for_risk
        for row in plan.order_schedule
    ]
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(risk_orders),
        "buy_order_count": sum(1 for risk_order in risk_orders if risk_order.side == "buy"),
        "sell_order_count": sum(1 for risk_order in risk_orders if risk_order.side == "sell"),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": _dedupe_readiness_messages(reasons),
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": _dedupe_readiness_messages(reasons),
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }


def build_execution_algo_safety_payload(
    cfg: BotConfig,
    plan: Any | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    base_payload = _strategy_safety_base(cfg, conversion, error=error)
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    next_slice = plan.next_slice
    risk_orders = [
        RiskOrder(
            strategy="execution_algo",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=next_slice.side,
            amount=next_slice.amount,
            price=next_slice.price * quote_rate_for_risk,
            quote_notional=next_slice.quote_notional * quote_rate_for_risk,
        )
    ] if next_slice is not None else []
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="execution_algo",
        live=True,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=_converted_market_context(
            exchange=plan.exchange,
            symbol=plan.symbol,
            best_bid=plan.best_bid,
            best_ask=plan.best_ask,
            mid_price=plan.mid_price,
            bid_depth_quote=plan.bid_depth_quote,
            ask_depth_quote=plan.ask_depth_quote,
            max_level_gap_bps=plan.max_level_gap_bps,
            order_book_timestamp_ms=plan.order_book_timestamp_ms,
            order_book_received_at=plan.order_book_received_at,
            quote_rate_for_risk=quote_rate_for_risk,
        ),
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        post_only=plan.price_mode == "maker",
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    if plan.status not in {"ready", "waiting_for_start"}:
        reasons.append(plan.reason)
    if plan.max_slippage_bps > cfg.risk.max_slippage_bps:
        warnings.append(
            f"execution max_slippage_bps {plan.max_slippage_bps:.4f} exceeds "
            f"risk.max_slippage_bps {cfg.risk.max_slippage_bps:.4f}"
        )
    approved = len(reasons) == 0
    quote_values = [
        item.quote_notional * quote_rate_for_risk for item in plan.schedule
    ]
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(risk_orders),
        "buy_order_count": sum(1 for risk_order in risk_orders if risk_order.side == "buy"),
        "sell_order_count": sum(1 for risk_order in risk_orders if risk_order.side == "sell"),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": _dedupe_readiness_messages(reasons),
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": _dedupe_readiness_messages(reasons),
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }


def build_spot_grid_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    config_payload = spot_grid_config_to_dict(grid_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _grid_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, grid_cfg.symbol)
    if not grid_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_spot_grid_safety_payload(cfg, None, conversion),
            "error": None,
        }

    book = books.get((grid_cfg.exchange, grid_cfg.symbol))
    if book is None:
        error = f"Missing {grid_cfg.exchange} {grid_cfg.symbol}"
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_spot_grid_safety_payload(cfg, None, conversion, error=error),
            "error": error,
        }

    try:
        plan = build_spot_grid_plan(book, grid_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_spot_grid_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "safety": build_spot_grid_safety_payload(cfg, plan, conversion),
        "error": None,
    }


def build_dca_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    dca_cfg = cfg.dca
    config_payload = dca_config_to_dict(dca_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _grid_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, dca_cfg.symbol)
    if not dca_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_dca_safety_payload(cfg, None, conversion),
            "error": None,
        }

    book = books.get((dca_cfg.exchange, dca_cfg.symbol))
    if book is None:
        error = f"Missing {dca_cfg.exchange} {dca_cfg.symbol}"
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_dca_safety_payload(cfg, None, conversion, error=error),
            "error": error,
        }

    try:
        plan = build_dca_plan(book, dca_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_dca_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "safety": build_dca_safety_payload(cfg, plan, conversion),
        "error": None,
    }


def build_execution_algo_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    exec_cfg = cfg.execution_algo
    config_payload = execution_algo_config_to_dict(exec_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _execution_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, exec_cfg.symbol)
    if not exec_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_execution_algo_safety_payload(cfg, None, conversion),
            "error": None,
        }

    book = books.get((exec_cfg.exchange, exec_cfg.symbol))
    if book is None:
        error = f"Missing {exec_cfg.exchange} {exec_cfg.symbol}"
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_execution_algo_safety_payload(
                cfg,
                None,
                conversion,
                error=error,
            ),
            "error": error,
        }

    try:
        plan = build_execution_algo_plan(book, exec_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_execution_algo_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "safety": build_execution_algo_safety_payload(cfg, plan, conversion),
        "error": None,
    }


def build_backtest_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    backtest_cfg = cfg.backtest
    config_payload = backtest_config_to_dict(backtest_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _execution_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, backtest_cfg.symbol)
    if not backtest_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "research",
            "result": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "error": None,
        }

    current_mid = None
    book = books.get((backtest_cfg.exchange, backtest_cfg.symbol))
    if book is not None and book.bids and book.asks:
        current_mid = (book.bids[0].price + book.asks[0].price) / 2

    try:
        result = run_paper_backtest(
            backtest_cfg,
            spot_grid=cfg.spot_grid,
            dca=cfg.dca,
            execution_algo=cfg.execution_algo,
            current_mid=current_mid,
        )
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "research",
            "result": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "error": str(exc),
        }

    return {
        "status": result.status,
        "mode": "research",
        "result": result.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
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


from .loops import (
    _daily_report_due,
    _market_maker_force_replace_reason,
    _market_maker_order_sync_delta,
    auto_buy_sell_task_loop,
    build_daily_report_message,
    market_maker_task_loop,
    monitor_loop,
    spot_grid_task_loop,
)

def _env_optional(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    return value if value else None


def _web_password(cfg: BotConfig) -> str | None:
    return _env_optional(cfg.web_security.password_env)


# Generated once per process. Used only when neither a cookie secret env var
# nor a web password is configured, so the session signing key is never a
# publicly known constant that would let anyone forge session tokens. Sessions
# are invalidated on restart in that case, which is the safe default for an
# otherwise unconfigured deployment.
_FALLBACK_COOKIE_SECRET = secrets.token_urlsafe(32)


def _cookie_secret(cfg: BotConfig) -> str:
    return (
        _env_optional(cfg.web_security.cookie_secret_env)
        or _web_password(cfg)
        or _FALLBACK_COOKIE_SECRET
    )


def _registration_code(cfg: BotConfig) -> str | None:
    return _env_optional(cfg.web_security.registration_code_env)


def _registration_code_required(cfg: BotConfig) -> bool:
    return bool(cfg.web_security.registration_code_env)


def _user_store(request: web.Request) -> WebUserStore:
    return request.app["web_user_store"]


def _login_rate_limiter(request: web.Request) -> LoginRateLimiter:
    return request.app["login_rate_limiter"]


def _strategy_center_store(request: web.Request) -> StrategyCenterStore:
    return request.app["strategy_center_store"]


def _request_user(request: web.Request) -> WebUser | None:
    email = str(request.get("user_email") or "")
    if not email:
        return None
    try:
        return _user_store(request).get_user(email)
    except ValueError:
        return None


def _owner_email_from_payload(payload: dict[str, Any], user: WebUser | None) -> str:
    if user is None:
        return str(payload.get("owner_email") or "").strip().lower()
    if user.role == "admin":
        return str(payload.get("owner_email") or user.email).strip().lower()
    return user.email


def _require_owner_or_admin(user: WebUser | None, owner_email: str) -> None:
    if user is None or user.role == "admin":
        return
    if str(owner_email or "").strip().lower() != user.email:
        raise PermissionError("user can only manage their own strategy center records")


def _request_is_https(request: web.Request, cfg: BotConfig) -> bool:
    if request.secure:
        return True
    if not cfg.web_security.trust_proxy_headers:
        return False
    return request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _client_ip(request: web.Request, cfg: BotConfig) -> str:
    if cfg.web_security.trust_proxy_headers:
        # X-Real-IP is set wholesale by a well-configured reverse proxy (e.g.
        # nginx's $remote_addr), so it cannot carry a client-supplied value.
        # X-Forwarded-For is normally *appended to* by the proxy
        # (nginx's $proxy_add_x_forwarded_for), so a client can prepend an
        # arbitrary spoofed address; only the rightmost hop added by our
        # immediate trusted proxy is safe to read. Preferring the leftmost
        # entry (or trusting X-Forwarded-For over X-Real-IP) would let any
        # remote client forge the IP used for the allowlist, login lockout,
        # and audit logging.
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.rsplit(",", 1)[-1].strip()
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


def _sign_session(cfg: BotConfig, timestamp: int, email: str = "") -> str:
    secret = _cookie_secret(cfg).encode("utf-8")
    payload = f"{timestamp}:{email}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _make_session_token(cfg: BotConfig, email: str = "") -> str:
    timestamp = int(time.time())
    if email:
        raw = f"v2:{timestamp}:{email}:{_sign_session(cfg, timestamp, email)}"
    else:
        raw = f"{timestamp}:{_sign_session(cfg, timestamp)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _session_identity(cfg: BotConfig, token: str | None) -> tuple[bool, str]:
    if not token:
        return False, ""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        if raw.startswith("v2:"):
            _, timestamp_text, email, signature = raw.split(":", 3)
        else:
            timestamp_text, signature = raw.split(":", 1)
            email = ""
        timestamp = int(timestamp_text)
    except (ValueError, TypeError):
        return False, ""
    if time.time() - timestamp > SESSION_MAX_AGE_SECONDS:
        return False, ""
    valid = hmac.compare_digest(signature, _sign_session(cfg, timestamp, email))
    return valid, email if valid else ""


def _session_valid(cfg: BotConfig, token: str | None) -> bool:
    valid, _ = _session_identity(cfg, token)
    return valid


def _add_security_headers(response: web.StreamResponse) -> web.StreamResponse:
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def _login_html(
    *,
    error: str = "",
    email_login: bool = False,
    registration_enabled: bool = False,
) -> str:
    hint = (
        "Use your registered email, password, and Google Authenticator code."
        if email_login
        else "Use the temporary dashboard password. Email login starts after users are registered."
    )
    return (
        LOGIN_HTML.replace("__ERROR__", html.escape(error))
        .replace("__LOGIN_HINT__", html.escape(hint))
        .replace("__EMAIL_DISPLAY__", "block" if email_login else "none")
        .replace("__REGISTER_DISPLAY__", "block" if registration_enabled else "none")
        .replace("__PASSWORD_AUTOFOCUS__", "" if email_login else "autofocus")
    )


def _register_html(
    *,
    cfg: BotConfig,
    error: str = "",
    user: WebUser | None = None,
) -> str:
    code_required = _registration_code_required(cfg)
    if user is not None:
        uri = totp_provisioning_uri(
            email=user.email,
            secret=user.totp_secret,
            issuer=cfg.web_security.totp_issuer,
        )
        body = f"""
  <div class="panel">
    <h1>2FA Setup</h1>
    <p>User <strong>{html.escape(user.email)}</strong> is registered.</p>
    <p>Add this setup key to Google Authenticator, then sign in with the 6-digit code.</p>
    <label>Setup Key</label>
    <code>{html.escape(user.totp_secret)}</code>
    <label>Authenticator URI</label>
    <code>{html.escape(uri)}</code>
    <a href="/login">Continue to login</a>
  </div>
"""
    else:
        body = f"""
  <form method="post" action="/register">
    <h1>Register User</h1>
    <p>Create an email user and Google Authenticator setup key. Use comma-separated assets such as ACS,BTC.</p>
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="username" autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="new-password">
    <label for="assets">Allowed Assets</label>
    <input id="assets" name="assets" type="text" placeholder="ACS,BTC">
    <div class="code-field">
      <label for="registration_code">Registration Code</label>
      <input id="registration_code" name="registration_code" type="password">
    </div>
    <button type="submit">Register</button>
    <a href="/login">Back to login</a>
    <div class="error">{html.escape(error)}</div>
  </form>
"""
    return (
        REGISTER_HTML.replace("__BODY__", body)
        .replace("__CODE_DISPLAY__", "block" if code_required else "none")
    )


def _email_login_enabled(request: web.Request) -> bool:
    try:
        return _user_store(request).has_users()
    except ValueError:
        return False


async def login_get(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    return web.Response(
        text=_login_html(
            email_login=_email_login_enabled(request),
            registration_enabled=cfg.web_security.registration_enabled,
        ),
        content_type="text/html",
    )


def _login_throttled_response(
    cfg: BotConfig,
    *,
    email_login: bool,
    retry_after: float,
) -> web.Response:
    wait_seconds = max(1, int(retry_after + 0.999))
    response = web.Response(
        text=_login_html(
            error=(
                "Too many failed attempts. "
                f"Try again in about {wait_seconds} seconds."
            ),
            email_login=email_login,
            registration_enabled=cfg.web_security.registration_enabled,
        ),
        content_type="text/html",
        status=429,
    )
    response.headers["Retry-After"] = str(wait_seconds)
    return response


async def login_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    form = await request.post()
    email_login = _email_login_enabled(request)
    supplied_password = str(form.get("password", ""))
    email = str(form.get("email", ""))
    totp = str(form.get("totp", ""))

    limiter = _login_rate_limiter(request)
    throttle_key = _client_ip(request, cfg) or "unknown"
    retry_after = limiter.retry_after(throttle_key)
    if retry_after > 0:
        return _login_throttled_response(
            cfg,
            email_login=email_login,
            retry_after=retry_after,
        )

    if email_login:
        try:
            user = _user_store(request).authenticate(
                email=email,
                password=supplied_password,
                totp=totp,
            )
        except ValueError:
            user = None
        if user is None:
            limiter.register_failure(throttle_key)
            return web.Response(
                text=_login_html(
                    error="Invalid email, password, or authenticator code",
                    email_login=True,
                    registration_enabled=cfg.web_security.registration_enabled,
                ),
                content_type="text/html",
                status=401,
            )
        limiter.register_success(throttle_key)
        response = web.HTTPFound("/")
        response.set_cookie(
            SESSION_COOKIE,
            _make_session_token(cfg, user.email),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=cfg.web_security.cookie_secure and _request_is_https(request, cfg),
            samesite="Strict",
        )
        raise response

    password = _web_password(cfg)
    if not password:
        raise web.HTTPFound("/")
    if not hmac.compare_digest(supplied_password, password):
        limiter.register_failure(throttle_key)
        return web.Response(
            text=_login_html(
                error="Invalid password",
                email_login=False,
                registration_enabled=cfg.web_security.registration_enabled,
            ),
            content_type="text/html",
            status=401,
        )
    limiter.register_success(throttle_key)
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


async def register_get(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    if not cfg.web_security.registration_enabled:
        return web.Response(text="Registration is disabled", status=404)
    return web.Response(
        text=_register_html(cfg=cfg),
        content_type="text/html",
    )


async def register_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    if not cfg.web_security.registration_enabled:
        return web.Response(text="Registration is disabled", status=404)
    form = await request.post()
    expected_code = _registration_code(cfg)
    if _registration_code_required(cfg) and expected_code is None:
        return web.Response(
            text=_register_html(
                cfg=cfg,
                error="Registration code environment variable is not set",
            ),
            content_type="text/html",
            status=503,
        )
    supplied_code = str(form.get("registration_code", ""))
    if expected_code is not None and not hmac.compare_digest(
        supplied_code,
        expected_code,
    ):
        return web.Response(
            text=_register_html(cfg=cfg, error="Invalid registration code"),
            content_type="text/html",
            status=403,
        )
    try:
        user = _user_store(request).create_user(
            email=str(form.get("email", "")),
            password=str(form.get("password", "")),
            allowed_assets=normalize_assets(str(form.get("assets", ""))),
        )
    except ValueError as exc:
        return web.Response(
            text=_register_html(cfg=cfg, error=str(exc)),
            content_type="text/html",
            status=400,
        )
    write_system_web_audit_event(
        cfg,
        action="user_register",
        target=user.email,
        detail="registered web user",
        payload={"email": user.email, "allowed_assets": user.allowed_assets},
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )
    return web.Response(
        text=_register_html(cfg=cfg, user=user),
        content_type="text/html",
    )


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
        async def call_handler() -> web.StreamResponse:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                _add_security_headers(exc)
                raise
            return _add_security_headers(response)

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
            return _add_security_headers(web.Response(text="Forbidden", status=403))

        if request.path in {"/login", "/logout", "/register"}:
            return await call_handler()
        if request.path == "/api/signal" or request.path.startswith("/api/signal/"):
            return await call_handler()

        password = _web_password(cfg)
        email_login = _email_login_enabled(request)
        auth_required = bool(password) or email_login
        if not auth_required:
            return await call_handler()
        if request.path in {
            "/api/health",
            "/api/metrics",
            "/metrics",
        } and _is_local_ip(remote):
            return await call_handler()
        session_valid, session_email = _session_identity(
            cfg,
            request.cookies.get(SESSION_COOKIE),
        )
        if not session_valid or (email_login and not session_email):
            if request.path.startswith("/api/"):
                return _add_security_headers(
                    web.json_response({"error": "authentication required"}, status=401)
                )
            redirect = web.HTTPFound("/login")
            _add_security_headers(redirect)
            raise redirect
        if session_email:
            try:
                if _user_store(request).get_user(session_email) is None:
                    raise ValueError("unknown user")
            except ValueError:
                if request.path.startswith("/api/"):
                    return _add_security_headers(
                        web.json_response({"error": "authentication required"}, status=401)
                    )
                redirect = web.HTTPFound("/login")
                _add_security_headers(redirect)
                raise redirect
            request["user_email"] = session_email
        return await call_handler()

    return security_middleware


async def index(_: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def api_state(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    view = request.query.get("view")
    sections = request.query.get("sections")
    payload = await state.get(view=view, sections=sections)
    runtime_cfg = await state.runtime_config(cfg)
    requesting_user = _request_user(request)
    payload["strategy_center"] = strategy_center_payload_for_view(
        build_strategy_center_payload(
            runtime_cfg,
            request.app["strategy_center_store"],
            user=requesting_user,
        ),
        view=view,
        sections=sections,
    )
    if (
        requesting_user is not None
        and requesting_user.role == "admin"
        and view in (None, "settings")
    ):
        payload["admin_users"] = [
            _public_admin_user_dict(item) for item in _user_store(request).list_users()
        ]
    return web.json_response(
        _filter_state_payload_for_user(
            payload,
            cfg=runtime_cfg,
            user=requesting_user,
        )
    )


async def api_profile(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    user = _request_user(request)
    runtime_cfg = await state.runtime_config(cfg)
    if user is None:
        return web.json_response(
            {
                "mode": "legacy",
                "available_assets": _configured_assets(runtime_cfg),
            }
        )
    try:
        payload = await request.json()
        preferred_asset = str(payload.get("preferred_asset", "")).strip().upper()
        if preferred_asset and preferred_asset not in _configured_assets(runtime_cfg):
            raise ValueError(f"unknown asset: {preferred_asset}")
        updated = _user_store(request).update_profile(
            email=user.email,
            preferred_asset=preferred_asset,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="user_profile",
        target=updated.email,
        detail="updated preferred asset",
        payload={"preferred_asset": updated.preferred_asset},
    )
    return web.json_response(
        {
            "ok": True,
            "profile": updated.public_dict(available_assets=_configured_assets(runtime_cfg)),
        }
    )


def _public_admin_user_dict(user: WebUser) -> dict[str, Any]:
    return {
        "email": user.email,
        "role": user.role,
        "totp_enabled": user.totp_enabled,
        "allowed_assets": user.allowed_assets,
        "preferred_asset": user.preferred_asset,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


async def api_admin_users(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    store = _user_store(request)
    audit_action = ""
    audit_target = ""
    audit_detail = ""
    audit_payload: dict[str, Any] = {}
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "").strip().lower()
        email = str(payload.get("email") or "").strip()

        if action == "list":
            pass
        elif action == "create_user":
            created = store.admin_create_user(
                email=email,
                password=str(payload.get("password") or ""),
                role=str(payload.get("role") or "user"),
                allowed_assets=payload.get("allowed_assets"),
                preferred_asset=str(payload.get("preferred_asset") or ""),
            )
            audit_action = "admin_user_create"
            audit_target = created.email
            audit_detail = f"created user with role {created.role}"
            audit_payload = {"email": created.email, "role": created.role}
        elif action == "update_user":
            if not email:
                raise ValueError("email is required")
            role_provided = "role" in payload
            allowed_assets_provided = "allowed_assets" in payload
            preferred_asset_provided = "preferred_asset" in payload
            new_password = str(payload.get("new_password") or "")
            updated = store.admin_update_user(
                email=email,
                role=str(payload.get("role") or "") if role_provided else None,
                allowed_assets=payload.get("allowed_assets"),
                allowed_assets_provided=allowed_assets_provided,
                preferred_asset=(
                    str(payload.get("preferred_asset") or "")
                    if preferred_asset_provided
                    else None
                ),
                preferred_asset_provided=preferred_asset_provided,
                new_password=new_password or None,
            )
            changes = [
                name
                for name, touched in (
                    ("role", role_provided),
                    ("assets", allowed_assets_provided or preferred_asset_provided),
                    ("password", bool(new_password)),
                )
                if touched
            ]
            audit_action = "admin_user_update"
            audit_target = updated.email
            audit_detail = "updated " + ", ".join(changes)
            audit_payload = {
                "email": updated.email,
                "role": updated.role,
                "allowed_assets": updated.allowed_assets,
                "preferred_asset": updated.preferred_asset,
                "changed_fields": changes,
            }
        elif action == "delete_user":
            if not email:
                raise ValueError("email is required")
            store.admin_delete_user(email=email)
            audit_action = "admin_user_delete"
            audit_target = email
            audit_detail = "deleted user"
            audit_payload = {"email": email}
        else:
            raise ValueError("unsupported admin users action")
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if audit_action:
        write_web_audit_event(
            cfg,
            request,
            action=audit_action,
            target=audit_target,
            detail=audit_detail,
            payload=audit_payload,
        )
    return web.json_response(
        {
            "ok": True,
            "users": [_public_admin_user_dict(item) for item in store.list_users()],
        }
    )


async def api_control(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)

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
            spot_markets=runtime_cfg.spot_markets,
        )
        allowed_exchanges = {account["key"] for account in accounts}
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges=allowed_exchanges,
            symbols_by_exchange=symbols_by_exchange,
        )
        base_config = await state.slow_execution_config(runtime_cfg.slow_execution)
        target_symbol = str(overrides.get("symbol") or base_config.symbol)
        _require_user_assets(_request_user(request), [
            _base_asset_from_symbol(target_symbol)
        ])
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
                spot_markets=runtime_cfg.spot_markets,
            ),
        }
    )


async def api_spot_grid(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _grid_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _spot_grid_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.spot_grid_config(runtime_cfg.spot_grid)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(_request_user(request), [
            _base_asset_from_symbol(target_symbol)
        ])
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_spot_grid_overrides(overrides, cfg=cfg)
    current_config = await state.spot_grid_config(cfg.spot_grid)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="spot_grid_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated Spot Grid config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": spot_grid_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _grid_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_dca(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _grid_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _dca_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.dca_config(runtime_cfg.dca)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(_request_user(request), [
            _base_asset_from_symbol(target_symbol)
        ])
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_dca_overrides(overrides, cfg=cfg)
    current_config = await state.dca_config(cfg.dca)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="dca_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated DCA Bot config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": dca_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _grid_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_execution_algo(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _execution_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _execution_algo_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.execution_algo_config(
            runtime_cfg.execution_algo
        )
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(_request_user(request), [
            _base_asset_from_symbol(target_symbol)
        ])
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_execution_algo_overrides(overrides, cfg=cfg)
    current_config = await state.execution_algo_config(cfg.execution_algo)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="execution_algo_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated TWAP/VWAP/POV config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": execution_algo_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _execution_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_backtest(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _execution_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _backtest_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.backtest_config(runtime_cfg.backtest)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(_request_user(request), [
            _base_asset_from_symbol(target_symbol)
        ])
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_backtest_overrides(overrides, cfg=cfg)
    current_config = await state.backtest_config(cfg.backtest)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="backtest_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated backtest config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": backtest_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _execution_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


def _strategy_center_response_payload(
    request: web.Request,
    cfg: BotConfig,
) -> dict[str, Any]:
    return build_strategy_center_payload(
        cfg,
        _strategy_center_store(request),
        user=_request_user(request),
    )


def _strategy_center_existing_row(
    rows: list[dict[str, Any]],
    row_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            return row
    raise ValueError(f"{label} not found: {row_id}")


def _strategy_center_optional_row(
    rows: list[dict[str, Any]],
    row_id: str,
) -> dict[str, Any] | None:
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            return row
    return None


def _strategy_payload_from_request(
    payload: dict[str, Any],
    *,
    user: WebUser | None,
    existing: dict[str, Any] | None = None,
) -> StrategyInstance:
    raw = dict(existing or {})
    raw.update(payload.get("strategy") if isinstance(payload.get("strategy"), dict) else payload)
    raw["owner_email"] = _owner_email_from_payload(raw, user)
    strategy = StrategyInstance.from_dict(raw)
    _require_owner_or_admin(user, strategy.owner_email)
    _require_user_assets(user, [strategy.asset or _base_asset_from_symbol(strategy.symbol)])
    return strategy


def _api_account_payload_from_request(
    payload: dict[str, Any],
    *,
    user: WebUser | None,
    existing: dict[str, Any] | None = None,
) -> UserApiAccount:
    raw = dict(existing or {})
    raw.update(payload.get("account") if isinstance(payload.get("account"), dict) else payload)
    raw["owner_email"] = _owner_email_from_payload(raw, user)
    account = UserApiAccount.from_dict(raw)
    _require_owner_or_admin(user, account.owner_email)
    _require_user_assets(user, account.asset_scope)
    return account


async def api_strategy_center(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    store = _strategy_center_store(request)
    user = _request_user(request)
    try:
        if not cfg.strategy_center.enabled:
            raise ValueError("strategy center is disabled")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            raise ValueError("action is required")
        runtime_cfg = await state.runtime_config(cfg)
        store_payload = store.read()

        if action in {"create_strategy", "update_strategy", "upsert_strategy"}:
            existing = None
            strategy_id = str(payload.get("id") or payload.get("strategy_id") or "").strip()
            strategy_raw = payload.get("strategy")
            if isinstance(strategy_raw, dict):
                strategy_id = str(strategy_raw.get("id") or strategy_id).strip()
            if action == "update_strategy" and strategy_id:
                existing = _strategy_center_existing_row(
                    store_payload["strategy_instances"],
                    strategy_id,
                    label="strategy",
                )
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            elif action == "upsert_strategy" and strategy_id:
                existing = _strategy_center_optional_row(
                    store_payload["strategy_instances"],
                    strategy_id,
                )
            if existing is not None:
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            strategy = _strategy_payload_from_request(
                payload,
                user=user,
                existing=existing,
            )
            store_payload = store.upsert_strategy(strategy)
            audit_action = "strategy_center_strategy"
            target = strategy.id
            detail = f"{action} {strategy.name}"
            audit_payload = strategy.summary()
        elif action == "delete_strategy":
            strategy_id = str(payload.get("id") or payload.get("strategy_id") or "").strip()
            if not strategy_id:
                raise ValueError("strategy_id is required")
            existing = _strategy_center_existing_row(
                store_payload["strategy_instances"],
                strategy_id,
                label="strategy",
            )
            _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            _require_user_assets(
                user,
                [str(existing.get("asset") or _base_asset_from_symbol(str(existing.get("symbol") or "")))],
            )
            store_payload = store.delete_strategy(strategy_id)
            audit_action = "strategy_center_strategy_delete"
            target = strategy_id
            detail = "deleted strategy instance"
            audit_payload = {"strategy_id": strategy_id}
        elif action in {"create_account", "update_account", "upsert_account"}:
            existing = None
            account_id = str(payload.get("id") or payload.get("account_id") or "").strip()
            account_raw = payload.get("account")
            if isinstance(account_raw, dict):
                account_id = str(account_raw.get("id") or account_id).strip()
            if action == "update_account" and account_id:
                existing = _strategy_center_existing_row(
                    store_payload["user_api_accounts"],
                    account_id,
                    label="api account",
                )
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            elif action == "upsert_account" and account_id:
                existing = _strategy_center_optional_row(
                    store_payload["user_api_accounts"],
                    account_id,
                )
            if existing is not None:
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            account = _api_account_payload_from_request(
                payload,
                user=user,
                existing=existing,
            )
            store_payload = store.upsert_api_account(account)
            audit_action = "strategy_center_api_account"
            target = account.id
            detail = f"{action} {account.label}"
            audit_payload = account.public_dict()
        elif action == "delete_account":
            account_id = str(payload.get("id") or payload.get("account_id") or "").strip()
            if not account_id:
                raise ValueError("account_id is required")
            existing = _strategy_center_existing_row(
                store_payload["user_api_accounts"],
                account_id,
                label="api account",
            )
            _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            _require_user_assets(user, list(existing.get("asset_scope") or []))
            store_payload = store.delete_api_account(account_id)
            audit_action = "strategy_center_api_account_delete"
            target = account_id
            detail = "deleted api account reference"
            audit_payload = {"account_id": account_id}
        elif action == "update_funding":
            raw = (
                payload.get("funding_arbitrage")
                if isinstance(payload.get("funding_arbitrage"), dict)
                else payload
            )
            funding = FundingArbitrageSettings.from_dict(raw)
            _require_user_assets(
                user,
                [
                    _base_asset_from_symbol(funding.spot_symbol),
                    _base_asset_from_symbol(funding.derivative_symbol),
                ],
            )
            store_payload = store.update_funding(funding)
            audit_action = "strategy_center_funding"
            target = funding.pair_id or funding.spot_symbol
            detail = "updated funding arbitrage settings"
            audit_payload = funding.to_dict()
        elif action == "update_signal_bot":
            raw = (
                payload.get("signal_bot")
                if isinstance(payload.get("signal_bot"), dict)
                else payload
            )
            signal_bot = SignalBotSettings.from_dict(raw)
            store_payload = store.update_signal_bot(signal_bot)
            audit_action = "strategy_center_signal_bot"
            target = "signal_bot"
            detail = "updated signal bot settings"
            audit_payload = signal_bot.to_dict()
        else:
            raise ValueError("unsupported strategy center action")
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    write_web_audit_event(
        runtime_cfg,
        request,
        action=audit_action,
        target=target,
        detail=detail,
        payload=audit_payload,
    )
    return web.json_response(
        {
            "ok": True,
            "strategy_center": build_strategy_center_public_payload(
                store_payload,
                current_user_email=user.email if user else "",
                current_user_role=user.role if user else "admin",
                allowed_assets=user.allowed_assets if user else [],
            ),
        }
    )


async def _json_or_text_payload(request: web.Request) -> dict[str, Any]:
    content_type = request.content_type.lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("signal payload must be an object")
        return payload
    text_payload = (await request.text()).strip()
    if not text_payload:
        return {}
    try:
        payload = json.loads(text_payload)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"message": text_payload}


def _signal_secret_from_request(
    request: web.Request,
    payload: dict[str, Any],
) -> str:
    return str(
        request.headers.get("X-Signal-Secret")
        or request.headers.get("X-Webhook-Secret")
        or request.query.get("secret")
        or payload.get("secret")
        or ""
    )


def _signal_payload_without_secret(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if str(key).lower() not in {"secret", "token", "webhook_secret"}
    }


async def api_signal_webhook(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    store = _strategy_center_store(request)
    source = str(request.match_info.get("source") or "custom").strip().lower()
    try:
        if not cfg.strategy_center.enabled:
            raise PermissionError("strategy center is disabled")
        payload = await _json_or_text_payload(request)
        store_payload = store.read()
        signal_bot = SignalBotSettings.from_dict(store_payload.get("signal_bot", {}))
        if not signal_bot.enabled:
            raise PermissionError("signal bot is disabled")
        if source == "custom" and not signal_bot.allow_custom_webhook:
            raise PermissionError("custom webhook is disabled")
        if source not in signal_bot.allowed_sources:
            raise PermissionError(f"signal source is not allowed: {source}")
        expected_secret = (
            os.environ.get(signal_bot.webhook_secret_env)
            if signal_bot.webhook_secret_env
            else None
        )
        if not expected_secret:
            raise PermissionError("signal webhook secret environment variable is not set")
        supplied_secret = _signal_secret_from_request(request, payload)
        if not hmac.compare_digest(supplied_secret, expected_secret):
            raise PermissionError("invalid signal webhook secret")

        clean_payload = _signal_payload_without_secret(payload)
        strategy_id = str(
            clean_payload.get("strategy_id")
            or signal_bot.default_strategy_id
            or ""
        ).strip()
        strategies = {
            str(item.get("id")): item
            for item in store_payload.get("strategy_instances", [])
            if isinstance(item, dict)
        }
        strategy = strategies.get(strategy_id) if strategy_id else None
        status = "accepted"
        reason = "stored only; execution requires strategy runner and risk approval"
        if strategy_id and strategy is None:
            status = "blocked"
            reason = "strategy_id is not registered"
        elif strategy is not None and not bool(strategy.get("enabled")):
            status = "blocked"
            reason = "strategy is disabled"
        event = SignalEvent.from_payload(
            clean_payload,
            source=source,
            default_strategy_id=signal_bot.default_strategy_id,
            status=status,
            reason=reason,
        )
        store_payload = store.append_signal(event)
    except PermissionError as exc:
        write_system_web_audit_event(
            cfg,
            action="signal_webhook",
            status="blocked",
            target=source,
            detail="rejected signal webhook",
            payload={},
            error=str(exc),
            actor_ip=_client_ip(request, cfg),
            path=request.path,
            method=request.method,
            user_agent=str(request.headers.get("User-Agent", ""))[:160],
        )
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    write_system_web_audit_event(
        cfg,
        action="signal_webhook",
        target=event.id,
        detail=f"received {source} signal",
        payload={
            "id": event.id,
            "source": event.source,
            "strategy_id": event.strategy_id,
            "symbol": event.symbol,
            "side": event.side,
            "action": event.action,
            "status": event.status,
            "reason": event.reason,
        },
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )
    return web.json_response(
        {
            "ok": True,
            "signal": event.to_dict(),
            "strategy_center": build_strategy_center_public_payload(store_payload),
        }
    )


async def api_market_maker(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = market_maker_symbols_for_accounts(
            runtime_cfg,
            base_cfg=cfg,
        )
        allowed_exchanges = {
            exchange.key for exchange in _all_account_exchanges(runtime_cfg)
        }
        current_instances = market_maker_configs_for_runtime(runtime_cfg)
        action = "upsert"
        if isinstance(payload, dict) and "instances" in payload:
            updated_instances = market_maker_configs_from_payload(
                payload["instances"],
                base_configs=current_instances,
                allowed_exchanges=allowed_exchanges,
                symbols_by_exchange=symbols_by_exchange,
                repair_stale_identity_id=True,
            )
            action = "replace"
        elif isinstance(payload, dict) and payload.get("delete_id"):
            delete_id = str(payload["delete_id"]).strip()
            updated_instances = [
                instance
                for instance in current_instances
                if instance.id != delete_id
            ]
            action = "delete"
        else:
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            target_id = str(payload.get("id") or "").strip()
            base_config = next(
                (
                    instance
                    for instance in current_instances
                    if instance.id == target_id
                ),
                current_instances[0] if current_instances else None,
            )
            updated_config = market_maker_config_from_payload(
                payload,
                base_config=base_config,
                allowed_exchanges=allowed_exchanges,
                symbols_by_exchange=symbols_by_exchange,
                repair_stale_identity_id=True,
            )
            replaced_instance = False
            updated_instances = []
            for instance in current_instances:
                if (target_id and instance.id == target_id) or (
                    not target_id and instance.id == updated_config.id
                ):
                    updated_instances.append(updated_config)
                    replaced_instance = True
                else:
                    updated_instances.append(instance)
            if not replaced_instance:
                updated_instances.append(updated_config)
        _require_user_assets(
            _request_user(request),
            [
                _base_asset_from_symbol(instance.symbol)
                for instance in updated_instances
                if instance.symbol
            ],
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_market_maker_instances(updated_instances, cfg=cfg)
    runtime_cfg = await state.runtime_config(cfg)
    current_instances = market_maker_configs_for_runtime(runtime_cfg)
    current_config = current_instances[0] if current_instances else runtime_cfg.market_maker
    write_web_audit_event(
        runtime_cfg,
        request,
        action="market_maker_config",
        target=", ".join(
            f"{instance.id}:{instance.exchange} {instance.symbol}".strip()
            for instance in current_instances
        ),
        detail=f"{action} Market Maker config",
        payload={
            "action": action,
            "instances": market_maker_configs_to_list(current_instances),
        },
    )
    return web.json_response(
        {
            "ok": True,
            "config": market_maker_config_to_dict(current_config),
            "instances": market_maker_configs_to_list(current_instances),
            **update,
        }
    )


async def api_markets(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        markets = _spot_markets_from_payload(
            payload,
            allowed_exchanges={exchange.key for exchange in cfg.spot_exchanges},
        )
        _require_user_assets(_request_user(request), _assets_from_spot_markets(markets))
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
        _require_admin_user(_request_user(request))
        payload = await request.json()
        pairs = _cash_and_carry_pairs_from_payload(payload)
        _require_user_assets(
            _request_user(request),
            _assets_from_cash_and_carry_pairs(pairs),
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
            spot_markets=runtime_cfg.spot_markets,
        )
        allowed_exchanges = {account["key"] for account in accounts}
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges=allowed_exchanges,
            symbols_by_exchange=symbols_by_exchange,
        )
        base_config = await state.slow_execution_config(cfg.slow_execution)
        task_config = replace(base_config, **{**overrides, "enabled": True})
        _require_user_assets(_request_user(request), [
            _base_asset_from_symbol(task_config.symbol)
        ])
        validate_task_config(task_config)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
        task_snapshot = await tasks.snapshot()
        task_row = next(
            (
                item
                for item in task_snapshot.get("tasks", [])
                if isinstance(item, dict) and item.get("id") == task_id
            ),
            None,
        )
        if isinstance(task_row, dict):
            task_config = (
                task_row.get("config")
                if isinstance(task_row.get("config"), dict)
                else {}
            )
            _require_user_assets(
                _request_user(request),
                [_base_asset_from_symbol(str(task_config.get("symbol") or ""))],
            )
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
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
        _require_admin_user(_request_user(request))
        payload = await request.json()
        if not bool(payload.get("terminal_only", True)):
            raise ValueError("only terminal task cleanup is supported")
        preview_only = bool(payload.get("preview") or payload.get("dry_run"))
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if preview_only:
        result = await tasks.preview_terminal_tasks()
        return web.json_response({"ok": True, "preview": True, **result})

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
        _require_admin_user(_request_user(request))
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
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
        _require_user_assets(
            _request_user(request),
            [_base_asset_from_symbol(str(payload.get("symbol") or ""))],
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
        _require_admin_user(_request_user(request))
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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
        _require_admin_user(_request_user(request))
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
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
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


async def api_metrics(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    payload = await state.get()
    return web.Response(
        text=render_prometheus_metrics(payload),
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


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
    web_user_store = WebUserStore(default_web_user_store_path(cfg))
    strategy_center_store = StrategyCenterStore(
        default_strategy_center_path(cfg),
        max_recent_signals=cfg.strategy_center.max_recent_signals,
    )
    app["monitor_state"] = state
    app["config"] = cfg
    app["auto_buy_sell_tasks"] = auto_buy_sell_tasks
    app["web_user_store"] = web_user_store
    app["strategy_center_store"] = strategy_center_store
    app["login_rate_limiter"] = LoginRateLimiter()

    async def monitor_context(app_: web.Application) -> Any:
        monitor_task = asyncio.create_task(
            monitor_loop(
                cfg,
                strategy,
                state,
                interval,
                strategy_center_store=strategy_center_store,
            )
        )
        mm_task = asyncio.create_task(market_maker_task_loop(cfg, state))
        grid_task = asyncio.create_task(spot_grid_task_loop(cfg, state))
        auto_task = asyncio.create_task(
            auto_buy_sell_task_loop(cfg, state, auto_buy_sell_tasks)
        )
        app_["monitor_task"] = monitor_task
        app_["market_maker_task"] = mm_task
        app_["spot_grid_task"] = grid_task
        app_["auto_buy_sell_task"] = auto_task
        try:
            yield
        finally:
            monitor_task.cancel()
            mm_task.cancel()
            grid_task.cancel()
            auto_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            with contextlib.suppress(asyncio.CancelledError):
                await mm_task
            with contextlib.suppress(asyncio.CancelledError):
                await grid_task
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
        choices=[
            "all",
            "spot-spread",
            "cash-and-carry",
            "options-arbitrage",
            "triangular-arbitrage",
        ],
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
    configure_logging()
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    app = create_app(cfg, args.strategy, args.poll_seconds)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
