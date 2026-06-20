from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from time import time
from typing import Any, Iterable

from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .models import OrderBookSnapshot


AUTH_ENV_FIELDS = ("api_key_env", "secret_env", "password_env")
DEFAULT_BALANCE_CURRENCIES = {"USD", "USDC", "USDT", "KRW"}


def _error_text(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def _status(errors: list[str], warnings: list[str]) -> str:
    if errors:
        return "error"
    if warnings:
        return "warning"
    return "ok"


def _all_exchanges(cfg: BotConfig) -> list[ExchangeConfig]:
    return [*cfg.spot_exchanges, *cfg.derivative_exchanges]


def _symbols_by_exchange(cfg: BotConfig) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for market in cfg.spot_markets:
        symbols.setdefault(market.exchange, set()).add(market.symbol)

    for pair in cfg.cash_and_carry_pairs:
        for exchange in cfg.spot_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.spot_symbol)
        for exchange in cfg.derivative_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.derivative_symbol)
    for combo in cfg.option_combos:
        symbols.setdefault(combo.spot_exchange, set()).add(combo.spot_symbol)
        symbols.setdefault(combo.option_exchange, set()).update(
            {combo.call_symbol, combo.put_symbol}
        )
    for route in cfg.triangular_arbitrage.routes:
        symbols.setdefault(route.exchange, set()).update(route.symbols)

    if cfg.market_maker.exchange and cfg.market_maker.symbol:
        symbols.setdefault(cfg.market_maker.exchange, set()).add(
            cfg.market_maker.symbol
        )
    if cfg.slow_execution.exchange and cfg.slow_execution.symbol:
        symbols.setdefault(cfg.slow_execution.exchange, set()).add(
            cfg.slow_execution.symbol
        )
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

    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _auth_env_status(exchange: ExchangeConfig) -> dict[str, Any]:
    fields = []
    missing = []
    set_names = []
    for field_name in AUTH_ENV_FIELDS:
        env_name = getattr(exchange, field_name)
        if not env_name:
            continue
        is_set = bool(os.environ.get(env_name))
        fields.append(
            {
                "field": field_name,
                "env": env_name,
                "set": is_set,
            }
        )
        if is_set:
            set_names.append(env_name)
        else:
            missing.append(env_name)

    return {
        "configured": bool(fields),
        "fields": fields,
        "set_env": set_names,
        "missing_env": missing,
        "private_checks_enabled": bool(fields) and not missing,
    }


def _split_symbol(symbol: str) -> tuple[str | None, str | None]:
    if "/" not in symbol:
        return None, None
    base, quote = symbol.split("/", 1)
    return base.upper(), quote.split(":", 1)[0].upper()


def _balance_currencies(symbols: Iterable[str]) -> set[str]:
    currencies = set(DEFAULT_BALANCE_CURRENCIES)
    for symbol in symbols:
        base, quote = _split_symbol(symbol)
        if base:
            currencies.add(base)
        if quote:
            currencies.add(quote)
    return currencies


def _symbol_currencies(symbols: Iterable[str]) -> set[str]:
    currencies = set()
    for symbol in symbols:
        base, quote = _split_symbol(symbol)
        if base:
            currencies.add(base)
        if quote:
            currencies.add(quote)
    return currencies


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _balance_value(balance: dict[str, Any], currency: str, field: str) -> float | None:
    nested = balance.get(currency)
    if isinstance(nested, dict):
        value = _number_or_none(nested.get(field))
        if value is not None:
            return value

    by_field = balance.get(field)
    if isinstance(by_field, dict):
        return _number_or_none(by_field.get(currency))
    return None


def _summarize_balance(
    balance: dict[str, Any],
    currencies: Iterable[str],
    *,
    include_zero: bool,
) -> list[dict[str, Any]]:
    rows = []
    for currency in sorted(currencies):
        row = {
            "currency": currency,
            "free": _balance_value(balance, currency, "free"),
            "used": _balance_value(balance, currency, "used"),
            "total": _balance_value(balance, currency, "total"),
        }
        has_value = any(
            row[field] not in {None, 0.0}
            for field in ("free", "used", "total")
        )
        if include_zero or has_value:
            rows.append(row)
    return rows


