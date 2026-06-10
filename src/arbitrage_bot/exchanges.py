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


REST_PROXY_ENV_OPTIONS = (
    ("http_proxy_env", "httpProxy"),
    ("https_proxy_env", "httpsProxy"),
    ("socks_proxy_env", "socksProxy"),
)

WEBSOCKET_PROXY_ENV_OPTIONS = (
    ("ws_proxy_env", "wsProxy"),
    ("wss_proxy_env", "wssProxy"),
    ("ws_socks_proxy_env", "wsSocksProxy"),
)


def _single_proxy_option(
    cfg: ExchangeConfig,
    env_options: Iterable[tuple[str, str]],
    proxy_type: str,
) -> dict[str, str]:
    active = []
    for env_field, option_key in env_options:
        env_name = getattr(cfg, env_field)
        if env_name and os.environ.get(env_name):
            active.append((option_key, env_name, os.environ[env_name]))

    if len(active) > 1:
        names = ", ".join(env_name for _, env_name, _ in active)
        raise ValueError(
            f"exchange {cfg.key} has multiple {proxy_type} proxy env vars set: "
            f"{names}. Configure only one proxy per account."
        )

    if not active:
        return {}

    option_key, _, proxy_url = active[0]
    return {option_key: proxy_url}


def _proxy_options_from_env(cfg: ExchangeConfig) -> dict[str, str]:
    return {
        **_single_proxy_option(cfg, REST_PROXY_ENV_OPTIONS, "REST"),
        **_single_proxy_option(cfg, WEBSOCKET_PROXY_ENV_OPTIONS, "WebSocket"),
    }


def _credential_from_env(env_name: str | None) -> str | None:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if not value:
        return None
    return value.replace("\\n", "\n")


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
        options.update(_proxy_options_from_env(cfg))
        if cfg.market_type != "spot":
            options["options"].setdefault("defaultType", cfg.market_type)

        api_key = _credential_from_env(cfg.api_key_env)
        secret = _credential_from_env(cfg.secret_env)
        password = _credential_from_env(cfg.password_env)
        if api_key:
            options["apiKey"] = api_key
        if secret:
            options["secret"] = secret
        if password:
            options["password"] = password

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

    async def fetch_open_orders(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
    ) -> list[dict[str, Any]]:
        client = self.client(cfg)
        return await client.fetch_open_orders(symbol)

    async def fetch_closed_orders(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        client = self.client(cfg)
        fetcher = getattr(client, "fetch_closed_orders", None)
        if fetcher is None:
            return []
        return await fetcher(symbol, None, limit)

    async def fetch_my_trades(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        client = self.client(cfg)
        fetcher = getattr(client, "fetch_my_trades", None)
        if fetcher is None:
            return []
        return await fetcher(symbol, None, limit)

    async def fetch_balance(self, cfg: ExchangeConfig) -> dict[str, Any]:
        client = self.client(cfg)
        return await client.fetch_balance()

    async def fetch_market_info(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
    ) -> dict[str, Any] | None:
        client = self.client(cfg)
        markets = await client.load_markets()
        if isinstance(markets, dict) and symbol in markets:
            market = markets[symbol]
            return market if isinstance(market, dict) else None

        market_getter = getattr(client, "market", None)
        if market_getter is None:
            return None
        market = market_getter(symbol)
        return market if isinstance(market, dict) else None

    async def cancel_order(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        order_id: str,
    ) -> dict[str, Any]:
        client = self.client(cfg)
        return await client.cancel_order(order_id, symbol)
