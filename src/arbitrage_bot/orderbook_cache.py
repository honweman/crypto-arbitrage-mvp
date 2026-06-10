from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from .config import ExchangeConfig
from .models import OrderBookSnapshot


CacheKey = tuple[str, str]


class OrderBookCache:
    def __init__(
        self,
        manager: Any,
        *,
        max_age_seconds: float = 2.0,
        max_backoff_seconds: float = 5.0,
    ) -> None:
        self._manager = manager
        self._max_age_seconds = max_age_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._cache: dict[CacheKey, OrderBookSnapshot] = {}
        self._errors: dict[CacheKey, str] = {}
        self._supported: dict[CacheKey, bool] = {}
        self._depths: dict[CacheKey, int] = {}
        self._tasks: dict[CacheKey, asyncio.Task[None]] = {}

    async def ensure_watch(
        self,
        cfg: ExchangeConfig,
        symbol: str,
        depth: int,
    ) -> bool:
        key = (cfg.key, symbol)
        supported_checker = getattr(self._manager, "watch_order_book_supported", None)
        if supported_checker is None or not supported_checker(cfg):
            self._supported[key] = False
            self._errors[key] = "websocket order book is not supported"
            return False

        self._supported[key] = True
        existing = self._tasks.get(key)
        if existing is not None and not existing.done() and self._depths.get(key) == depth:
            return True

        if existing is not None:
            existing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await existing

        self._depths[key] = depth
        self._tasks[key] = asyncio.create_task(
            self._watch_loop(cfg, symbol, depth),
            name=f"orderbook-watch:{cfg.key}:{symbol}",
        )
        return True

    def update(self, snapshot: OrderBookSnapshot) -> None:
        self._cache[(snapshot.exchange, snapshot.symbol)] = snapshot

    def get(
        self,
        exchange: str,
        symbol: str,
        *,
        max_age_seconds: float | None = None,
    ) -> OrderBookSnapshot | None:
        key = (exchange, symbol)
        snapshot = self._cache.get(key)
        if snapshot is None:
            return None
        allowed_age = self._max_age_seconds if max_age_seconds is None else max_age_seconds
        if allowed_age > 0 and time.time() - snapshot.received_at > allowed_age:
            return None
        return snapshot

    def status(
        self,
        exchange: str,
        symbol: str,
        *,
        max_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        key = (exchange, symbol)
        snapshot = self._cache.get(key)
        task = self._tasks.get(key)
        age_seconds = (
            max(0.0, time.time() - snapshot.received_at)
            if snapshot is not None
            else None
        )
        allowed_age = self._max_age_seconds if max_age_seconds is None else max_age_seconds
        return {
            "exchange": exchange,
            "symbol": symbol,
            "source": snapshot.source if snapshot is not None else None,
            "timestamp_ms": snapshot.timestamp_ms if snapshot is not None else None,
            "received_at": snapshot.received_at if snapshot is not None else None,
            "age_seconds": age_seconds,
            "fresh": bool(
                snapshot is not None
                and (allowed_age <= 0 or age_seconds is not None and age_seconds <= allowed_age)
            ),
            "bid_levels": len(snapshot.bids) if snapshot is not None else 0,
            "ask_levels": len(snapshot.asks) if snapshot is not None else 0,
            "websocket_supported": self._supported.get(key),
            "watch_running": task is not None and not task.done(),
            "depth": self._depths.get(key),
            "error": self._errors.get(key),
        }

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _watch_loop(
        self,
        cfg: ExchangeConfig,
        symbol: str,
        depth: int,
    ) -> None:
        key = (cfg.key, symbol)
        backoff_seconds = 0.25
        while True:
            try:
                snapshot = await self._manager.watch_order_book(cfg, symbol, depth)
                if snapshot is None or not snapshot.bids or not snapshot.asks:
                    self._errors[key] = "empty websocket order book"
                else:
                    self._cache[key] = snapshot
                    self._errors.pop(key, None)
                    backoff_seconds = 0.25
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._errors[key] = f"{exc.__class__.__name__}: {exc}"
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(
                    self._max_backoff_seconds,
                    backoff_seconds * 2,
                )