def _market_summary(market: dict[str, Any] | None) -> dict[str, Any]:
    if market is None:
        return {"found": False}
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    precision = (
        market.get("precision") if isinstance(market.get("precision"), dict) else {}
    )
    amount_limits = (
        limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
    )
    price_limits = limits.get("price") if isinstance(limits.get("price"), dict) else {}
    cost_limits = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}

    return {
        "found": True,
        "id": market.get("id"),
        "symbol": market.get("symbol"),
        "active": market.get("active"),
        "type": market.get("type"),
        "spot": market.get("spot"),
        "precision": {
            "amount": precision.get("amount"),
            "price": precision.get("price"),
        },
        "limits": {
            "amount_min": amount_limits.get("min"),
            "amount_max": amount_limits.get("max"),
            "price_min": price_limits.get("min"),
            "price_max": price_limits.get("max"),
            "cost_min": cost_limits.get("min"),
            "cost_max": cost_limits.get("max"),
        },
    }


def _book_summary(book: OrderBookSnapshot | None) -> dict[str, Any]:
    if book is None:
        return {"available": False}
    best_bid = book.bids[0].price if book.bids else None
    best_ask = book.asks[0].price if book.asks else None
    mid_price = (
        (best_bid + best_ask) / 2
        if best_bid is not None and best_ask is not None
        else None
    )
    spread_bps = (
        (best_ask - best_bid) / mid_price * 10_000
        if best_bid is not None and best_ask is not None and mid_price
        else None
    )
    return {
        "available": True,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_bps": spread_bps,
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
        "timestamp_ms": book.timestamp_ms,
    }


def _order_summary(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "client_order_id": order.get("clientOrderId") or order.get("clientOrderID"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "type": order.get("type"),
        "price": order.get("price"),
        "amount": order.get("amount"),
        "filled": order.get("filled"),
        "remaining": order.get("remaining"),
        "status": order.get("status"),
        "timestamp": order.get("timestamp"),
    }


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _network_status_summary(raw_networks: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_networks, dict):
        return []
    rows = []
    for network, raw in sorted(raw_networks.items()):
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "network": str(network),
                "active": _bool_or_none(raw.get("active")),
                "deposit": _bool_or_none(raw.get("deposit")),
                "withdraw": _bool_or_none(raw.get("withdraw")),
                "fee": _number_or_none(raw.get("fee")),
            }
        )
    return rows


def _transfer_status_summary(
    payload: dict[str, Any],
    currencies: Iterable[str],
) -> dict[str, Any]:
    if payload.get("unsupported"):
        return {
            "checked": False,
            "unsupported": True,
            "skipped_reason": payload.get("skipped_reason"),
            "currencies": [],
        }
    raw_currencies = payload.get("currencies")
    if not isinstance(raw_currencies, dict):
        raw_currencies = {}
    rows = []
    for currency in sorted({item.upper() for item in currencies if item}):
        raw = raw_currencies.get(currency)
        if not isinstance(raw, dict):
            rows.append(
                {
                    "currency": currency,
                    "found": False,
                    "active": None,
                    "deposit": None,
                    "withdraw": None,
                    "fee": None,
                    "networks": [],
                }
            )
            continue
        rows.append(
            {
                "currency": currency,
                "found": True,
                "active": _bool_or_none(raw.get("active")),
                "deposit": _bool_or_none(raw.get("deposit")),
                "withdraw": _bool_or_none(raw.get("withdraw")),
                "fee": _number_or_none(raw.get("fee")),
                "networks": _network_status_summary(raw.get("networks")),
            }
        )
    return {
        "checked": True,
        "unsupported": False,
        "currencies": rows,
    }


def _transfer_status_warnings(exchange: ExchangeConfig, summary: dict[str, Any]) -> list[str]:
    if summary.get("unsupported"):
        return [f"{exchange.key} transfer status API is unsupported"]
    warnings = []
    for row in summary.get("currencies", []):
        currency = row.get("currency")
        if row.get("found") is False:
            warnings.append(f"{exchange.key} {currency} transfer status is unavailable")
            continue
        if row.get("active") is False:
            warnings.append(f"{exchange.key} {currency} currency is inactive")
        if row.get("deposit") is False:
            warnings.append(f"{exchange.key} {currency} deposit is disabled")
        if row.get("withdraw") is False:
            warnings.append(f"{exchange.key} {currency} withdrawal is disabled")
    return warnings


