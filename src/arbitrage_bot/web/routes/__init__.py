from __future__ import annotations

from aiohttp import web

from .market_tickers import api_market_tickers
from .monitor import api_state, api_state_stream


def register_routes(app: web.Application) -> None:
    from ..assets import STATIC_DIR
    from ..observability_routes import api_health, api_metrics, favicon
    from ..security import (
        forgot_password_code_post,
        forgot_password_get,
        login_get,
        login_post,
        logout,
        register_code_post,
        register_get,
        register_post,
        reset_password_post,
        security_get,
        security_post,
    )
    from .control import api_control
    from .profile import (
        api_account,
        api_admin_users,
        api_profile,
        index,
    )
    from .strategies import (
        api_backtest,
        api_cross_exchange_rebalance,
        api_dca,
        api_execution_algo,
        api_slow_execution,
        api_spot_grid,
        api_strategy_preflight,
    )
    from .strategy_center import api_signal_webhook, api_strategy_center
    from .trading import (
        api_cancel_bulk_orders,
        api_cancel_order,
        api_cash_and_carry_pairs,
        api_cleanup_auto_buy_sell_tasks,
        api_config_versions_get,
        api_config_versions_post,
        api_control_auto_buy_sell_task,
        api_create_auto_buy_sell_task,
        api_market_maker,
        api_markets,
        api_risk,
        api_strategy_control,
    )
    from .workspace import (
        api_user_backtests_get,
        api_user_backtests_post,
        api_user_workspace,
    )

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/register", register_get)
    app.router.add_post("/register/code", register_code_post)
    app.router.add_post("/register", register_post)
    app.router.add_get("/forgot-password", forgot_password_get)
    app.router.add_post("/forgot-password/code", forgot_password_code_post)
    app.router.add_post("/reset-password", reset_password_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/security", security_get)
    app.router.add_post("/security", security_post)
    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_static("/static/", STATIC_DIR, name="static", append_version=True)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/state/stream", api_state_stream)
    app.router.add_get("/api/market-tickers", api_market_tickers)
    app.router.add_post("/api/market-tickers", api_market_tickers)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/account", api_account)
    app.router.add_post("/api/control", api_control)
    app.router.add_get("/api/config-versions", api_config_versions_get)
    app.router.add_post("/api/config-versions", api_config_versions_post)
    app.router.add_post("/api/markets", api_markets)
    app.router.add_post("/api/cash-and-carry-pairs", api_cash_and_carry_pairs)
    app.router.add_post("/api/risk", api_risk)
    app.router.add_post("/api/market-maker", api_market_maker)
    app.router.add_post(
        "/api/cross-exchange-rebalance",
        api_cross_exchange_rebalance,
    )
    app.router.add_post("/api/spot-grid", api_spot_grid)
    app.router.add_post("/api/dca", api_dca)
    app.router.add_post("/api/execution-algo", api_execution_algo)
    app.router.add_post("/api/backtest", api_backtest)
    app.router.add_post("/api/strategy-center", api_strategy_center)
    app.router.add_post("/api/user-workspace", api_user_workspace)
    app.router.add_get("/api/user-backtests", api_user_backtests_get)
    app.router.add_post("/api/user-backtests", api_user_backtests_post)
    app.router.add_post("/api/signal/{source}", api_signal_webhook)
    app.router.add_post("/api/signal", api_signal_webhook)
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
    app.router.add_post("/api/strategies/preflight", api_strategy_preflight)
    app.router.add_post("/api/admin/users", api_admin_users)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/metrics", api_metrics)
