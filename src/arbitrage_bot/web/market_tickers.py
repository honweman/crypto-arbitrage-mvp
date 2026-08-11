from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import ExchangeConfig
from ..exchanges import ExchangeManager


MAX_WATCHLIST_ITEMS = 20
MARKET_TICKER_CACHE_SECONDS = 8.0
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9._-]{1,30}/[A-Z0-9._-]{1,30}(?::[A-Z0-9._-]{1,30})?$")


PUBLIC_TICKER_ACCOUNTS: tuple[ExchangeConfig, ...] = (
    ExchangeConfig(
        id="binance",
        label="ticker:binance:spot",
        display_label="Binance Spot",
        market_type="spot",
    ),
    ExchangeConfig(
        id="binanceusdm",
        label="ticker:binance:swap",
        display_label="Binance Perpetual",
        market_type="swap",
    ),
    ExchangeConfig(
        id="bybit",
        label="ticker:bybit:spot",
        display_label="Bybit Spot",
        market_type="spot",
    ),
    ExchangeConfig(
        id="bybit",
        label="ticker:bybit:swap",
        display_label="Bybit Perpetual",
        market_type="swap",
    ),
)


def ticker_accounts_payload() -> list[dict[str, str]]:
    return [
        {
            "key": account.key,
            "label": account.display_label or account.key,
            "exchange": account.id,
            "market_type": account.market_type,
        }
        for account in PUBLIC_TICKER_ACCOUNTS
    ]


def default_watchlist() -> list[dict[str, str]]:
    return [
        {"exchange": "ticker:binance:spot", "symbol": f"{asset}/USDT"}
        for asset in ("BTC", "ETH", "SOL", "BNB")
    ] + [
        {
            "exchange": "ticker:binance:swap",
            "symbol": f"{asset}/USDT:USDT",
        }
        for asset in ("BTC", "ETH")
    ]


def normalize_watchlist(
    items: Iterable[dict[str, Any]],
    *,
    allowed_accounts: dict[str, ExchangeConfig] | None = None,
) -> list[dict[str, str]]:
    accounts = allowed_accounts or {row.key: row for row in PUBLIC_TICKER_ACCOUNTS}
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("watchlist items must be objects")
        exchange = str(raw.get("exchange") or "").strip()
        account = accounts.get(exchange)
        if account is None:
            raise ValueError(f"unsupported ticker account: {exchange}")
        symbol = str(raw.get("symbol") or "").strip().upper().replace(" ", "")
        if account.market_type == "swap" and ":" not in symbol and "/" in symbol:
            quote = symbol.split("/", 1)[1]
            symbol = f"{symbol}:{quote}"
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(
                "ticker symbol must use BASE/QUOTE or BASE/QUOTE:SETTLE format"
            )
        key = (exchange, symbol)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"exchange": exchange, "symbol": symbol})
        if len(rows) > MAX_WATCHLIST_ITEMS:
            raise ValueError(f"watchlist supports at most {MAX_WATCHLIST_ITEMS} items")
    if not rows:
        raise ValueError("watchlist must contain at least one item")
    return rows


class MarketWatchlistStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "users": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "users": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
            return {"version": 1, "users": {}}
        return raw

    def get(self, owner_email: str) -> list[dict[str, str]]:
        owner = str(owner_email or "").strip().lower()
        with self._lock:
            raw = self._load().get("users", {}).get(owner)
        if not isinstance(raw, list):
            return default_watchlist()
        try:
            return normalize_watchlist(raw)
        except ValueError:
            return default_watchlist()

    def set(
        self,
        owner_email: str,
        items: Iterable[dict[str, Any]],
    ) -> list[dict[str, str]]:
        owner = str(owner_email or "").strip().lower()
        if not owner:
            raise ValueError("watchlist owner is required")
        normalized = normalize_watchlist(items)
        with self._lock:
            payload = self._load()
            payload.setdefault("users", {})[owner] = normalized
            payload["updated_at"] = time.time()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        return normalized


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ticker_row(
    account: ExchangeConfig,
    symbol: str,
    ticker: dict[str, Any] | None,
) -> dict[str, Any]:
    raw = ticker or {}
    price = next(
        (
            value
            for field in ("last", "close", "bid", "ask")
            if (value := _number(raw.get(field))) is not None and value > 0
        ),
        None,
    )
    percentage = _number(raw.get("percentage"))
    open_price = _number(raw.get("open"))
    if percentage is None and price is not None and open_price is not None and open_price > 0:
        percentage = (price - open_price) / open_price * 100.0
    return {
        "exchange": account.key,
        "exchange_label": account.display_label or account.key,
        "exchange_id": account.id,
        "market_type": account.market_type,
        "symbol": symbol,
        "price": price,
        "change_24h_pct": percentage,
        "bid": _number(raw.get("bid")),
        "ask": _number(raw.get("ask")),
        "timestamp_ms": _number(raw.get("timestamp")),
        "status": "ok" if price is not None else "unavailable",
    }


class MarketTickerService:
    def __init__(
        self,
        store: MarketWatchlistStore,
        *,
        manager: ExchangeManager | None = None,
        cache_seconds: float = MARKET_TICKER_CACHE_SECONDS,
    ) -> None:
        self.store = store
        self.manager = manager or ExchangeManager(order_journal_path="")
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def snapshot(
        self,
        owner_email: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        owner = str(owner_email or "").strip().lower()
        items = self.store.get(owner)
        signature = json.dumps(items, sort_keys=True, separators=(",", ":"))
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(owner)
            if (
                not force
                and cached is not None
                and cached[1] == signature
                and now - cached[0] <= self.cache_seconds
            ):
                return json.loads(json.dumps(cached[2]))

        accounts = {row.key: row for row in PUBLIC_TICKER_ACCOUNTS}
        grouped: dict[str, list[str]] = {}
        for item in items:
            grouped.setdefault(item["exchange"], []).append(item["symbol"])
        results = await asyncio.gather(
            *(
                self.manager.fetch_tickers(accounts[key], symbols)
                for key, symbols in grouped.items()
            ),
            return_exceptions=True,
        )
        tickers_by_exchange: dict[str, dict[str, dict[str, Any]]] = {}
        for key, result in zip(grouped, results):
            tickers_by_exchange[key] = result if isinstance(result, dict) else {}

        rows = []
        for item in items:
            account = accounts[item["exchange"]]
            tickers = tickers_by_exchange.get(account.key, {})
            ticker = tickers.get(item["symbol"])
            if not isinstance(ticker, dict):
                ticker = next(
                    (
                        row
                        for row in tickers.values()
                        if isinstance(row, dict)
                        and str(row.get("symbol") or "") == item["symbol"]
                    ),
                    None,
                )
            rows.append(_ticker_row(account, item["symbol"], ticker))
        payload = {
            "status": "ok" if any(row["status"] == "ok" for row in rows) else "error",
            "items": rows,
            "watchlist": items,
            "accounts": ticker_accounts_payload(),
            "updated_at": time.time(),
            "refresh_seconds": self.cache_seconds,
        }
        async with self._lock:
            self._cache[owner] = (now, signature, payload)
        return json.loads(json.dumps(payload))

    def save(self, owner_email: str, items: Iterable[dict[str, Any]]) -> None:
        self.store.set(owner_email, items)
        self._cache.pop(str(owner_email or "").strip().lower(), None)

    async def close(self) -> None:
        await self.manager.close()
