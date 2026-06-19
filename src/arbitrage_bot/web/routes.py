from __future__ import annotations

from aiohttp import web


def register_routes(app: web.Application) -> None:
    from . import (
        STATIC_DIR,
        api_cancel_bulk_orders,
        api_cancel_order,
        api_cash_and_carry_pairs,
        api_cleanup_auto_buy_sell_tasks,
        api_control,
        api_control_auto_buy_sell_task,
        api_create_auto_buy_sell_task,
        api_dca,
        api_health,
        api_market_maker,
        api_markets,
        api_profile,
        api_risk,
        api_slow_execution,
        api_spot_grid,
        api_state,
        api_strategy_control,
        index,
        login_get,
        login_post,
        logout,
        register_get,
        register_post,
    )

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/register", register_get)
    app.router.add_post("/register", register_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC_DIR, name="static", append_version=True)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/control", api_control)
    app.router.add_post("/api/markets", api_markets)
    app.router.add_post("/api/cash-and-carry-pairs", api_cash_and_carry_pairs)
    app.router.add_post("/api/risk", api_risk)
    app.router.add_post("/api/market-maker", api_market_maker)
    app.router.add_post("/api/spot-grid", api_spot_grid)
    app.router.add_post("/api/dca", api_dca)
    app.router.add_post("/api/auto-buy-sell", api_slow_execution)
    app.router.add_post("/api/slow-execution", api_slow_execution)
    app.router.add_post("/api/auto-buy-sell/tasks", api_create_auto_buy_sell_task)
    app.router.add_post(
        "/api/auto-buy-sell/tasks/cleanup",
        api_cleanup_auto_buy_sell_tasks,
    )
    app.router.add_post(
        "/api/auto-buy-sell/tasks/{task_id}/control",
        api_control_auto_buy_sell_task,
    )
    app.router.add_post("/api/orders/cancel", api_cancel_order)
    app.router.add_post("/api/orders/cancel-bulk", api_cancel_bulk_orders)
    app.router.add_post("/api/strategies/control", api_strategy_control)
    app.router.add_get("/api/health", api_health)
