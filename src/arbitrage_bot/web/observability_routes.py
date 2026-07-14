from __future__ import annotations

from pathlib import Path

from aiohttp import web

from ..observability import render_prometheus_metrics
from .deployment import RuntimeSupervisor
from .security import STATIC_CACHE_CONTROL
from .state import MonitorState


STATIC_DIR = Path(__file__).resolve().parent / "static"


async def api_health(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    payload = await state.get(view="status")
    supervisor = request.app.get("runtime_supervisor")
    deployment = (
        supervisor.status()
        if isinstance(supervisor, RuntimeSupervisor)
        else {
            "process_ready": True,
            "deployment_ready": True,
            "role": "legacy",
            "leader_ready": True,
            "mutation_allowed": True,
            "error": None,
        }
    )
    order_activity = payload.get("order_activity", {})
    reliability = payload.get("order_reliability") or order_activity.get(
        "reliability", {}
    )
    pending_intents = max(
        int(reliability.get("pending_count") or 0),
        int(reliability.get("unresolved_count") or 0),
    )
    safe_to_replace = bool(
        deployment.get("deployment_ready")
        and not deployment.get("error")
        and pending_intents == 0
    )
    health = {
        "ok": bool(deployment.get("process_ready") and not deployment.get("error")),
        "deployment": deployment,
        "runtime": {
            "status": payload.get("status"),
            "program_running": bool(payload.get("program", {}).get("running")),
            "lifecycle": payload.get("strategy_lifecycle", {}).get("summary", {}),
            "open_order_count": int(order_activity.get("open_order_count") or 0),
            "active_market_makers": int(
                payload.get("market_maker", {}).get("active_instance_count") or 0
            ),
            "active_auto_tasks": int(
                payload.get("slow_execution", {})
                .get("tasks", {})
                .get("active_count", 0)
                or 0
            ),
            "pending_order_intents": pending_intents,
        },
        "safe_to_replace": safe_to_replace,
    }
    return web.json_response(health, status=200 if health["ok"] else 503)


async def api_metrics(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    payload = await state.get()
    return web.Response(
        text=render_prometheus_metrics(payload),
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


async def favicon(_: web.Request) -> web.FileResponse:
    return web.FileResponse(
        STATIC_DIR / "favicon.svg",
        headers={
            "Content-Type": "image/svg+xml",
            "Cache-Control": STATIC_CACHE_CONTROL,
        },
    )
