from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from .background import (
    auto_buy_sell_task_loop,
    cross_exchange_rebalance_task_loop,
    market_maker_task_loop,
    monitor_loop,
    spot_grid_task_loop,
)
from .core import (
    _all_account_exchanges,
    default_market_watchlist_path,
    default_runtime_store_path,
    default_strategy_center_path,
    default_user_backtest_path,
    default_user_paper_trading_path,
    default_user_workspace_path,
    default_web_user_store_path,
)
from .deployment import (
    RuntimeLeaderLease,
    RuntimeSupervisor,
    deployment_mutation_middleware,
    zero_downtime_enabled,
)
from .market_tickers import MarketTickerService, MarketWatchlistStore
from .strategy_preflight import (
    StrategyPreflightService,
)
from .users import (
    WebUserStore,
)
from .verification import (
    EmailVerificationManager,
    VerificationEmailSender,
)

from .preflight import PreflightError, enforce_preflight
from .security import (
    LoginRateLimiter,
    build_security_middleware,
    performance_middleware,
    write_system_web_audit_event,
)
from .routes.monitor import (
    STATE_PAYLOAD_BUILDER_KEY,
)
from .routes.control import _watch_startup_configuration
from .routes.profile import _state_payload_for_request
from .state import MonitorState

from ..auto_buy_sell_task import (
    AutoBuySellTaskService,
    default_task_store_path,
)
from ..config import (
    BotConfig,
    load_config,
)
from ..exchanges import ExchangeManager
from ..data_backup import backup_task_loop
from ..observability import configure_logging
from ..main import (
    StrategyName,
)
from ..strategy_center import (
    StrategyCenterStore,
)
from ..venue_health import (
    venue_connection_health_loop,
)
from ..user_backtesting import UserBacktestService, UserBacktestStore
from ..user_account_check import (
    WorkspaceAccountCheckService,
    WorkspaceMarketDiscoveryService,
)
from ..user_account_health import workspace_account_health_loop
from ..user_paper_engine import (
    UserPaperTradingService,
    user_paper_trading_task_loop,
)
from ..user_paper_store import UserPaperTradingStore
from ..user_workspace import (
    UserWorkspaceStore,
)
from ..workspace_runtime import (
    build_workspace_runtime_accounts,
)


def _activate_registered_pending_projects(
    web_user_store: WebUserStore,
    workspace_store: UserWorkspaceStore,
) -> list[str]:
    activated: list[str] = []
    for project in workspace_store.list_projects(owner_email="", is_admin=True):
        if project.status != "pending":
            continue
        if web_user_store.get_user(project.owner_email) is None:
            continue
        workspace_store.set_project_status(project.id, "active")
        activated.append(project.id)
    return activated


