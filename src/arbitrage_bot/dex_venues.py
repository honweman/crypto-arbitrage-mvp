from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp


VENUE_IDS = {"hyperliquid", "polymarket", "dydx", "aster"}
DEFAULT_TIMEOUT_SECONDS = 12.0


def _list_size(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _dict_size(value: Any) -> int:
    return len(value) if isinstance(value, dict) else 0


async def _json_response(response: aiohttp.ClientResponse) -> Any:
    if response.status >= 400:
        body = (await response.text())[:200]
        raise RuntimeError(f"HTTP {response.status}: {body}")
    return await response.json(content_type=None)


async def _probe_hyperliquid(
    session: aiohttp.ClientSession,
    address: str,
) -> dict[str, Any]:
    endpoint = "https://api.hyperliquid.xyz/info"
    async with session.post(endpoint, json={"type": "meta"}) as response:
        meta = await _json_response(response)
    async with session.post(
        endpoint,
        json={"type": "clearinghouseState", "user": address},
    ) as response:
        account = await _json_response(response)
    return {
        "market_count": _list_size((meta or {}).get("universe")),
        "account_value": (account or {}).get("marginSummary", {}).get("accountValue"),
        "position_count": _list_size((account or {}).get("assetPositions")),
    }


async def _probe_polymarket(
    session: aiohttp.ClientSession,
    address: str,
) -> dict[str, Any]:
    async with session.get("https://clob.polymarket.com/time") as response:
        server_time = await _json_response(response)
    async with session.get(
        "https://data-api.polymarket.com/positions",
        params={"user": address, "sizeThreshold": "0.01", "limit": "100"},
    ) as response:
        positions = await _json_response(response)
    return {
        "server_time": server_time,
        "position_count": _list_size(positions),
    }


async def _probe_dydx(session: aiohttp.ClientSession) -> dict[str, Any]:
    async with session.get(
        "https://indexer.dydx.trade/v4/perpetualMarkets",
    ) as response:
        payload = await _json_response(response)
    markets = (payload or {}).get("markets")
    return {"market_count": _dict_size(markets)}


async def _probe_aster(
    session: aiohttp.ClientSession,
    address: str,
) -> dict[str, Any]:
    request = {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "aster_getBalance",
        "params": [address, "latest"],
    }
    async with session.post(
        "https://tapi.asterdex.com/info",
        json=request,
    ) as response:
        payload = await _json_response(response)
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"])[:200])
    result = (payload or {}).get("result") if isinstance(payload, dict) else None
    return {
        "balance_available": isinstance(result, dict),
        "position_count": _list_size((result or {}).get("positions")),
    }


async def probe_dex_venue(
    *,
    venue: str,
    wallet_address: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    venue_id = str(venue or "").strip().lower()
    if venue_id not in VENUE_IDS:
        raise ValueError(f"unsupported decentralized venue: {venue_id}")
    address = str(wallet_address or "").strip()
    if venue_id != "dydx" and not address:
        raise ValueError(f"{venue_id} requires a verified wallet")
    started = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=max(2.0, float(timeout_seconds)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if venue_id == "hyperliquid":
                detail = await _probe_hyperliquid(session, address)
            elif venue_id == "polymarket":
                detail = await _probe_polymarket(session, address)
            elif venue_id == "dydx":
                detail = await _probe_dydx(session)
            else:
                detail = await _probe_aster(session, address)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        return {
            "status": "error",
            "venue": venue_id,
            "wallet_address": address,
            "error": f"{exc.__class__.__name__}: {exc}"[:240],
            "latency_ms": (time.perf_counter() - started) * 1000,
            "checked_at": time.time(),
            "live_trading_authorized": False,
        }
    return {
        "status": "healthy",
        "venue": venue_id,
        "wallet_address": address,
        "detail": detail,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "checked_at": time.time(),
        "live_trading_authorized": False,
    }
