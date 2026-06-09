from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Iterable
from typing import Any

from .config import ExchangeConfig
from .models import OrderBookSnapshot, Side
from .orderbook import normalize_levels


class ExchangeManager:
    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}

    def _build_client(self, cfg: ExchangeConfig) -> Any:
        ccxt = importlib.import_module("ccxt.async_support")
        exchange_cls = getattr(ccxt, cfg.id)

        options: dict[str, Any] = {
            "enableRateLimit": True,
            "options": dict(cfg.options),
        }
        if cfg.market_type != "spot":
            options["options"].setdefault("defaultType", cfg.market_type)

        if cfg.api_key_env and os.environ.get(cfg.api_key_env):
            options["apiKey"] = os.environ[cfg.api_key_env]
        if cfg.secret_env and os.environ.get(cfg.secret_env):
            options["secret"] = os.environ[cfg.secret_env]
        if cfg.password_env and os.environ.get(cfg.password_env):
            options["password"] = os.environ[cfg.password_env]

        return exchange_cls(options)

    def client(self, cfg: ExchangeConfig) -> Any:
        if cfg.key not in self._clients:
            self._clients[cfg.key] = self._build_client(cfg)
        return self._clients[cfg.key]

    async def close(self) -> None:
        await asyncio.gather(
            *[client.close() for client in self._clients.values()],
            return_exceptions=True,
        )

    async def fetch_order_book(
        self,
        cfg: ExchangeConfig,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot | None:
        client = self.client(cfg)
        try:
            raw = await client.fetch_order_book(symbol, limit=depth)
        except Exception as exc:  # noqa: BLE001
            print(
                f"failed to fetch order book: exchange={cfg.key} symbol={symbol} error={exc}",
                file=sys.stderr,
            )
            return None

        return OrderBookSnapshot(
            exchange=cfg.key,
            symbol=symbol,
            bids=normalize_levels(raw.get("bids", [])),
            asks=normalize_levels(raw.get("asks", [])),
            timestamp_ms=raw.get("timestamp"),
        )

    async def fetch_order_books(
        self,
        configs: Iterable[ExchangeConfig],
        symbols_by_exchange: dict[str, Iterable[str]],
        depth: int,
    ) -> dict[tuple[str, str], OrderBookSnapshot]:
        tasks = []
        for cfg in configs:
            for symbol in symbols_by_exchange.get(cfg.key, []):
                tasks.append(self.fetch_order_book(cfg, symbol, depth))

        snapshots = await asyncio.gather(*tasks)
        return {
            (snapshot.exchange, snapshot.symbol): snapshot
            for snapshot in snapshots
            if snapshot is not None
        }

    async def fetch_funding_rate(
        self,
        cfg: ExchangeConfig,
        symbol: str,
    ) -> tuple[str, str, float] | None:
        client = self.client(cfg)
        fetcher = getattr(client, "fetch_funding_rate", None)
        if fetcher is None:
            return None
        try:
            raw = await fetcher(symbol)
        except Exception as exc:  # noqa: BLE001
            print(
                f"failed to fetch funding rate: exchange={cfg.key} symbol={symbol} error={exc}",
                file=sys.stderr,
            )
            return None
        rate = raw.get("fundingRate")
        if rate is None:
            return None
        return (cfg.key, symbol, float(rate))

    async def fetch_funding_rates(
        self,
        configs: Iterable[ExchangeConfig],
        symbols_by_exchange: dict[str, Iterable[str]],
    ) -> dict[tuple[str, str], float]:
        tasks = []
        for cfg in configs:
            for symbol in symbols_by_exchange.get(cfg.key, []):
                tasks.append(self.fetch_funding_rate(cfg, symbol))
        results = await asyncio.gather(*tasks)
        return {
            (exchange, symbol): rate
            for result in results
            if result is not None
            for exchange, symbol, rate in [result]
        }

    async def create_limit_order(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        side: Side,
        amount: float,
        price: float,
        post_only: bool = True,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        client = self.client(cfg)
        await client.load_markets()
        order_amount = float(client.amount_to_precision(symbol, amount))
        order_price = float(client.price_to_precision(symbol, price))
        params: dict[str, Any] = {}
        if post_only:
            params["postOnly"] = True
        if client_order_id:
            params["clientOrderId"] = client_order_id
        return await client.create_order(
            symbol,
            "limit",
            side,
            order_amount,
            order_price,
            params,
        )

    async def cancel_open_orders(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
    ) -> list[dict[str, Any]]:
        client = self.client(cfg)
        cancel_all = getattr(client, "cancel_all_orders", None)
        if cancel_all is not None:
            result = await cancel_all(symbol)
            return result if isinstance(result, list) else [result]

        open_orders = await client.fetch_open_orders(symbol)
        canceled = []
        for order in open_orders:
            canceled.append(await client.cancel_order(order["id"], symbol))
        return canceled
