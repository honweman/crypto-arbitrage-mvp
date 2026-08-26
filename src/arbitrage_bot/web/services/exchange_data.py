from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any


from ..constants import (
    ORDER_ACTIVITY_LIMIT,
)


from ...account_check import (
    _auth_env_status,
    _balance_currencies,
    _market_summary,
    _summarize_balance,
)
from ...config import (
    BotConfig,
    ExchangeConfig,
    RiskConfig,
    SlowExecutionConfig,
)
from ...derivatives import derivative_account_summary, normalize_derivative_position
from ...exchanges import ExchangeManager
from ...fill_store import load_fill_rows, persist_fill_pnl
from ...funding_basis import funding_basis_payload, funding_settings_from_strategy_center
from ...main import (
    _option_symbols_for_option_combos,
    _spot_symbols_for_option_combos,
)
from ...models import OrderBookSnapshot
from ...options_monitor import options_arbitrage_payload
from ...order_reconciliation import (
    build_order_reconciliation_payload,
)
from ...portfolio_metrics import (
    _trade_attribution,
    build_order_attribution_map,
    enrich_recent_trades_with_pnl,
)
from ...strategy_performance import build_strategy_performance_payload
from ...strategy_timeline import (
    write_strategy_timeline_from_payload,
)
from ...trade_log import (
    read_recent_trade_entries,
    write_trade_event,
)
from ...web_config import (
    _derivative_symbols_by_exchange,
)


from .shared import (
    _all_account_exchanges,
    _exchange_balance_symbols,
    _find_exchange_by_key,
)

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

def _add_reserve(
    reserves: dict[str, float], currency: str, amount: float | None
) -> None:
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
                    remaining * price
                    if remaining is not None and price is not None
                    else None,
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
    workspace_connection_id = str(exchange.credential_connection_id or "").strip()
    if workspace_connection_id:
        account["workspace_connection_id"] = workspace_connection_id

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
        "position_count": sum(
            len(account.get("positions", [])) for account in accounts
        ),
        "checked_account_count": sum(
            1 for account in accounts if account.get("checked")
        ),
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
    open_amount = remaining if remaining is not None else amount
    open_notional = (
        price * open_amount
        if price is not None and open_amount is not None
        else None
    )
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
        "open_notional": open_notional,
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
        performance_fills = load_fill_rows(cfg.pnl_store) or recent_trades
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
        performance_fills = recent_trades
        pnl_store_warnings = [f"fill P/L store unavailable: {exc}"]
    strategy_performance = build_strategy_performance_payload(
        recent_log_entries,
        performance_fills,
        currency=cfg.common_quote_currency,
        market_maker_runtime=market_maker_runtime,
        auto_buy_sell_tasks=auto_buy_sell_tasks,
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
        "strategy_performance": strategy_performance,
        "open_order_count": len(open_orders),
        "closed_order_count": len(closed_orders),
        "recent_trade_count": len(recent_trades),
        "checked_account_count": checked_accounts,
        "total_account_count": len(accounts),
        "last_finished": time.time(),
        "errors": errors,
        "warnings": warnings,
        "reliability": (
            manager.order_reliability_summary()
            if callable(getattr(manager, "order_reliability_summary", None))
            else {"enabled": False, "pending_count": 0, "total_count": 0}
        ),
    }
    base_payload["reconciliation"] = build_order_reconciliation_payload(
        base_payload,
        market_maker_runtime=market_maker_runtime,
        auto_buy_sell_tasks=auto_buy_sell_tasks,
    )
    return {
        **base_payload,
    }

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
    allowed_symbols = set(
        _exchange_balance_symbols(cfg, exec_cfg).get(exchange.key, [])
    )
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
    exchanges_by_key = {
        exchange.key: exchange for exchange in _all_account_exchanges(cfg)
    }
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
            errors.append(
                {"order": order, "error": f"symbol is not configured: {symbol}"}
            )
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
