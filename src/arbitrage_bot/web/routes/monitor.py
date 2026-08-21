from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from ..security import _add_security_headers


StatePayloadBuilder = Callable[[web.Request], Awaitable[dict[str, Any]]]
STATE_PAYLOAD_BUILDER_KEY: web.AppKey[StatePayloadBuilder] = web.AppKey(
    "state_payload_builder"
)

STATE_STREAM_MIN_INTERVAL_SECONDS = 1.0
STATE_STREAM_MAX_INTERVAL_SECONDS = 15.0
STATE_STREAM_DEFAULT_INTERVAL_SECONDS = 2.0


async def api_state(request: web.Request) -> web.Response:
    return web.json_response(await request.app[STATE_PAYLOAD_BUILDER_KEY](request))


async def api_state_stream(request: web.Request) -> web.StreamResponse:
    """Push view-scoped state snapshots over server-sent events."""
    try:
        interval = float(request.query.get("interval", ""))
    except ValueError:
        interval = STATE_STREAM_DEFAULT_INTERVAL_SECONDS
    interval = min(
        STATE_STREAM_MAX_INTERVAL_SECONDS,
        max(STATE_STREAM_MIN_INTERVAL_SECONDS, interval),
    )
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
    _add_security_headers(response)
    await response.prepare(request)
    try:
        while True:
            payload = await request.app[STATE_PAYLOAD_BUILDER_KEY](request)
            body = json.dumps(payload)
            await response.write(f"data: {body}\n\n".encode("utf-8"))
            await asyncio.sleep(interval)
    except (
        asyncio.CancelledError,
        ConnectionResetError,
        ConnectionError,
        RuntimeError,
    ):
        pass
    return response
