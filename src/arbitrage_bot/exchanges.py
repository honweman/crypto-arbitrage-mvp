from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from .config import ExchangeConfig
from .models import OrderBookSnapshot, Side
from .order_validation import validate_prepared_limit_order
from .orderbook import normalize_levels


LOGGER = logging.getLogger(__name__)


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

CCXT_TOP_LEVEL_OPTION_KEYS = {
    "hostname",
    "urls",
}


@dataclass(frozen=True)
class LimitOrderFeatures:
    post_only: bool = True
    client_order_id: bool = True
    recover_by_client_order_id: bool = True
    batch_create: bool = False
    batch_cancel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_only": self.post_only,
            "client_order_id": self.client_order_id,
            "recover_by_client_order_id": self.recover_by_client_order_id,
            "batch_create": self.batch_create,
            "batch_cancel": self.batch_cancel,
        }


LIMIT_ORDER_FEATURE_OVERRIDES: dict[str, LimitOrderFeatures] = {
    "bithumb": LimitOrderFeatures(
        post_only=False,
        client_order_id=True,
        recover_by_client_order_id=True,
    ),
    "bybit": LimitOrderFeatures(
        post_only=True,
        client_order_id=True,
        recover_by_client_order_id=True,
        batch_create=True,
        batch_cancel=True,
    ),
    "binance": LimitOrderFeatures(
        post_only=True,
        client_order_id=True,
        recover_by_client_order_id=True,
        batch_create=True,
        batch_cancel=True,
    ),
    "binanceusdm": LimitOrderFeatures(
        post_only=True,
        client_order_id=True,
        recover_by_client_order_id=True,
        batch_create=True,
        batch_cancel=True,
    ),
    "coinbase": LimitOrderFeatures(
        post_only=True,
        client_order_id=True,
        recover_by_client_order_id=True,
        batch_cancel=True,
    ),
    "upbit": LimitOrderFeatures(
        post_only=True,
        client_order_id=True,
        recover_by_client_order_id=True,
    ),
}


def limit_order_features(cfg: ExchangeConfig) -> LimitOrderFeatures:
    return LIMIT_ORDER_FEATURE_OVERRIDES.get(cfg.id, LimitOrderFeatures())


def limit_order_capability_errors(
    cfg: ExchangeConfig,
    *,
    post_only: bool,
    client_order_id: str | None = None,
) -> list[str]:
    features = limit_order_features(cfg)
    errors = []
    if post_only and not features.post_only:
        errors.append(
            f"{cfg.key} limit orders do not support post-only through the configured API; "
            "set market_maker.post_only=false and risk.require_post_only=false "
            "only if you accept taker-fill risk"
        )
    if client_order_id and not features.client_order_id:
        errors.append(f"{cfg.key} does not support client order ids")
    return errors


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


def _symbol_quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0].upper()


def _upbit_usdt_tick_size(price: float) -> Decimal:
    value = Decimal(str(price))
    if value >= Decimal("10"):
        return Decimal("0.01")
    if value >= Decimal("1"):
        return Decimal("0.001")
    if value >= Decimal("0.1"):
        return Decimal("0.0001")
    if value >= Decimal("0.01"):
        return Decimal("0.00001")
    if value >= Decimal("0.001"):
        return Decimal("0.000001")
    if value >= Decimal("0.0001"):
        return Decimal("0.0000001")
    return Decimal("0.00000001")


def _upbit_tick_size(symbol: str, price: float) -> Decimal | None:
    quote_currency = _symbol_quote_currency(symbol)
    if quote_currency == "USDT":
        return _upbit_usdt_tick_size(price)
    if quote_currency == "BTC":
        return Decimal("0.00000001")
    return None


def _round_price_to_tick(price: float, tick_size: Decimal, side: Side) -> float:
    value = Decimal(str(price))
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    ticks = (value / tick_size).to_integral_value(rounding=rounding)
    rounded = ticks * tick_size
    return float(rounded)


def _limit_price_to_exchange_tick(
    cfg: ExchangeConfig,
    *,
    symbol: str,
    side: Side,
    price: float,
) -> float:
    if cfg.id != "upbit":
        return price
    tick_size = _upbit_tick_size(symbol, price)
    if tick_size is None:
        return price
    return _round_price_to_tick(price, tick_size, side)


