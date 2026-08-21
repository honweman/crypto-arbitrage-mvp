from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any



from ..security import (
    default_web_audit_path,
    read_recent_web_audit_events,
)

from ...account_check import (
    _auth_env_status,
)
from ...config import (
    BotConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
)
from ...fill_store import load_daily_pnl_summary
from ...models import OrderBookSnapshot
from ...strategy_timeline import (
    read_recent_strategy_timeline_entries,
    summarize_strategy_timeline_entries,
)
from ...trade_log import (
    read_recent_trade_entries,
    summarize_trade_entries,
)


from .shared import (
    _all_account_exchanges,
    _exchange_balance_symbols,
    _risk_account_enabled,
    _risk_strategy_enabled,
)

def _top_level(
    book: OrderBookSnapshot | None, side: str
) -> tuple[float | None, float | None]:
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
                "bid_common": bid * rate
                if bid is not None and rate is not None
                else None,
                "ask_common": ask * rate
                if ask is not None and rate is not None
                else None,
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
    compact_entries = [_compact_trade_log_entry(entry) for entry in recent_entries]
    trade_log_payload = asdict(cfg.trade_log)
    trade_log_payload["recent_entries"] = compact_entries
    trade_log_payload["recent_events"] = compact_entries
    trade_log_payload["summary"] = summarize_trade_entries(recent_entries)
    trade_log_payload["error"] = trade_log_error
    try:
        timeline_entries = read_recent_strategy_timeline_entries(cfg.strategy_timeline)
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
    timeline_payload["summary"] = summarize_strategy_timeline_entries(timeline_entries)
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

    live_base = (
        cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading
    )
    exchange_labels = {
        exchange.key: exchange.display_label or exchange.label or exchange.key
        for exchange in _all_account_exchanges(cfg)
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
            "exchange_label": exchange_labels.get(exchange, exchange),
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
            strategy_id="cross_exchange_rebalance",
            label="Cross-Exchange Rebalance",
            configured=cfg.cross_exchange_rebalance.enabled,
            exchange=cfg.cross_exchange_rebalance.buy_exchange,
            symbol=(
                f"{cfg.cross_exchange_rebalance.buy_symbol} -> "
                f"{cfg.cross_exchange_rebalance.sell_symbol}"
            ).strip(" ->"),
            strategy_allowed=(
                cfg.risk.strategy_enabled.get(
                    "cross_exchange_rebalance",
                    False,
                )
                and _risk_account_enabled(
                    cfg,
                    cfg.cross_exchange_rebalance.sell_exchange,
                )
            ),
            live_ready=cfg.cross_exchange_rebalance.live_enabled,
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

def _account_payload_by_exchange(
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
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
        safety = (
            market_maker.get("safety")
            if isinstance(market_maker.get("safety"), dict)
            else {}
        )
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
            or (
                account_balances.get("status")
                if account_balances.get("accounts")
                else "starting"
            )
            or "starting"
        )
        order_status = str(
            orders.get("status")
            or (
                order_activity.get("status")
                if order_activity.get("accounts")
                else "starting"
            )
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
            reasons.extend(
                _account_payload_messages(balance) or ["balance check failed"]
            )
        elif used and balance_status == "warning":
            reasons.extend(
                _account_payload_messages(balance) or ["balance check warning"]
            )
        if used and order_status == "error":
            reasons.extend(
                _account_payload_messages(orders) or ["order activity failed"]
            )
        elif used and order_status == "warning":
            reasons.extend(
                _account_payload_messages(orders) or ["order activity warning"]
            )
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
        if strategy.get("configured") and not strategy.get("strategy_allowed", True):
            status = "disabled"
        elif (
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
        1
        for row in markets or []
        if isinstance(row, dict) and row.get("status") != "ok"
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
    elif not (
        cfg.risk.enabled and cfg.risk.trading_enabled and cfg.risk.allow_live_trading
    ):
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
                priority="medium"
                if order_activity.get("status") == "warning"
                else "high",
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
    if (
        protection_blocked_count
        or protection_warning_count
        or protection_manual_review_count
    ):
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
            cfg.risk.enabled
            and cfg.risk.trading_enabled
            and cfg.risk.allow_live_trading
        ),
        "accounts": account_rows,
        "strategies": strategy_rows,
        "balance_checks": {
            "status": account_balances.get("status") or "starting",
            "checked_account_count": account_balances.get("checked_account_count", 0),
            "total_account_count": account_balances.get(
                "total_account_count", len(account_rows)
            ),
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
            "paused_strategies": sum(
                1 for row in strategy_rows if row["status"] == "paused"
            ),
            "execution_protection_blocked_count": protection_blocked_count,
            "execution_protection_warning_count": protection_warning_count,
            "execution_protection_manual_review_count": protection_manual_review_count,
            "derivative_blocked_account_count": derivative_blocked_count,
            "derivative_warning_account_count": derivative_warning_count,
            "derivative_position_count": derivative_readiness["position_count"],
            "blocked_count": blocked_accounts
            + blocked_strategies
            + protection_blocked_count,
            "warning_count": warning_count,
            "warning_messages": list(warnings or [])[:6],
            "action_count": len(unique_actions),
        },
        "next_actions": unique_actions[:12],
        "checked_at": time.time(),
    }
