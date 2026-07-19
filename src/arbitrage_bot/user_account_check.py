from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .config import ExchangeConfig
from .exchanges import ExchangeManager
from .user_workspace import UserExchangeAccount, UserProject


DEFAULT_CHECK_TIMEOUT_SECONDS = 20.0
DEFAULT_DISCOVERY_CACHE_SECONDS = 300.0


def _base_currency(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].strip().upper()


def _quote_currency(symbol: str) -> str:
    if "/" not in str(symbol or ""):
        return ""
    return str(symbol).split("/", 1)[1].split(":", 1)[0].strip().upper()


def workspace_exchange_config(
    *,
    exchange: str,
    market_type: str,
    api_variant: str,
    runtime_key: str,
) -> ExchangeConfig:
    exchange_id = str(exchange or "").strip().lower()
    market = str(market_type or "spot").strip().lower()
    variant = str(api_variant or "default").strip().lower()
    options: dict[str, Any] = {}

    if exchange_id == "bithumb":
        options["private_api"] = "v2.0"
    elif exchange_id == "upbit" and variant == "indonesia":
        options["hostname"] = "id-api.upbit.com"
    elif exchange_id == "hyperliquid" and variant == "testnet":
        options["hostname"] = "hyperliquid-testnet.xyz"

    ccxt_id = (
        "binanceusdm" if exchange_id == "binance" and market == "swap" else exchange_id
    )
    return ExchangeConfig(
        id=ccxt_id,
        label=f"workspace:{runtime_key}",
        market_type=market,
        options=options,
    )


def _market_matches_type(market: dict[str, Any], market_type: str) -> bool:
    if market_type == "spot":
        return market.get("spot") is True or market.get("type") == "spot"
    if market_type == "swap":
        return market.get("swap") is True or market.get("type") == "swap"
    if market_type == "future":
        return market.get("future") is True or market.get("type") == "future"
    return False


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _market_row(market: dict[str, Any]) -> dict[str, Any]:
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    amount = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
    cost = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}
    precision = (
        market.get("precision") if isinstance(market.get("precision"), dict) else {}
    )
    symbol = str(market.get("symbol") or "")
    return {
        "symbol": symbol,
        "base": str(market.get("base") or _base_currency(symbol)).upper(),
        "quote": str(market.get("quote") or _quote_currency(symbol)).upper(),
        "settle": str(market.get("settle") or "").upper(),
        "active": market.get("active"),
        "type": market.get("type"),
        "amount_min": _number(amount.get("min")),
        "cost_min": _number(cost.get("min")),
        "amount_precision": precision.get("amount"),
        "price_precision": precision.get("price"),
    }


def _safe_error(exc: Exception, credentials: dict[str, str] | None = None) -> str:
    message = f"{exc.__class__.__name__}: {exc}"
    for secret in sorted((credentials or {}).values(), key=len, reverse=True):
        if secret and len(secret) >= 4:
            message = message.replace(secret, "[redacted]")
    return message[:240]


async def discover_workspace_markets(
    *,
    exchange: str,
    market_type: str,
    api_variant: str,
    asset: str,
    timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS,
    manager_factory: Callable[..., ExchangeManager] = ExchangeManager,
) -> list[dict[str, Any]]:
    target_asset = str(asset or "").strip().upper()
    cfg = workspace_exchange_config(
        exchange=exchange,
        market_type=market_type,
        api_variant=api_variant,
        runtime_key=f"discovery:{exchange}:{market_type}:{api_variant}",
    )
    manager = manager_factory()
    try:
        client = manager.client(cfg)
        markets = await asyncio.wait_for(
            client.load_markets(),
            timeout=max(1.0, timeout_seconds),
        )
        if not isinstance(markets, dict):
            return []
        rows = [
            _market_row(market)
            for market in markets.values()
            if isinstance(market, dict)
            and str(market.get("base") or "").upper() == target_asset
            and _market_matches_type(market, market_type)
            and market.get("active") is not False
            and market.get("symbol")
        ]
        return sorted(rows, key=lambda row: (row["quote"], row["symbol"]))[:250]
    finally:
        await manager.close()


def _balance_value(balance: dict[str, Any], currency: str, field: str) -> float | None:
    row = balance.get(currency)
    if isinstance(row, dict) and row.get(field) is not None:
        return _number(row.get(field))
    by_field = balance.get(field)
    if isinstance(by_field, dict):
        return _number(by_field.get(currency))
    return None


def _balance_rows(
    balance: dict[str, Any], currencies: set[str]
) -> list[dict[str, Any]]:
    rows = []
    for currency in sorted(currencies):
        row = {
            "currency": currency,
            "free": _balance_value(balance, currency, "free"),
            "used": _balance_value(balance, currency, "used"),
            "total": _balance_value(balance, currency, "total"),
        }
        if any(
            value not in {None, 0.0}
            for value in row.values()
            if not isinstance(value, str)
        ):
            rows.append(row)
    return rows


def _order_book_summary(book: Any) -> dict[str, Any]:
    bids = list(getattr(book, "bids", []) or [])
    asks = list(getattr(book, "asks", []) or [])
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    mid = (
        (best_bid + best_ask) / 2
        if best_bid is not None and best_ask is not None
        else None
    )
    return {
        "available": bool(bids and asks),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid,
        "spread_bps": (
            (best_ask - best_bid) / mid * 10_000
            if mid and best_bid is not None and best_ask is not None
            else None
        ),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "timestamp_ms": getattr(book, "timestamp_ms", None),
    }