def _market_from_loaded_markets(
    client: Any,
    markets: Any,
    symbol: str,
) -> dict[str, Any] | None:
    market = None
    if isinstance(markets, dict):
        market = markets.get(symbol)
    if market is None:
        market_getter = getattr(client, "market", None)
        if market_getter is not None:
            market = market_getter(symbol)
    return market if isinstance(market, dict) else None


class BithumbV2Error(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _jwt_hs256(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _api_value(value: Any) -> str:
    if isinstance(value, float):
        return format(value, ".15f").rstrip("0").rstrip(".") or "0"
    return str(value)


def _bithumb_query_string(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    pairs: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                pairs.append(f"{key}[]={_api_value(item)}")
        else:
            pairs.append(f"{key}={_api_value(value)}")
    return "&".join(pairs)


def _bithumb_url_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if "=" not in part:
            pairs.append(quote(part, safe="[]-_.~"))
            continue
        key, value = part.split("=", 1)
        pairs.append(f"{quote(key, safe='[]-_.~')}={quote(value, safe='-_.~')}")
    return "&".join(pairs)


def _bithumb_market_code(symbol: str) -> str:
    if "/" not in symbol:
        return symbol
    base, quote_currency = symbol.split("/", 1)
    quote_currency = quote_currency.split(":", 1)[0]
    return f"{quote_currency.upper()}-{base.upper()}"


def _symbol_from_bithumb_market(market: str, fallback: str) -> str:
    if "-" not in market:
        return fallback
    quote_currency, base = market.split("-", 1)
    return f"{base.upper()}/{quote_currency.upper()}"


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_from_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp() * 1000
    except ValueError:
        return None


def _bithumb_side(side: Any) -> str:
    raw = str(side or "").lower()
    if raw == "bid":
        return "buy"
    if raw == "ask":
        return "sell"
    return raw


def _bithumb_status(state: Any) -> str:
    raw = str(state or "").lower()
    if raw in {"wait", "watch"}:
        return "open"
    if raw == "done":
        return "closed"
    if raw == "cancel":
        return "canceled"
    return raw


def _normalize_bithumb_v2_order(raw: dict[str, Any], fallback_symbol: str) -> dict[str, Any]:
    market = str(raw.get("market") or "")
    symbol = _symbol_from_bithumb_market(market, fallback_symbol)
    price = _number_or_none(raw.get("price"))
    amount = _number_or_none(raw.get("volume"))
    filled = _number_or_none(raw.get("executed_volume"))
    remaining = _number_or_none(raw.get("remaining_volume"))
    cost = (
        _number_or_none(raw.get("executed_funds"))
        or _number_or_none(raw.get("paid_amount"))
        or _number_or_none(raw.get("trades_amount"))
    )
    if cost is None and price is not None and filled is not None:
        cost = price * filled
    timestamp = _timestamp_from_iso(
        raw.get("created_at") or raw.get("createdAt") or raw.get("datetime")
    )
    order_id = raw.get("order_id") or raw.get("uuid") or raw.get("id")
    client_order_id = (
        raw.get("client_order_id")
        or raw.get("clientOrderId")
        or raw.get("clientOrderID")
    )
    return {
        "id": str(order_id or ""),
        "clientOrderId": str(client_order_id or ""),
        "clientOrderID": str(client_order_id or ""),
        "symbol": symbol,
        "side": _bithumb_side(raw.get("side")),
        "type": str(raw.get("order_type") or raw.get("ord_type") or ""),
        "status": _bithumb_status(raw.get("state") or raw.get("status")),
        "price": price,
        "average": _number_or_none(raw.get("average_price") or raw.get("avg_price")),
        "amount": amount,
        "filled": filled,
        "remaining": remaining,
        "cost": cost,
        "timestamp": timestamp,
        "datetime": raw.get("created_at") or raw.get("datetime"),
        "fee": {
            "cost": _number_or_none(raw.get("paid_fee")),
            "currency": "",
        }
        if raw.get("paid_fee") is not None
        else None,
        "info": raw,
    }


class BithumbV2Client:
    def __init__(
        self,
        cfg: ExchangeConfig,
        public_client: Any,
        *,
        api_key: str | None,
        secret: str | None,
    ) -> None:
        self.cfg = cfg
        self.public_client = public_client
        self.apiKey = api_key
        self.secret = secret
        self.base_url = str(cfg.options.get("api_url") or "https://api.bithumb.com").rstrip("/")
        self.has = dict(getattr(public_client, "has", {}) or {})
        self.has.update(
            {
                "fetchBalance": True,
                "fetchOpenOrders": True,
                "fetchClosedOrders": True,
                "fetchMyTrades": False,
                "createOrder": True,
                "cancelOrder": True,
            }
        )
        self._session: Any | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.public_client, name)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        close = getattr(self.public_client, "close", None)
        if close is not None:
            await close()

    async def _http_session(self) -> Any:
        if self._session is None:
            aiohttp = importlib.import_module("aiohttp")
            self._session = aiohttp.ClientSession()
        return self._session

    def _authorization(self, query: str = "") -> str:
        if not self.apiKey or not self.secret:
            raise BithumbV2Error("Bithumb v2 API key/secret are not configured")
        payload: dict[str, Any] = {
            "access_key": self.apiKey,
            "nonce": str(uuid4()),
            "timestamp": int(time.time() * 1000),
        }
        if query:
            payload["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return f"Bearer {_jwt_hs256(payload, self.secret)}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        query_params = params if json_body is None else json_body
        query = _bithumb_query_string(query_params)
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{_bithumb_url_query(query)}"
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization(query),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        session = await self._http_session()
        async with session.request(method, url, headers=headers, json=json_body) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else None
            except json.JSONDecodeError:
                payload = text
            if response.status >= 400:
                raise BithumbV2Error(
                    f"HTTP {response.status} {method} {path}: {payload}"
                )
            return payload

    async def fetch_balance(self) -> dict[str, Any]:
        payload = await self._request("GET", "/v1/accounts")
        rows = payload if isinstance(payload, list) else []
        result: dict[str, Any] = {"info": payload, "free": {}, "used": {}, "total": {}}
        for row in rows:
            if not isinstance(row, dict):
                continue
            currency = str(row.get("currency") or "").upper()
            if not currency:
                continue
            free = _number_or_none(row.get("balance")) or 0.0
            used = _number_or_none(row.get("locked")) or 0.0
            total = free + used
            result["free"][currency] = free
            result["used"][currency] = used
            result["total"][currency] = total
            result[currency] = {
                "free": free,
                "used": used,
                "total": total,
            }
        return result

    async def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        params = {
            "market": _bithumb_market_code(symbol),
            "state": "wait",
            "limit": 100,
            "order_by": "desc",
        }
        payload = await self._request("GET", "/v1/orders", params=params)
        rows = payload if isinstance(payload, list) else []
        return [
            _normalize_bithumb_v2_order(row, symbol)
            for row in rows
            if isinstance(row, dict)
        ]

    async def fetch_closed_orders(
        self,
        symbol: str,
        since: Any = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        target_limit = max(1, int(limit or 20))
        orders: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page = 1
        while len(orders) < target_limit:
            request_limit = min(100, target_limit - len(orders))
            params = {
                "market": _bithumb_market_code(symbol),
                "state": "done",
                "limit": request_limit,
                "page": page,
                "order_by": "desc",
            }
            payload = await self._request("GET", "/v1/orders", params=params)
            rows = payload if isinstance(payload, list) else []
            if not rows:
                break
            added = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                order = _normalize_bithumb_v2_order(row, symbol)
                order_id = str(order.get("id") or "")
                if order_id and order_id in seen_ids:
                    continue
                if order_id:
                    seen_ids.add(order_id)
                orders.append(order)
                added += 1
                if len(orders) >= target_limit:
                    break
            if len(rows) < request_limit or added == 0:
                break
            page += 1
        return orders

    async def fetch_my_trades(
        self,
        symbol: str,
        since: Any = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return []

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        side_api = "bid" if str(side).lower() == "buy" else "ask"
        order_type_api = str(order_type or "limit").lower()
        body: dict[str, Any] = {
            "market": _bithumb_market_code(symbol),
            "side": side_api,
            "order_type": order_type_api,
        }
        if order_type_api == "limit":
            body["price"] = _api_value(price)
            body["volume"] = _api_value(amount)
        elif order_type_api == "price":
            body["price"] = _api_value(price if price is not None else amount)
        elif order_type_api == "market":
            body["volume"] = _api_value(amount)
        else:
            raise BithumbV2Error(f"unsupported Bithumb v2 order_type: {order_type}")
        client_order_id = params.get("clientOrderId") or params.get("client_order_id")
        if client_order_id:
            body["client_order_id"] = str(client_order_id)[:36]
        payload = await self._request("POST", "/v2/orders", json_body=body)
        if isinstance(payload, dict):
            return _normalize_bithumb_v2_order(payload, symbol)
        return {"id": "", "symbol": symbol, "side": side, "type": order_type, "info": payload}

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        fallback_symbol = symbol or ""
        payload = await self._request(
            "DELETE",
            "/v2/order",
            params={"order_id": order_id},
        )
        if isinstance(payload, dict):
            return _normalize_bithumb_v2_order(payload, fallback_symbol)
        return {"id": order_id, "symbol": fallback_symbol, "status": "canceled", "info": payload}


class ExchangeManager:
    def __init__(
        self,
        *,
        credentials_by_key: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._clients: dict[str, Any] = {}
        self._credentials_by_key = {
            str(key): {
                str(field): str(value).replace("\\n", "\n")
                for field, value in credentials.items()
                if value
            }
            for key, credentials in (credentials_by_key or {}).items()
        }

    def _build_client(self, cfg: ExchangeConfig) -> Any:
        ccxt = importlib.import_module("ccxt.async_support")
        exchange_cls = getattr(ccxt, cfg.id)
        exchange_options = dict(cfg.options)
        top_level_options = {
            key: exchange_options.pop(key)
            for key in list(exchange_options)
            if key in CCXT_TOP_LEVEL_OPTION_KEYS
        }

        options: dict[str, Any] = {
            "enableRateLimit": True,
            "options": exchange_options,
            **top_level_options,
        }
        options.update(_proxy_options_from_env(cfg))
        if cfg.market_type != "spot":
            options["options"].setdefault("defaultType", cfg.market_type)

        direct_credentials = self._credentials_by_key.get(cfg.key, {})
        api_key = direct_credentials.get("api_key") or _credential_from_env(
            cfg.api_key_env
        )
        secret = direct_credentials.get("secret") or _credential_from_env(
            cfg.secret_env
        )
        password = (
            direct_credentials.get("password")
            or direct_credentials.get("passphrase")
            or _credential_from_env(cfg.password_env)
        )
        if api_key:
            options["apiKey"] = api_key
        if secret:
            options["secret"] = secret
        if password:
            options["password"] = password

        client = exchange_cls(options)
        if cfg.id == "bithumb" and str(cfg.options.get("private_api", "")).lower() in {
            "v2",
            "v2.0",
            "v2.1",
            "v2.1.5",
        }:
            return BithumbV2Client(
                cfg,
                client,
                api_key=api_key,
                secret=secret,
            )

        return client

    def client(self, cfg: ExchangeConfig) -> Any:
        if cfg.key not in self._clients:
            self._clients[cfg.key] = self._build_client(cfg)
        return self._clients[cfg.key]

    async def close(self) -> None:
        await asyncio.gather(
            *[client.close() for client in self._clients.values()],
            return_exceptions=True,
        )
        self._clients.clear()
        self._credentials_by_key.clear()

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
            LOGGER.warning(
                "failed to fetch order book",
                extra={
                    "exchange": cfg.key,
                    "symbol": symbol,
                    "error": str(exc),
                },
            )
            return None

        return OrderBookSnapshot(
            exchange=cfg.key,
            symbol=symbol,
            bids=normalize_levels(raw.get("bids", [])),
            asks=normalize_levels(raw.get("asks", [])),
            timestamp_ms=raw.get("timestamp"),
            source="rest",
            received_at=time.time(),
        )

    def watch_order_book_supported(self, cfg: ExchangeConfig) -> bool:
        client = self.client(cfg)
        capabilities = getattr(client, "has", None) or {}
        return (
            capabilities.get("watchOrderBook") is True
            and getattr(client, "watch_order_book", None) is not None
        )

    async def watch_order_book(
        self,
        cfg: ExchangeConfig,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot | None:
        if not self.watch_order_book_supported(cfg):
            raise NotImplementedError(
                f"{cfg.key} websocket order book is not supported by this ccxt client"
            )
        client = self.client(cfg)
        raw = await client.watch_order_book(symbol, limit=depth)
        return OrderBookSnapshot(
            exchange=cfg.key,
            symbol=symbol,
            bids=normalize_levels(raw.get("bids", [])),
            asks=normalize_levels(raw.get("asks", [])),
            timestamp_ms=raw.get("timestamp"),
            source="websocket",
            received_at=time.time(),
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
            LOGGER.warning(
                "failed to fetch funding rate",
                extra={
                    "exchange": cfg.key,
                    "symbol": symbol,
                    "error": str(exc),
                },
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
        capability_errors = limit_order_capability_errors(
            cfg,
            post_only=post_only,
        )
        if capability_errors:
            raise ValueError("; ".join(capability_errors))

        prepared = await self.prepare_limit_order(
            cfg,
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
        )
        if prepared["errors"]:
            raise ValueError("; ".join(prepared["errors"]))
        client = self.client(cfg)
        order_amount = prepared["amount"]
        order_price = prepared["price"]
        params: dict[str, Any] = {}
        if post_only:
            params["postOnly"] = True
        if client_order_id and limit_order_features(cfg).client_order_id:
            params["clientOrderId"] = client_order_id
        return await client.create_order(
            symbol,
            "limit",
            side,
            order_amount,
            order_price,
            params,
        )

    async def create_prepared_limit_order(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        side: Side,
        prepared: dict[str, Any],
        post_only: bool = True,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        capability_errors = limit_order_capability_errors(
            cfg,
            post_only=post_only,
        )
        if capability_errors:
            raise ValueError("; ".join(capability_errors))
        if prepared.get("errors"):
            raise ValueError("; ".join(str(error) for error in prepared["errors"]))

        client = self.client(cfg)
        params: dict[str, Any] = {}
        if post_only:
            params["postOnly"] = True
        if client_order_id and limit_order_features(cfg).client_order_id:
            params["clientOrderId"] = client_order_id
        return await client.create_order(
            symbol,
            "limit",
            side,
            float(prepared["amount"]),
            float(prepared["price"]),
            params,
        )

    async def create_prepared_limit_orders(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        sides: list[Side],
        prepared_orders: list[dict[str, Any]],
        post_only: bool = True,
        client_order_ids: list[str | None] | None = None,
    ) -> list[dict[str, Any]]:
        capability_errors = limit_order_capability_errors(
            cfg,
            post_only=post_only,
        )
        if capability_errors:
            raise ValueError("; ".join(capability_errors))
        features = limit_order_features(cfg)
        if not features.batch_create:
            raise NotImplementedError(f"{cfg.key} batch order create is not enabled")
        if len(sides) != len(prepared_orders):
            raise ValueError("sides and prepared_orders length mismatch")

        client = self.client(cfg)
        create_many = getattr(client, "create_orders", None)
        if create_many is None or client.has.get("createOrders") is not True:
            raise NotImplementedError(f"{cfg.key} batch order create is not supported")

        order_requests = []
        client_order_ids = client_order_ids or [None] * len(prepared_orders)
        for side, prepared, client_order_id in zip(
            sides,
            prepared_orders,
            client_order_ids,
        ):
            if prepared.get("errors"):
                raise ValueError("; ".join(str(error) for error in prepared["errors"]))
            params: dict[str, Any] = {}
            if post_only:
                params["postOnly"] = True
            if client_order_id and features.client_order_id:
                params["clientOrderId"] = client_order_id
            order_requests.append(
                {
                    "symbol": symbol,
                    "type": "limit",
                    "side": side,
                    "amount": float(prepared["amount"]),
                    "price": float(prepared["price"]),
                    "params": params,
                }
            )

        result = await create_many(order_requests)
        return result if isinstance(result, list) else [result]

    async def prepare_limit_order(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        side: Side,
        amount: float,
        price: float,
    ) -> dict[str, Any]:
        client = self.client(cfg)
        markets = await client.load_markets()
        market = _market_from_loaded_markets(client, markets, symbol)
        order_amount = float(client.amount_to_precision(symbol, amount))
        exchange_price = _limit_price_to_exchange_tick(
            cfg,
            symbol=symbol,
            side=side,
            price=price,
        )
        order_price = float(client.price_to_precision(symbol, exchange_price))
        return validate_prepared_limit_order(
            exchange=cfg.key,
            symbol=symbol,
            side=side,
            requested_amount=amount,
            requested_price=price,
            amount=order_amount,
            price=order_price,
            market=market,
        )

    async def prepare_limit_orders(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        orders: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        client = self.client(cfg)
        markets = await client.load_markets()
        market = _market_from_loaded_markets(client, markets, symbol)
        rows = []
        for order in orders:
            amount = float(order["amount"])
            price = float(order["price"])
            side = order["side"]
            order_amount = float(client.amount_to_precision(symbol, amount))
            exchange_price = _limit_price_to_exchange_tick(
                cfg,
                symbol=symbol,
                side=side,
                price=price,
            )
            order_price = float(client.price_to_precision(symbol, exchange_price))
            rows.append(
                validate_prepared_limit_order(
                    exchange=cfg.key,
                    symbol=symbol,
                    side=side,
                    requested_amount=amount,
                    requested_price=price,
                    amount=order_amount,
                    price=order_price,
                    market=market,
                )
            )
        return rows

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
        capabilities = getattr(client, "has", None) or {}
        if capabilities.get("fetchClosedOrders") is False:
            return []
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
        capabilities = getattr(client, "has", None) or {}
        if capabilities.get("fetchMyTrades") is False:
            return []
        fetcher = getattr(client, "fetch_my_trades", None)
        if fetcher is None:
            return []
        return await fetcher(symbol, None, limit)

    async def fetch_positions(
        self,
        cfg: ExchangeConfig,
        symbols: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        client = self.client(cfg)
        capabilities = getattr(client, "has", None) or {}
        if capabilities.get("fetchPositions") is False:
            return []
        fetcher = getattr(client, "fetch_positions", None)
        if fetcher is None:
            return []
        symbols_list = sorted({symbol for symbol in symbols or [] if symbol})
        if symbols_list:
            return await fetcher(symbols_list)
        return await fetcher()

    async def fetch_balance(self, cfg: ExchangeConfig) -> dict[str, Any]:
        client = self.client(cfg)
        return await client.fetch_balance()

    async def fetch_currency_status(
        self,
        cfg: ExchangeConfig,
        *,
        currencies: Iterable[str],
    ) -> dict[str, Any]:
        client = self.client(cfg)
        capabilities = getattr(client, "has", None) or {}
        fetcher = getattr(client, "fetch_currencies", None)
        if capabilities.get("fetchCurrencies") is False or fetcher is None:
            return {
                "checked": False,
                "unsupported": True,
                "currencies": {},
                "skipped_reason": "exchange client does not support fetch_currencies",
            }
        loaded = await fetcher()
        if not isinstance(loaded, dict):
            return {
                "checked": True,
                "unsupported": False,
                "currencies": {},
            }
        requested = {currency.upper() for currency in currencies if currency}
        return {
            "checked": True,
            "unsupported": False,
            "currencies": {
                currency: loaded.get(currency)
                for currency in sorted(requested)
                if isinstance(loaded.get(currency), dict)
            },
        }

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

    async def fetch_ohlcv(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        timeframe: str,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> list[list[Any]]:
        client = self.client(cfg)
        capabilities = getattr(client, "has", None) or {}
        fetcher = getattr(client, "fetch_ohlcv", None)
        if capabilities.get("fetchOHLCV") is False or fetcher is None:
            raise NotImplementedError(f"{cfg.key} does not support public OHLCV data")
        await client.load_markets()
        rows = await fetcher(symbol, timeframe, since_ms, limit)
        return [list(row) for row in rows or [] if isinstance(row, (list, tuple))]

    async def cancel_order(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        order_id: str,
    ) -> dict[str, Any]:
        client = self.client(cfg)
        return await client.cancel_order(order_id, symbol)

    async def cancel_orders(
        self,
        cfg: ExchangeConfig,
        *,
        symbol: str,
        order_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not order_ids:
            return []
        features = limit_order_features(cfg)
        if not features.batch_cancel:
            raise NotImplementedError(f"{cfg.key} batch order cancel is not enabled")
        client = self.client(cfg)
        cancel_many = getattr(client, "cancel_orders", None)
        if cancel_many is None or client.has.get("cancelOrders") is not True:
            raise NotImplementedError(f"{cfg.key} batch order cancel is not supported")
        result = await cancel_many(order_ids, symbol)
        return result if isinstance(result, list) else [result]