def _risk_summary(cfg: BotConfig, exchange: ExchangeConfig) -> dict[str, Any]:
    key = exchange.key
    account_enabled = cfg.risk.account_enabled.get(key, True)
    allowed_by_list = not cfg.risk.allowed_exchanges or key in cfg.risk.allowed_exchanges
    blocked_by_list = key in cfg.risk.blocked_exchanges
    return {
        "risk_enabled": cfg.risk.enabled,
        "trading_enabled": cfg.risk.trading_enabled,
        "allow_live_trading": cfg.risk.allow_live_trading,
        "account_enabled": account_enabled,
        "allowed_by_exchange_list": allowed_by_list,
        "blocked_by_exchange_list": blocked_by_list,
        "max_order_quote": cfg.risk.max_order_quote,
        "max_cycle_quote": cfg.risk.max_cycle_quote,
        "max_open_orders": cfg.risk.max_open_orders,
        "max_daily_loss_quote": cfg.risk.max_daily_loss_quote,
    }


async def check_exchange_account(
    cfg: BotConfig,
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
    *,
    include_zero_balances: bool,
    order_book_depth: int,
    open_order_preview_limit: int,
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    auth = _auth_env_status(exchange)
    risk = _risk_summary(cfg, exchange)

    if not auth["configured"]:
        warnings.append("API env vars are not configured for this exchange")
    elif auth["missing_env"]:
        warnings.append("one or more configured API env vars are not set")
    if not risk["trading_enabled"]:
        warnings.append("risk.trading_enabled is false")
    if not risk["allow_live_trading"]:
        warnings.append("risk.allow_live_trading is false")
    if not risk["account_enabled"]:
        warnings.append(f"risk.account_enabled.{exchange.key} is false")
    if not risk["allowed_by_exchange_list"]:
        warnings.append(f"{exchange.key} is not in risk.allowed_exchanges")
    if risk["blocked_by_exchange_list"]:
        warnings.append(f"{exchange.key} is in risk.blocked_exchanges")
    if not symbols:
        warnings.append("no configured symbols for this exchange")

    market_checks = []
    for symbol in symbols:
        market_entry: dict[str, Any] = {"symbol": symbol}
        try:
            market_entry["market"] = _market_summary(
                await manager.fetch_market_info(exchange, symbol=symbol)
            )
            if not market_entry["market"]["found"]:
                errors.append(f"{exchange.key} {symbol} market is not found")
            elif market_entry["market"].get("active") is False:
                warnings.append(f"{exchange.key} {symbol} market is inactive")
        except Exception as exc:  # noqa: BLE001
            message = _error_text(exc)
            market_entry["market"] = {"found": False, "error": message}
            errors.append(f"{exchange.key} {symbol} market check failed: {message}")

        try:
            market_entry["order_book"] = _book_summary(
                await manager.fetch_order_book(exchange, symbol, order_book_depth)
            )
            if not market_entry["order_book"]["available"]:
                errors.append(f"{exchange.key} {symbol} order book is unavailable")
        except Exception as exc:  # noqa: BLE001
            message = _error_text(exc)
            market_entry["order_book"] = {"available": False, "error": message}
            errors.append(f"{exchange.key} {symbol} order book failed: {message}")

        market_checks.append(market_entry)

    balance_entry: dict[str, Any] = {
        "checked": False,
        "skipped_reason": None,
        "currencies": [],
    }
    transfer_status_entry: dict[str, Any] = {
        "checked": False,
        "unsupported": False,
        "skipped_reason": None,
        "currencies": [],
    }
    open_order_entries: list[dict[str, Any]] = []
    balance_currencies = _balance_currencies(symbols)
    transfer_currencies = _symbol_currencies(symbols)
    if transfer_currencies:
        try:
            transfer_status_entry = _transfer_status_summary(
                await manager.fetch_currency_status(
                    exchange,
                    currencies=transfer_currencies,
                ),
                transfer_currencies,
            )
            warnings.extend(
                _transfer_status_warnings(exchange, transfer_status_entry)
            )
        except Exception as exc:  # noqa: BLE001
            message = _error_text(exc)
            transfer_status_entry = {
                "checked": True,
                "unsupported": False,
                "error": message,
                "currencies": [],
            }
            warnings.append(f"{exchange.key} transfer status check failed: {message}")

    if auth["private_checks_enabled"]:
        try:
            balance = await manager.fetch_balance(exchange)
            balance_entry = {
                "checked": True,
                "currencies": _summarize_balance(
                    balance,
                    balance_currencies,
                    include_zero=include_zero_balances,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            message = _error_text(exc)
            balance_entry = {
                "checked": True,
                "error": message,
                "currencies": [],
            }
            errors.append(f"{exchange.key} balance check failed: {message}")

        for symbol in symbols:
            try:
                open_orders = await manager.fetch_open_orders(exchange, symbol=symbol)
                open_order_entries.append(
                    {
                        "symbol": symbol,
                        "checked": True,
                        "count": len(open_orders),
                        "preview": [
                            _order_summary(order)
                            for order in open_orders[:open_order_preview_limit]
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                message = _error_text(exc)
                open_order_entries.append(
                    {
                        "symbol": symbol,
                        "checked": True,
                        "error": message,
                        "count": None,
                        "preview": [],
                    }
                )
                errors.append(f"{exchange.key} {symbol} open orders failed: {message}")
    else:
        balance_entry["skipped_reason"] = (
            "api env vars missing" if auth["configured"] else "api env vars not configured"
        )
        for symbol in symbols:
            open_order_entries.append(
                {
                    "symbol": symbol,
                    "checked": False,
                    "skipped_reason": balance_entry["skipped_reason"],
                    "count": None,
                    "preview": [],
                }
            )

    return {
        "exchange": exchange.key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "fee_bps": exchange.fee_bps,
        "symbols": symbols,
        "status": _status(errors, warnings),
        "warnings": warnings,
        "errors": errors,
        "auth": auth,
        "risk": risk,
        "markets": market_checks,
        "balance": balance_entry,
        "transfer_status": transfer_status_entry,
        "open_orders": open_order_entries,
    }


async def run_account_checks(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    exchange_keys: list[str] | None = None,
    symbols: list[str] | None = None,
    include_zero_balances: bool = False,
    order_book_depth: int = 5,
    open_order_preview_limit: int = 5,
) -> dict[str, Any]:
    configured_symbols = _symbols_by_exchange(cfg)
    selected_keys = set(exchange_keys or [])
    exchanges = [
        exchange
        for exchange in _all_exchanges(cfg)
        if not selected_keys or exchange.key in selected_keys
    ]

    missing_exchanges = sorted(selected_keys - {exchange.key for exchange in exchanges})
    accounts = []
    for exchange in exchanges:
        account_symbols = sorted(set(symbols or configured_symbols.get(exchange.key, [])))
        accounts.append(
            await check_exchange_account(
                cfg,
                manager,
                exchange,
                account_symbols,
                include_zero_balances=include_zero_balances,
                order_book_depth=order_book_depth,
                open_order_preview_limit=open_order_preview_limit,
            )
        )

    errors = [f"exchange is not configured: {key}" for key in missing_exchanges]
    warnings: list[str] = []
    if not accounts:
        warnings.append("no exchanges selected")

    account_errors = [
        error
        for account in accounts
        for error in account.get("errors", [])
    ]
    account_warnings = [
        warning
        for account in accounts
        for warning in account.get("warnings", [])
    ]

    return {
        "type": "account_check",
        "checked_at": time(),
        "status": _status([*errors, *account_errors], [*warnings, *account_warnings]),
        "errors": errors,
        "warnings": warnings,
        "accounts": accounts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only exchange account preflight check"
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--exchange",
        action="append",
        dest="exchange_keys",
        help="Exchange label/key to check. Repeat to check multiple accounts.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Symbol to check. Repeat for multiple symbols. Defaults to configured symbols.",
    )
    parser.add_argument(
        "--include-zero-balances",
        action="store_true",
        help="Include zero/empty target-currency balances in output.",
    )
    parser.add_argument(
        "--order-book-depth",
        type=int,
        default=5,
        help="Public order book depth to fetch for each checked symbol.",
    )
    parser.add_argument(
        "--open-order-preview-limit",
        type=int,
        default=5,
        help="Maximum open order rows to include per symbol.",
    )
    return parser


async def _run_from_args(args: argparse.Namespace) -> dict[str, Any]:
    manager = ExchangeManager()
    try:
        cfg = load_config(args.config)
        return await run_account_checks(
            cfg,
            manager,
            exchange_keys=args.exchange_keys,
            symbols=args.symbols,
            include_zero_balances=args.include_zero_balances,
            order_book_depth=max(1, args.order_book_depth),
            open_order_preview_limit=max(0, args.open_order_preview_limit),
        )
    finally:
        await manager.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        payload = asyncio.run(_run_from_args(args))
    except KeyboardInterrupt:
        return
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": _error_text(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