async def check_workspace_account(
    *,
    account: UserExchangeAccount,
    project: UserProject,
    credentials: dict[str, str],
    order_book_depth: int = 5,
    timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS,
    manager_factory: Callable[..., ExchangeManager] = ExchangeManager,
) -> dict[str, Any]:
    if not account.symbol:
        raise ValueError("select a trading pair before testing the account")
    if _base_currency(account.symbol) != project.asset:
        raise ValueError(
            f"account symbol base must match project asset {project.asset}"
        )
    cfg = workspace_exchange_config(
        exchange=account.exchange,
        market_type=account.market_type,
        api_variant=account.api_variant,
        runtime_key=account.id,
    )
    manager = manager_factory(credentials_by_key={cfg.key: credentials})
    started = time.perf_counter()
    try:
        market = await asyncio.wait_for(
            manager.fetch_market_info(cfg, symbol=account.symbol),
            timeout=max(1.0, timeout_seconds),
        )
        if market is None:
            raise ValueError(f"market is unavailable: {account.symbol}")
        if market.get("active") is False:
            raise ValueError(f"market is inactive: {account.symbol}")

        book = await asyncio.wait_for(
            manager.fetch_order_book(cfg, account.symbol, max(1, order_book_depth)),
            timeout=max(1.0, timeout_seconds),
        )
        book_summary = _order_book_summary(book)
        if not book_summary["available"]:
            raise ValueError(f"order book is unavailable: {account.symbol}")

        balance = await asyncio.wait_for(
            manager.fetch_balance(cfg),
            timeout=max(1.0, timeout_seconds),
        )
        open_orders = await asyncio.wait_for(
            manager.fetch_open_orders(cfg, symbol=account.symbol),
            timeout=max(1.0, timeout_seconds),
        )
        currencies = {
            _base_currency(account.symbol),
            _quote_currency(account.symbol),
        }
        return {
            "status": "healthy",
            "checked_at": time.time(),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "exchange": account.exchange,
            "market_type": account.market_type,
            "api_variant": account.api_variant,
            "symbol": account.symbol,
            "market": _market_row(market),
            "order_book": book_summary,
            "balances": _balance_rows(balance, currencies),
            "open_order_count": len(open_orders or []),
            "permissions": {
                "private_account_read": "verified",
                "open_order_read": "verified",
                "trade_endpoint": "supported_not_exercised",
                "trade_permission": (
                    "user_confirmed"
                    if account.trade_permission_confirmed
                    else "not_confirmed"
                ),
                "withdrawal_disabled": (
                    "user_confirmed"
                    if account.withdrawal_disabled_confirmed
                    else "not_confirmed"
                ),
                "safe_for_strategy_setup": bool(
                    account.trade_permission_confirmed
                    and account.withdrawal_disabled_confirmed
                ),
                "note": (
                    "The connection test never places an order, so exchange-side "
                    "trade permission is user-confirmed rather than exercised."
                ),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "checked_at": time.time(),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "exchange": account.exchange,
            "market_type": account.market_type,
            "api_variant": account.api_variant,
            "symbol": account.symbol,
            "permissions": {
                "private_account_read": "failed",
                "open_order_read": "failed_or_not_reached",
                "trade_endpoint": "not_checked",
                "trade_permission": (
                    "user_confirmed"
                    if account.trade_permission_confirmed
                    else "not_confirmed"
                ),
                "withdrawal_disabled": (
                    "user_confirmed"
                    if account.withdrawal_disabled_confirmed
                    else "not_confirmed"
                ),
                "safe_for_strategy_setup": False,
            },
            "error": _safe_error(exc, credentials),
        }
    finally:
        await manager.close()


class WorkspaceMarketDiscoveryService:
    def __init__(
        self, *, cache_seconds: float = DEFAULT_DISCOVERY_CACHE_SECONDS
    ) -> None:
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._cache: dict[
            tuple[str, str, str, str], tuple[float, list[dict[str, Any]]]
        ] = {}
        self._lock = asyncio.Lock()

    async def discover(
        self,
        *,
        exchange: str,
        market_type: str,
        api_variant: str,
        asset: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        key = (
            str(exchange).lower(),
            str(market_type).lower(),
            str(api_variant).lower(),
            str(asset).upper(),
        )
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self.cache_seconds:
                return [dict(row) for row in cached[1]], True
        try:
            rows = await discover_workspace_markets(
                exchange=key[0],
                market_type=key[1],
                api_variant=key[2],
                asset=key[3],
            )
        except Exception as exc:
            detail = f"{exc.__class__.__name__}: {exc}"[:200]
            raise RuntimeError(f"market discovery failed: {detail}") from exc
        async with self._lock:
            self._cache[key] = (time.monotonic(), [dict(row) for row in rows])
        return rows, False


class WorkspaceAccountCheckService:
    def __init__(self, *, cooldown_seconds: float = 5.0) -> None:
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._active: set[str] = set()
        self._last_finished: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check(
        self,
        *,
        account: UserExchangeAccount,
        project: UserProject,
        credentials: dict[str, str],
    ) -> dict[str, Any]:
        now = time.monotonic()
        async with self._lock:
            if account.id in self._active:
                raise RuntimeError("account connection test is already running")
            last_finished = self._last_finished.get(account.id)
            if (
                last_finished is not None
                and now - last_finished < self.cooldown_seconds
            ):
                remaining = self.cooldown_seconds - (now - last_finished)
                raise RuntimeError(
                    f"wait {remaining:.1f}s before testing this account again"
                )
            self._active.add(account.id)
        try:
            return await check_workspace_account(
                account=account,
                project=project,
                credentials=credentials,
            )
        finally:
            async with self._lock:
                self._active.discard(account.id)
                self._last_finished[account.id] = time.monotonic()
