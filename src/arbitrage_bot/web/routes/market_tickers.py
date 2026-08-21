from __future__ import annotations

import json

from aiohttp import web

from ..market_tickers import MarketTickerService
from ..security import _request_user, write_web_audit_event


async def api_market_tickers(request: web.Request) -> web.Response:
    user = _request_user(request)
    owner_email = user.email if user is not None else "legacy@local"
    service: MarketTickerService = request.app["market_ticker_service"]
    try:
        if request.method == "POST":
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("items must be a list")
            service.save(owner_email, items)
            write_web_audit_event(
                request.app["config"],
                request,
                action="market_watchlist_update",
                target=owner_email,
                detail=f"saved {len(items)} market ticker item(s)",
                payload={"items": items},
            )
        result = await service.snapshot(
            owner_email,
            force=request.method == "POST",
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response(result)