def create_app(
    cfg: BotConfig,
    strategy: StrategyName,
    poll_seconds: float | None,
) -> web.Application:
    interval = cfg.poll_seconds if poll_seconds is None else poll_seconds
    os.environ.setdefault(
        "CRYPTO_ARB_ORDER_JOURNAL_PATH",
        str(Path(cfg.trade_log.path).with_name("order_intents.sqlite3")),
    )
    app = web.Application(
        middlewares=[
            build_security_middleware(cfg),
            deployment_mutation_middleware,
            performance_middleware,
        ]
    )
    auto_buy_sell_tasks = AutoBuySellTaskService(default_task_store_path(cfg))
    web_user_store = WebUserStore(
        default_web_user_store_path(cfg),
        master_key_env=cfg.web_security.credential_master_key_env,
    )
    web_user_store.migrate_totp_secrets()
    user_workspace_store = UserWorkspaceStore(
        default_user_workspace_path(cfg),
        master_key_env=cfg.web_security.credential_master_key_env,
    )
    self_service_project_migrations = _activate_registered_pending_projects(
        web_user_store,
        user_workspace_store,
    )
    workspace_runtime_accounts = build_workspace_runtime_accounts(
        user_workspace_store,
        owner_emails=[
            row.email for row in web_user_store.list_users() if row.role == "admin"
        ],
    )
    state = MonitorState(
        cfg,
        interval,
        runtime_store_path=default_runtime_store_path(cfg),
        workspace_runtime_accounts=workspace_runtime_accounts,
    )
    user_paper_store = UserPaperTradingStore(default_user_paper_trading_path(cfg))
    user_paper_service = UserPaperTradingService(
        user_workspace_store,
        user_paper_store,
        quote_rates=cfg.quote_rates,
        common_quote_currency=cfg.common_quote_currency,
        order_book_depth=cfg.order_book_depth,
    )
    user_backtest_store = UserBacktestStore(default_user_backtest_path(cfg))
    user_backtest_service = UserBacktestService(
        user_workspace_store,
        user_backtest_store,
    )
    workspace_market_discovery = WorkspaceMarketDiscoveryService()
    workspace_account_checker = WorkspaceAccountCheckService()
    market_ticker_service = MarketTickerService(
        MarketWatchlistStore(default_market_watchlist_path(cfg))
    )
    strategy_preflight_service = StrategyPreflightService()
    strategy_center_store = StrategyCenterStore(
        default_strategy_center_path(cfg),
        max_recent_signals=cfg.strategy_center.max_recent_signals,
    )
    app["monitor_state"] = state
    app["config"] = cfg
    app["auto_buy_sell_tasks"] = auto_buy_sell_tasks
    app["web_user_store"] = web_user_store
    app["user_workspace_store"] = user_workspace_store
    app["self_service_project_migrations"] = self_service_project_migrations
    app["user_paper_store"] = user_paper_store
    app["user_paper_service"] = user_paper_service
    app["user_backtest_store"] = user_backtest_store
    app["user_backtest_service"] = user_backtest_service
    app["workspace_market_discovery"] = workspace_market_discovery
    app["workspace_account_checker"] = workspace_account_checker
    app["market_ticker_service"] = market_ticker_service
    app["strategy_preflight_service"] = strategy_preflight_service
    app["config_guard_tasks"] = set()
    app["strategy_center_store"] = strategy_center_store
    app["login_rate_limiter"] = LoginRateLimiter()
    app["email_verification_manager"] = EmailVerificationManager(
        ttl_seconds=cfg.web_security.verification_code_ttl_seconds,
        resend_seconds=cfg.web_security.verification_resend_seconds,
        max_attempts=cfg.web_security.verification_max_attempts,
    )
    app["verification_email_sender"] = VerificationEmailSender(cfg.alerts)

    leader_lock_path = os.environ.get("CRYPTO_ARB_LEADER_LOCK_PATH") or str(
        Path(cfg.trade_log.path).with_name("runtime_leader.lock")
    )
    leader_lease = RuntimeLeaderLease(leader_lock_path)

    async def recover_startup_orders() -> dict[str, Any]:
        startup_manager = ExchangeManager()
        try:
            startup_cfg = await state.runtime_config(cfg)
            startup_recovery = await startup_manager.recover_pending_order_intents(
                _all_account_exchanges(startup_cfg),
                resolve_confirmed_absent=True,
            )
            await state.set_order_reliability(startup_recovery)
            return startup_recovery
        finally:
            await startup_manager.close()

    async def handle_runtime_failure(reason: str) -> None:
        await state.set_auto_stopped(reason=reason)
        write_system_web_audit_event(
            cfg,
            action="runtime_supervisor_auto_stop",
            status="error",
            target="program",
            detail=reason,
        )

    supervisor = RuntimeSupervisor(
        leader_lease,
        task_factories={
            "monitor": lambda: monitor_loop(
                cfg,
                strategy,
                state,
                interval,
                strategy_center_store=strategy_center_store,
            ),
            "market_maker": lambda: market_maker_task_loop(
                cfg,
                state,
                user_workspace_store,
            ),
            "cross_exchange_rebalance": lambda: cross_exchange_rebalance_task_loop(
                cfg, state
            ),
            "spot_grid": lambda: spot_grid_task_loop(cfg, state),
            "auto_buy_sell": lambda: auto_buy_sell_task_loop(
                cfg,
                state,
                auto_buy_sell_tasks,
                user_workspace_store,
            ),
            "user_paper": lambda: user_paper_trading_task_loop(
                user_paper_service,
                running_check=state.is_running,
                quote_rates_provider=state.quote_rates,
            ),
        },
        recover_orders=recover_startup_orders,
        on_failure=handle_runtime_failure,
        startup_guard=lambda: _watch_startup_configuration(app),
        enforce_leader_writes=zero_downtime_enabled(),
    )
    app["runtime_supervisor"] = supervisor

    async def monitor_context(app_: web.Application) -> Any:
        supervisor_task = asyncio.create_task(
            supervisor.run(),
            name="runtime-supervisor",
        )
        venue_health_task = asyncio.create_task(
            venue_connection_health_loop(
                user_workspace_store,
                leader_check=lambda: (
                    supervisor.role == "leader" and supervisor.leader_ready
                ),
            ),
            name="venue-connection-health",
        )
        workspace_account_health_task = asyncio.create_task(
            workspace_account_health_loop(
                user_workspace_store,
                workspace_account_checker,
                leader_check=lambda: (
                    supervisor.role == "leader" and supervisor.leader_ready
                ),
            ),
            name="workspace-account-health",
        )
        backup_task: asyncio.Task[Any] | None = None
        if cfg.backup.enabled:
            backup_task = asyncio.create_task(
                backup_task_loop(cfg),
                name="data-backup",
            )
        try:
            yield
        finally:
            guard_tasks: set[asyncio.Task[Any]] = app_["config_guard_tasks"]
            for guard_task in list(guard_tasks):
                guard_task.cancel()
            supervisor_task.cancel()
            venue_health_task.cancel()
            workspace_account_health_task.cancel()
            if backup_task is not None:
                backup_task.cancel()
            if guard_tasks:
                await asyncio.gather(*guard_tasks, return_exceptions=True)
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor_task
            await asyncio.gather(venue_health_task, return_exceptions=True)
            await asyncio.gather(
                workspace_account_health_task,
                return_exceptions=True,
            )
            if backup_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await backup_task
            await user_backtest_service.close()
            await user_paper_service.close()
            await market_ticker_service.close()

    app.cleanup_ctx.append(monitor_context)

    from .routes import register_routes

    app[STATE_PAYLOAD_BUILDER_KEY] = _state_payload_for_request
    register_routes(app)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crypto arbitrage monitor web UI")
    parser.add_argument(
        "--config", default="config.acs.json", help="Path to JSON config"
    )
    parser.add_argument(
        "--strategy",
        choices=[
            "all",
            "spot-spread",
            "cash-and-carry",
            "options-arbitrage",
            "triangular-arbitrage",
        ],
        default="spot-spread",
        help="Strategy to monitor",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override config poll interval",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Log production preflight errors instead of refusing to start",
    )
    return parser


def main() -> None:
    configure_logging()
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    try:
        enforce_preflight(cfg, strict=not args.skip_preflight)
    except PreflightError as exc:
        for message in exc.errors:
            print(f"preflight error: {message}", file=sys.stderr)
        print(
            "refusing to start; fix the configuration above or rerun with "
            "--skip-preflight to start anyway",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    app = create_app(cfg, args.strategy, args.poll_seconds)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
