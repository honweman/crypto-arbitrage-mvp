from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from .state import MonitorState

from ..alerts import AlertService
from ..auto_buy_sell_task import AutoBuySellTaskService
from ..config import BotConfig
from ..exchanges import ExchangeManager
from ..main import (
    StrategyName,
    _quote_rates_from_sources,
    _symbols_for_configured_spot_markets,
    _symbols_for_triangular_routes,
    scan_with_manager,
)
from ..market_maker import (
    cancel_order_ids as cancel_market_maker_order_ids,
    run_cycle as run_market_maker_cycle,
)
from ..models import OrderBookSnapshot
from ..orderbook_cache import OrderBookCache
from ..order_reconciliation import (
    RECONCILIATION_AUTO_STOP_WARMUP_SECONDS,
    _monitor_auto_stop_decision,
    _monitor_reconciliation_warmup_active,
)
from ..pnl import build_portfolio_pnl
from ..portfolio_metrics import (
    _portfolio_position_for_symbol,
    build_synced_portfolio_pnl,
)
from ..risk import current_daily_pnl_quote
from ..solana import SolanaTokenClient
from ..spot_arbitrage_executor import run_spot_arbitrage_execution_cycle
from ..spot_grid_executor import (
    TrackedGridOrder,
    cancel_order_ids as cancel_spot_grid_order_ids,
    fills_from_sync,
    load_plan_order_book as load_spot_grid_order_book,
    load_runtime_state as load_spot_grid_runtime_state,
    run_cycle as run_spot_grid_cycle,
    runtime_state_from_tracked,
    save_runtime_state as save_spot_grid_runtime_state,
    sync_tracked_grid_orders,
    tracked_orders_from_state,
    tracked_orders_from_sync,
)
from ..strategy_center import StrategyCenterStore
from ..strategy_timeline import (
    strategy_timeline_event_from_payload,
    strategy_timeline_fingerprint,
    write_strategy_timeline_from_payload,
)
from ..strategies.spot_spread import find_converted_spot_spread_opportunities
from ..strategies.triangular import find_triangular_arbitrage_opportunities
from ..trade_log import write_trade_event
from ..web_config import (
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    _spot_symbols_by_exchange,
    backtest_config_to_dict,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    market_maker_config_to_dict,
    market_maker_configs_for_runtime,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_grid_config_to_dict,
)
from . import (
    ACCOUNT_BALANCE_POLL_SECONDS,
    ORDER_ACTIVITY_POLL_SECONDS,
    SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
    _all_account_exchanges,
    _build_initial_payload,
    _cached_onchain_payload,
    _find_exchange_by_key,
    _global_scan_health_warnings,
    _missing_market_warnings,
    _onchain_error_payload,
    _risk_account_enabled,
    _risk_strategy_enabled,
    build_market_maker_payload,
    build_market_rows,
    build_backtest_payload,
    build_dca_payload,
    build_execution_algo_payload,
    build_slow_execution_payload,
    build_spot_grid_payload,
    build_trading_console_payload,
    fetch_account_balances_payload,
    fetch_derivatives_risk_payload,
    fetch_funding_basis_payload,
    fetch_onchain_payload,
    fetch_options_arbitrage_payload,
    fetch_order_activity_payload,
    write_system_web_audit_event,
)


def _parse_daily_report_time(value: str) -> tuple[int, int]:
    hour_text, _, minute_text = value.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text or "0")
    except ValueError:
        return (23, 59)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return (23, 59)
    return (hour, minute)


def _daily_report_due(
    cfg: BotConfig,
    *,
    last_report_day: str | None,
    now: float | None = None,
) -> tuple[bool, str]:
    now = time.time() if now is None else now
    local = time.localtime(now)
    day = time.strftime("%Y-%m-%d", local)
    hour, minute = _parse_daily_report_time(cfg.alerts.daily_report_time)
    due = (
        cfg.alerts.daily_report_enabled
        and day != last_report_day
        and (local.tm_hour, local.tm_min) >= (hour, minute)
    )
    return due, day


def build_daily_report_message(
    cfg: BotConfig,
    *,
    scan_count: int,
    order_activity: dict[str, Any],
    account_balances: dict[str, Any],
    trading_console: dict[str, Any],
    auto_buy_sell_tasks: dict[str, Any],
    warnings: list[str],
) -> str:
    daily = order_activity.get("daily_pnl") or {}
    sources = daily.get("sources") or {}
    source_lines = []
    for source, row in sorted(sources.items()):
        if isinstance(row, dict):
            source_lines.append(
                f"- {source}: P/L {row.get('realized_pnl', 0.0):.8f}, "
                f"trades {row.get('trade_count', 0)}"
            )
    if not source_lines:
        source_lines.append("- no realized fills")

    return "\n".join(
        [
            f"Daily trading report ({time.strftime('%Y-%m-%d')})",
            f"Status scans: {scan_count}",
            f"Daily P/L: {daily.get('total_realized_pnl', 0.0):.8f} {cfg.common_quote_currency}",
            f"Daily trades: {daily.get('trade_count', 0)}",
            f"Open orders: {order_activity.get('open_order_count', 0)}",
            f"Recent fills: {order_activity.get('recent_trade_count', 0)}",
            f"Accounts checked: {account_balances.get('checked_account_count', 0)}/{account_balances.get('total_account_count', 0)}",
            f"Live trading: {trading_console.get('live_trading', False)}",
            f"Auto Buy/Sell tasks: {auto_buy_sell_tasks.get('active_count', 0)} active / {auto_buy_sell_tasks.get('task_count', 0)} total",
            f"Warnings: {len(warnings)}",
            "",
            "P/L by source:",
            *source_lines,
        ]
    )


async def monitor_loop(
    cfg: BotConfig,
    strategy: StrategyName,
    state: MonitorState,
    poll_seconds: float,
    strategy_center_store: StrategyCenterStore | None = None,
) -> None:
    manager = ExchangeManager()
    solana_client = (
        SolanaTokenClient(cfg.onchain_monitor.rpc_urls or cfg.onchain_monitor.rpc_url)
        if cfg.onchain_monitor.enabled
        else None
    )
    onchain_payload = (
        _cached_onchain_payload(cfg)
        or _build_initial_payload(cfg, poll_seconds)["onchain"]
    )
    account_balances_payload = _build_initial_payload(cfg, poll_seconds)[
        "account_balances"
    ]
    derivatives_payload = _build_initial_payload(cfg, poll_seconds)["derivatives"]
    funding_basis_payload = _build_initial_payload(cfg, poll_seconds)[
        "funding_basis"
    ]
    options_arbitrage_payload = _build_initial_payload(cfg, poll_seconds)[
        "options_arbitrage"
    ]
    order_activity_payload = _build_initial_payload(cfg, poll_seconds)[
        "order_activity"
    ]
    trading_console_payload = _build_initial_payload(cfg, poll_seconds)[
        "trading_console"
    ]
    market_maker_payload = _build_initial_payload(cfg, poll_seconds)["market_maker"]
    slow_execution_payload = _build_initial_payload(cfg, poll_seconds)[
        "slow_execution"
    ]
    spot_grid_payload = _build_initial_payload(cfg, poll_seconds)["spot_grid"]
    dca_payload = _build_initial_payload(cfg, poll_seconds)["dca"]
    execution_algo_payload = _build_initial_payload(cfg, poll_seconds)[
        "execution_algo"
    ]
    backtest_payload = _build_initial_payload(cfg, poll_seconds)["backtest"]
    spot_arbitrage_payload = _build_initial_payload(cfg, poll_seconds)[
        "spot_arbitrage"
    ]
    portfolio_payload = _build_initial_payload(cfg, poll_seconds)["portfolio"]
    alert_service = AlertService(cfg.alerts)
    next_onchain_scan = 0.0
    next_balance_scan = 0.0
    next_order_activity_scan = 0.0
    consecutive_problem_cycles = 0
    last_daily_report_day: str | None = None
    last_spot_arbitrage_execution_at = 0.0
    last_spot_arbitrage_timeline_fingerprint = ""
    scan_count = 0
    loop_started_monotonic = time.monotonic()
    try:
        while True:
            strategy_center_store_payload: dict[str, Any] = {}
            if strategy_center_store is not None and cfg.strategy_center.enabled:
                try:
                    strategy_center_store_payload = strategy_center_store.read()
                except Exception:  # noqa: BLE001
                    strategy_center_store_payload = {}
            if not await state.is_running():
                now = time.monotonic()
                if now >= next_balance_scan or now >= next_order_activity_scan:
                    runtime_cfg = await state.runtime_config(cfg)
                    runtime_slow_execution = runtime_cfg.slow_execution
                    readonly_warnings: list[str] = []
                    if now >= next_balance_scan:
                        try:
                            account_balances_payload = await fetch_account_balances_payload(
                                runtime_cfg,
                                manager,
                                runtime_slow_execution,
                            )
                        except Exception as exc:  # noqa: BLE001
                            account_balances_payload = {
                                "status": "error",
                                "accounts": [],
                                "totals": [],
                                "checked_account_count": 0,
                                "total_account_count": len(
                                    _all_account_exchanges(runtime_cfg)
                                ),
                                "last_finished": time.time(),
                                "errors": [str(exc)],
                            }
                        try:
                            derivatives_payload = await fetch_derivatives_risk_payload(
                                runtime_cfg,
                                manager,
                            )
                        except Exception as exc:  # noqa: BLE001
                            derivatives_payload = {
                                **_build_initial_payload(runtime_cfg, poll_seconds)[
                                    "derivatives"
                                ],
                                "status": "error",
                                "last_finished": time.time(),
                                "errors": [str(exc)],
                            }
                        try:
                            funding_basis_payload = await fetch_funding_basis_payload(
                                runtime_cfg,
                                manager,
                                strategy_center_payload=strategy_center_store_payload,
                            )
                        except Exception as exc:  # noqa: BLE001
                            funding_basis_payload = {
                                **_build_initial_payload(runtime_cfg, poll_seconds)[
                                    "funding_basis"
                                ],
                                "status": "error",
                                "last_finished": time.time(),
                                "errors": [str(exc)],
                            }
                        try:
                            options_arbitrage_payload = (
                                await fetch_options_arbitrage_payload(
                                    runtime_cfg,
                                    manager,
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            options_arbitrage_payload = {
                                **_build_initial_payload(runtime_cfg, poll_seconds)[
                                    "options_arbitrage"
                                ],
                                "status": "error",
                                "last_finished": time.time(),
                                "errors": [str(exc)],
                            }
                        next_balance_scan = now + ACCOUNT_BALANCE_POLL_SECONDS
                    if now >= next_order_activity_scan:
                        try:
                            auto_tasks_snapshot = await state.auto_buy_sell_tasks()
                            market_maker_runtime_snapshot = (
                                await state.market_maker_runtime()
                            )
                            order_activity_payload = await fetch_order_activity_payload(
                                runtime_cfg,
                                manager,
                                runtime_slow_execution,
                                quote_rates=runtime_cfg.quote_rates,
                                books={},
                                market_maker_runtime=market_maker_runtime_snapshot,
                                auto_buy_sell_tasks=auto_tasks_snapshot,
                            )
                        except Exception as exc:  # noqa: BLE001
                            order_activity_payload = _build_initial_payload(
                                runtime_cfg,
                                poll_seconds,
                            )["order_activity"]
                            order_activity_payload.update(
                                {
                                    "status": "error",
                                    "last_finished": time.time(),
                                    "errors": [str(exc)],
                                }
                            )
                            order_activity_payload["reconciliation"] = {
                                **order_activity_payload.get("reconciliation", {}),
                                "status": "error",
                                "issue_count": 1,
                                "notice_count": 0,
                                "total_item_count": 1,
                                "level_counts": {"error": 1, "warning": 0, "info": 0},
                                "critical_issue_count": 1,
                                "auto_stop_recommended": True,
                                "auto_stop_reasons": [
                                    f"order_activity_error: {str(exc)}"
                                ],
                                "issues": [
                                    {
                                        "level": "error",
                                        "type": "order_activity_error",
                                        "strategy": "",
                                        "exchange": "",
                                        "symbol": "",
                                        "order_id": "",
                                        "source_id": "",
                                        "message": str(exc),
                                    }
                                ],
                                "checked_at": time.time(),
                            }
                        next_order_activity_scan = (
                            now + ORDER_ACTIVITY_POLL_SECONDS
                        )
                    if account_balances_payload.get("status") == "error":
                        errors = account_balances_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Account balances: {errors[0]}")
                    if derivatives_payload.get("status") == "error":
                        errors = derivatives_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Derivatives: {errors[0]}")
                    elif derivatives_payload.get("status") == "blocked":
                        reasons = [
                            reason
                            for account in derivatives_payload.get("accounts", [])
                            for reason in account.get("risk_reasons", [])
                        ]
                        readonly_warnings.append(
                            f"Derivatives: {reasons[0] if reasons else 'risk limit breached'}"
                        )
                    if order_activity_payload.get("status") == "error":
                        errors = order_activity_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Orders: {errors[0]}")
                    if funding_basis_payload.get("status") == "error":
                        errors = funding_basis_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Funding/Basis: {errors[0]}")
                    if options_arbitrage_payload.get("status") == "error":
                        errors = options_arbitrage_payload.get("errors") or [
                            "unavailable"
                        ]
                        readonly_warnings.append(f"Options: {errors[0]}")
                    await state.set_readonly_health(
                        cfg=runtime_cfg,
                        exec_cfg=runtime_slow_execution,
                        account_balances=account_balances_payload,
                        order_activity=order_activity_payload,
                        derivatives=derivatives_payload,
                        funding_basis=funding_basis_payload,
                        options_arbitrage=options_arbitrage_payload,
                        warnings=readonly_warnings,
                    )
                else:
                    await state.set_paused()
                await asyncio.sleep(0.5)
                continue

            monotonic_started = time.monotonic()
            started_at = time.time()
            scan_count += 1
            runtime_cfg = cfg
            try:
                runtime_cfg = await state.runtime_config(cfg)
                runtime_slow_execution = runtime_cfg.slow_execution
                strategy_pauses = await state.strategy_pauses()
                spot_arbitrage_payload = {
                    "type": "spot_spread_execution",
                    "strategy": "spot_spread",
                    "status": "disabled",
                    "mode": "dry_run",
                    "plan": None,
                    "risk": None,
                    "execution": None,
                    "error": None,
                    "cooldown_seconds": SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
                }
                portfolio_books: dict[tuple[str, str], OrderBookSnapshot] = {}
                needs_spot_order_books = bool(
                    runtime_cfg.spot_markets
                    or (
                        runtime_slow_execution.enabled
                        and runtime_slow_execution.exchange
                        and runtime_slow_execution.symbol
                    )
                    or (
                        runtime_cfg.spot_grid.enabled
                        and runtime_cfg.spot_grid.exchange
                        and runtime_cfg.spot_grid.symbol
                    )
                    or (
                        runtime_cfg.dca.enabled
                        and runtime_cfg.dca.exchange
                        and runtime_cfg.dca.symbol
                    )
                    or (
                        runtime_cfg.execution_algo.enabled
                        and runtime_cfg.execution_algo.exchange
                        and runtime_cfg.execution_algo.symbol
                    )
                    or (
                        runtime_cfg.backtest.enabled
                        and runtime_cfg.backtest.exchange
                        and runtime_cfg.backtest.symbol
                    )
                    or (
                        runtime_cfg.market_maker.enabled
                        and runtime_cfg.market_maker.exchange
                        and runtime_cfg.market_maker.symbol
                    )
                )
                if strategy in {"all", "spot-spread"} and needs_spot_order_books:
                    symbols_by_exchange = _symbols_for_configured_spot_markets(
                        runtime_cfg
                    )
                    if (
                        runtime_slow_execution.enabled
                        and runtime_slow_execution.exchange
                        and runtime_slow_execution.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_slow_execution.exchange,
                            set(),
                        ).add(runtime_slow_execution.symbol)
                    if (
                        runtime_cfg.spot_grid.enabled
                        and runtime_cfg.spot_grid.exchange
                        and runtime_cfg.spot_grid.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_cfg.spot_grid.exchange,
                            set(),
                        ).add(runtime_cfg.spot_grid.symbol)
                    if (
                        runtime_cfg.dca.enabled
                        and runtime_cfg.dca.exchange
                        and runtime_cfg.dca.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_cfg.dca.exchange,
                            set(),
                        ).add(runtime_cfg.dca.symbol)
                    if (
                        runtime_cfg.execution_algo.enabled
                        and runtime_cfg.execution_algo.exchange
                        and runtime_cfg.execution_algo.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_cfg.execution_algo.exchange,
                            set(),
                        ).add(runtime_cfg.execution_algo.symbol)
                    if (
                        runtime_cfg.backtest.enabled
                        and runtime_cfg.backtest.exchange
                        and runtime_cfg.backtest.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_cfg.backtest.exchange,
                            set(),
                        ).add(runtime_cfg.backtest.symbol)
                    spot_exchange_keys = {
                        exchange.key for exchange in runtime_cfg.spot_exchanges
                    }
                    if (
                        runtime_cfg.market_maker.enabled
                        and runtime_cfg.market_maker.exchange in spot_exchange_keys
                        and runtime_cfg.market_maker.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_cfg.market_maker.exchange,
                            set(),
                        ).add(runtime_cfg.market_maker.symbol)
                    books = await manager.fetch_order_books(
                        runtime_cfg.spot_exchanges,
                        symbols_by_exchange,
                        runtime_cfg.order_book_depth,
                    )
                    derivative_keys = {
                        exchange.key for exchange in runtime_cfg.derivative_exchanges
                    }
                    if (
                        runtime_cfg.market_maker.enabled
                        and runtime_cfg.market_maker.exchange in derivative_keys
                        and runtime_cfg.market_maker.symbol
                    ):
                        derivative_books = await manager.fetch_order_books(
                            runtime_cfg.derivative_exchanges,
                            {
                                runtime_cfg.market_maker.exchange: {
                                    runtime_cfg.market_maker.symbol
                                }
                            },
                            runtime_cfg.order_book_depth,
                        )
                        books.update(derivative_books)
                    portfolio_books = books
                    quote_rates = _quote_rates_from_sources(runtime_cfg, books)
                    rows = build_market_rows(
                        runtime_cfg.spot_markets,
                        books,
                        quote_rates,
                    )
                    if strategy_pauses.get("spot_spread", False):
                        opportunities = []
                    else:
                        opportunities = find_converted_spot_spread_opportunities(
                            books=books,
                            exchanges=runtime_cfg.spot_exchanges,
                            markets=runtime_cfg.spot_markets,
                            notional_quote=runtime_cfg.notional_quote,
                            min_profit_quote=runtime_cfg.min_profit_quote,
                            min_profit_bps=runtime_cfg.min_profit_bps,
                            quote_rates=quote_rates,
                            common_quote_currency=runtime_cfg.common_quote_currency,
                        )
                    extra_warnings: list[str] = []
                    if (
                        strategy == "all"
                        and runtime_cfg.cash_and_carry_pairs
                        and not strategy_pauses.get("cash_and_carry", False)
                    ):
                        try:
                            opportunities.extend(
                                await scan_with_manager(
                                    runtime_cfg,
                                    "cash-and-carry",
                                    manager,
                                )
                            )
                            opportunities.sort(
                                key=lambda item: item.profit_bps,
                                reverse=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            extra_warnings.append(
                                f"Cash & carry scan failed: {exc.__class__.__name__}: {exc}"
                            )
                    if (
                        strategy == "all"
                        and runtime_cfg.triangular_arbitrage.enabled
                        and runtime_cfg.triangular_arbitrage.routes
                        and not strategy_pauses.get("triangular_arbitrage", False)
                    ):
                        try:
                            triangular_books = await manager.fetch_order_books(
                                runtime_cfg.spot_exchanges,
                                _symbols_for_triangular_routes(
                                    runtime_cfg.triangular_arbitrage.routes
                                ),
                                runtime_cfg.order_book_depth,
                            )
                            opportunities.extend(
                                find_triangular_arbitrage_opportunities(
                                    books=triangular_books,
                                    exchanges=runtime_cfg.spot_exchanges,
                                    cfg=runtime_cfg.triangular_arbitrage,
                                )
                            )
                            opportunities.sort(
                                key=lambda item: item.profit_bps,
                                reverse=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            extra_warnings.append(
                                "Triangular arbitrage scan failed: "
                                f"{exc.__class__.__name__}: {exc}"
                            )
                    warnings = [*_missing_market_warnings(rows), *extra_warnings]
                    if strategy_pauses.get("market_maker", False):
                        market_maker_payload = build_market_maker_payload(
                            runtime_cfg,
                            books,
                            base_cfg=cfg,
                        )
                        market_maker_payload["status"] = "paused"
                        market_maker_payload["mode"] = "paused"
                    else:
                        market_maker_payload = build_market_maker_payload(
                            runtime_cfg,
                            books,
                            base_cfg=cfg,
                        )
                    if strategy_pauses.get("spot_grid", False):
                        spot_grid_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "config": spot_grid_config_to_dict(runtime_cfg.spot_grid),
                            "accounts": slow_execution_accounts(
                                runtime_cfg.spot_exchanges,
                                _grid_symbols_by_exchange(runtime_cfg),
                            ),
                            "error": None,
                        }
                    else:
                        spot_grid_payload = build_spot_grid_payload(
                            runtime_cfg,
                            books,
                        )
                    if strategy_pauses.get("dca", False):
                        dca_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "config": dca_config_to_dict(runtime_cfg.dca),
                            "accounts": slow_execution_accounts(
                                runtime_cfg.spot_exchanges,
                                _grid_symbols_by_exchange(runtime_cfg),
                            ),
                            "error": None,
                        }
                    else:
                        dca_payload = build_dca_payload(
                            runtime_cfg,
                            books,
                        )
                    if strategy_pauses.get("execution_algo", False):
                        execution_algo_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "config": execution_algo_config_to_dict(
                                runtime_cfg.execution_algo
                            ),
                            "accounts": slow_execution_accounts(
                                runtime_cfg.spot_exchanges,
                                _execution_symbols_by_exchange(runtime_cfg),
                            ),
                            "error": None,
                        }
                    else:
                        execution_algo_payload = build_execution_algo_payload(
                            runtime_cfg,
                            books,
                        )
                    if strategy_pauses.get("backtest", False):
                        backtest_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "result": None,
                            "config": backtest_config_to_dict(runtime_cfg.backtest),
                            "accounts": slow_execution_accounts(
                                runtime_cfg.spot_exchanges,
                                _execution_symbols_by_exchange(runtime_cfg),
                            ),
                            "error": None,
                        }
                    else:
                        backtest_payload = build_backtest_payload(runtime_cfg, books)
                    if strategy_pauses.get("slow_execution", False):
                        slow_execution_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "config": slow_execution_config_to_dict(
                                runtime_slow_execution
                            ),
                            "accounts": slow_execution_accounts(
                                runtime_cfg.spot_exchanges,
                                _spot_symbols_by_exchange(runtime_cfg),
                            ),
                            "error": None,
                        }
                    else:
                        slow_execution_payload = build_slow_execution_payload(
                            runtime_cfg,
                            books,
                            runtime_slow_execution,
                        )
                    portfolio_payload = build_portfolio_pnl(
                        runtime_cfg,
                        books,
                        quote_rates,
                    )
                    spot_live_allowed = (
                        runtime_cfg.risk.enabled
                        and runtime_cfg.risk.trading_enabled
                        and runtime_cfg.risk.allow_live_trading
                        and runtime_cfg.risk.strategy_enabled.get("spot_spread", True)
                    )
                    if strategy_pauses.get("spot_spread", False):
                        spot_arbitrage_payload = {
                            **spot_arbitrage_payload,
                            "status": "paused",
                            "mode": "paused",
                        }
                    elif not opportunities:
                        spot_arbitrage_payload = {
                            **spot_arbitrage_payload,
                            "status": "no_opportunity",
                            "mode": "live" if spot_live_allowed else "dry_run",
                        }
                    elif not spot_live_allowed:
                        spot_arbitrage_payload = {
                            **spot_arbitrage_payload,
                            "status": "live_disabled",
                            "mode": "dry_run",
                            "opportunity": opportunities[0].to_dict(),
                        }
                    else:
                        cooldown_remaining = (
                            SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS
                            - (time.monotonic() - last_spot_arbitrage_execution_at)
                        )
                        if cooldown_remaining > 0:
                            spot_arbitrage_payload = {
                                **spot_arbitrage_payload,
                                "status": "cooldown",
                                "mode": "live",
                                "opportunity": opportunities[0].to_dict(),
                                "cooldown_remaining_seconds": cooldown_remaining,
                            }
                        else:
                            spot_arbitrage_payload = await run_spot_arbitrage_execution_cycle(
                                runtime_cfg,
                                manager,
                                opportunities=opportunities,
                                books=books,
                                quote_rates=quote_rates,
                                live=True,
                            )
                            write_trade_event(runtime_cfg.trade_log, spot_arbitrage_payload)
                            if spot_arbitrage_payload.get("status") != "no_opportunity":
                                last_spot_arbitrage_execution_at = time.monotonic()
                    timeline_event = strategy_timeline_event_from_payload(
                        spot_arbitrage_payload,
                        source="monitor",
                    )
                    timeline_fingerprint = strategy_timeline_fingerprint(
                        timeline_event
                    )
                    if timeline_fingerprint != last_spot_arbitrage_timeline_fingerprint:
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            spot_arbitrage_payload,
                            source="monitor",
                        )
                        last_spot_arbitrage_timeline_fingerprint = (
                            timeline_fingerprint
                        )
                else:
                    opportunities = await scan_with_manager(
                        runtime_cfg,
                        strategy,
                        manager,
                    )
                    rows = []
                    quote_rates = runtime_cfg.quote_rates
                    warnings = []
                    market_maker_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "error": None,
                    }
                    slow_execution_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": slow_execution_config_to_dict(
                            runtime_slow_execution
                        ),
                        "accounts": slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _spot_symbols_by_exchange(runtime_cfg),
                        ),
                        "error": None,
                    }
                    spot_grid_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": spot_grid_config_to_dict(runtime_cfg.spot_grid),
                        "accounts": slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _grid_symbols_by_exchange(runtime_cfg),
                        ),
                        "error": None,
                    }
                    dca_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": dca_config_to_dict(runtime_cfg.dca),
                        "accounts": slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _grid_symbols_by_exchange(runtime_cfg),
                        ),
                        "error": None,
                    }
                    execution_algo_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": execution_algo_config_to_dict(
                            runtime_cfg.execution_algo
                        ),
                        "accounts": slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _execution_symbols_by_exchange(runtime_cfg),
                        ),
                        "error": None,
                    }
                    backtest_payload = {
                        "status": "disabled",
                        "mode": "research",
                        "result": None,
                        "config": backtest_config_to_dict(runtime_cfg.backtest),
                        "accounts": slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _execution_symbols_by_exchange(runtime_cfg),
                        ),
                        "error": None,
                    }
                    spot_arbitrage_payload = {
                        **spot_arbitrage_payload,
                        "status": "disabled",
                    }
                    portfolio_payload = _build_initial_payload(
                        runtime_cfg,
                        poll_seconds,
                    )["portfolio"]

                now = time.monotonic()
                if now >= next_balance_scan:
                    try:
                        account_balances_payload = await fetch_account_balances_payload(
                            runtime_cfg,
                            manager,
                            runtime_slow_execution,
                        )
                    except Exception as exc:  # noqa: BLE001
                        account_balances_payload = {
                            "status": "error",
                            "accounts": [],
                            "totals": [],
                            "checked_account_count": 0,
                            "total_account_count": len(
                                _all_account_exchanges(runtime_cfg)
                            ),
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                        }
                    try:
                        derivatives_payload = await fetch_derivatives_risk_payload(
                            runtime_cfg,
                            manager,
                        )
                    except Exception as exc:  # noqa: BLE001
                        derivatives_payload = {
                            **_build_initial_payload(runtime_cfg, poll_seconds)[
                                "derivatives"
                            ],
                            "status": "error",
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                        }
                    try:
                        funding_basis_payload = await fetch_funding_basis_payload(
                            runtime_cfg,
                            manager,
                            strategy_center_payload=strategy_center_store_payload,
                        )
                    except Exception as exc:  # noqa: BLE001
                        funding_basis_payload = {
                            **_build_initial_payload(runtime_cfg, poll_seconds)[
                                "funding_basis"
                            ],
                            "status": "error",
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                        }
                    try:
                        options_arbitrage_payload = (
                            await fetch_options_arbitrage_payload(
                                runtime_cfg,
                                manager,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        options_arbitrage_payload = {
                            **_build_initial_payload(runtime_cfg, poll_seconds)[
                                "options_arbitrage"
                            ],
                            "status": "error",
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                        }
                    next_balance_scan = now + ACCOUNT_BALANCE_POLL_SECONDS

                if now >= next_order_activity_scan:
                    try:
                        auto_tasks_snapshot = await state.auto_buy_sell_tasks()
                        market_maker_runtime_snapshot = await state.market_maker_runtime()
                        order_activity_payload = await fetch_order_activity_payload(
                            runtime_cfg,
                            manager,
                            runtime_slow_execution,
                            quote_rates=quote_rates,
                            books=portfolio_books,
                            market_maker_runtime=market_maker_runtime_snapshot,
                            auto_buy_sell_tasks=auto_tasks_snapshot,
                        )
                    except Exception as exc:  # noqa: BLE001
                        order_activity_payload = {
                            "status": "error",
                            "accounts": [],
                            "open_orders": [],
                            "closed_orders": [],
                            "recent_trades": [],
                            "pnl_summary": {
                                "currency": runtime_cfg.common_quote_currency,
                                "window": "recent_fills",
                                "trade_count": 0,
                                "attributed_trade_count": 0,
                                "unattributed_trade_count": 0,
                                "total_realized_pnl": 0.0,
                                "total_fees": 0.0,
                                "total_notional": 0.0,
                                "sources": {},
                                "missing_cost_basis": [],
                                "missing_quote_rates": [],
                                "missing_fee_rates": [],
                                "observed_at": None,
                            },
                            "pnl_store": {
                                "enabled": runtime_cfg.pnl_store.enabled,
                                "path": runtime_cfg.pnl_store.path,
                                "stored_fill_count": 0,
                                "daily": None,
                            },
                            "daily_pnl": {
                                "enabled": runtime_cfg.pnl_store.enabled,
                                "path": runtime_cfg.pnl_store.path,
                                "day": None,
                                "currency": runtime_cfg.common_quote_currency,
                                "trade_count": 0,
                                "total_realized_pnl": 0.0,
                                "total_fees": 0.0,
                                "total_notional": 0.0,
                                "sources": {},
                                "updated_at": None,
                            },
                            "open_order_count": 0,
                            "closed_order_count": 0,
                            "recent_trade_count": 0,
                            "reconciliation": {
                                "status": "error",
                                "tracked_order_count": 0,
                                "matched_open_count": 0,
                                "matched_fill_count": 0,
                                "untracked_open_count": 0,
                                "unattributed_fill_count": 0,
                                "issue_count": 1,
                                "notice_count": 0,
                                "total_item_count": 1,
                                "level_counts": {"error": 1, "warning": 0, "info": 0},
                                "critical_issue_count": 1,
                                "auto_stop_recommended": True,
                                "auto_stop_reasons": [
                                    f"order_activity_error: {str(exc)}"
                                ],
                                "issues": [
                                    {
                                        "level": "error",
                                        "type": "order_activity_error",
                                        "strategy": "",
                                        "exchange": "",
                                        "symbol": "",
                                        "order_id": "",
                                        "source_id": "",
                                        "message": str(exc),
                                    }
                                ],
                                "checked_at": time.time(),
                            },
                            "checked_account_count": 0,
                            "total_account_count": len(
                                _all_account_exchanges(runtime_cfg)
                            ),
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                            "warnings": [],
                        }
                    next_order_activity_scan = now + ORDER_ACTIVITY_POLL_SECONDS

                if portfolio_books:
                    portfolio_payload = build_synced_portfolio_pnl(
                        runtime_cfg,
                        portfolio_books,
                        quote_rates,
                        account_balances_payload,
                        order_activity_payload,
                    )

                trading_console_payload = build_trading_console_payload(
                    runtime_cfg,
                    runtime_slow_execution,
                    strategy_paused=strategy_pauses,
                    order_activity=order_activity_payload,
                    auto_buy_sell_tasks=await state.auto_buy_sell_tasks(),
                )

                if runtime_cfg.onchain_monitor.enabled and now >= next_onchain_scan:
                    try:
                        onchain_payload = await fetch_onchain_payload(
                            runtime_cfg,
                            solana_client,
                        )
                    except Exception as exc:  # noqa: BLE001
                        onchain_payload = _onchain_error_payload(
                            runtime_cfg,
                            onchain_payload,
                            exc,
                        )
                    next_onchain_scan = (
                        now + max(1.0, runtime_cfg.onchain_monitor.poll_seconds)
                    )

                warnings = [
                    *warnings,
                    *_global_scan_health_warnings(
                        onchain_payload=onchain_payload,
                        account_balances_payload=account_balances_payload,
                        order_activity_payload=order_activity_payload,
                    ),
                ]
                if derivatives_payload.get("status") == "error":
                    errors = derivatives_payload.get("errors") or ["unavailable"]
                    warnings = [*warnings, f"Derivatives: {errors[0]}"]
                elif derivatives_payload.get("status") == "blocked":
                    reasons = [
                        reason
                        for account in derivatives_payload.get("accounts", [])
                        for reason in account.get("risk_reasons", [])
                    ]
                    warnings = [
                        *warnings,
                        f"Derivatives: {reasons[0] if reasons else 'risk limit breached'}",
                    ]
                reconciliation_payload = (
                    order_activity_payload.get("reconciliation")
                    if isinstance(order_activity_payload.get("reconciliation"), dict)
                    else {}
                )
                reconciliation_stop_requested = bool(
                    reconciliation_payload.get("auto_stop_recommended")
                )
                reconciliation_reasons = [
                    str(reason)
                    for reason in reconciliation_payload.get("auto_stop_reasons", [])
                    if reason
                ]
                program_updated_at = await state.program_updated_at()
                reconciliation_warmup_active = (
                    reconciliation_stop_requested
                    and _monitor_reconciliation_warmup_active(
                        process_uptime_seconds=(
                            monotonic_started - loop_started_monotonic
                        ),
                        program_age_seconds=started_at - program_updated_at,
                    )
                )
                reconciliation_stop = (
                    reconciliation_stop_requested and not reconciliation_warmup_active
                )
                if reconciliation_warmup_active:
                    reconciliation_payload["auto_stop_warmup_active"] = True
                    reconciliation_payload["auto_stop_suppressed"] = True
                    reconciliation_payload["auto_stop_warmup_seconds"] = (
                        RECONCILIATION_AUTO_STOP_WARMUP_SECONDS
                    )
                if reconciliation_stop:
                    warnings = [
                        *warnings,
                        "Reconciliation: " + reconciliation_reasons[0]
                        if reconciliation_reasons
                        else "Reconciliation has critical order state issues",
                    ]
                if market_maker_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Market maker: {market_maker_payload.get('error')}",
                    ]
                if slow_execution_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Auto Buy/Sell: {slow_execution_payload.get('error')}",
                    ]
                if spot_grid_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Spot Grid: {spot_grid_payload.get('error')}",
                    ]
                if dca_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"DCA Bot: {dca_payload.get('error')}",
                    ]
                if execution_algo_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"TWAP/VWAP/POV: {execution_algo_payload.get('error')}",
                    ]
                if backtest_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Backtest: {backtest_payload.get('error')}",
                    ]
                if funding_basis_payload.get("status") == "error":
                    funding_errors = funding_basis_payload.get("errors") or [
                        "unavailable"
                    ]
                    warnings = [
                        *warnings,
                        f"Funding/Basis: {funding_errors[0]}",
                    ]
                if options_arbitrage_payload.get("status") == "error":
                    option_errors = options_arbitrage_payload.get("errors") or [
                        "unavailable"
                    ]
                    warnings = [
                        *warnings,
                        f"Options: {option_errors[0]}",
                    ]
                if spot_arbitrage_payload.get("status") in {
                    "blocked_by_plan",
                    "blocked_by_risk",
                    "blocked_by_slippage",
                    "blocked_by_validation",
                    "blocked_by_balance",
                    "execution_error",
                    "hedge_required",
                }:
                    reason = ""
                    risk_payload = spot_arbitrage_payload.get("risk")
                    if isinstance(risk_payload, dict):
                        reasons = risk_payload.get("reasons")
                        if isinstance(reasons, list) and reasons:
                            reason = str(reasons[0])
                    errors = spot_arbitrage_payload.get("errors")
                    if not reason and isinstance(errors, list) and errors:
                        reason = str(errors[0])
                    if not reason:
                        reason = str(spot_arbitrage_payload.get("status"))
                    warnings = [*warnings, f"Spot arbitrage: {reason}"]
                daily_loss_stop = False
                if runtime_cfg.risk.max_daily_loss_quote > 0:
                    daily_pnl_quote = current_daily_pnl_quote(runtime_cfg)
                    if daily_pnl_quote <= -runtime_cfg.risk.max_daily_loss_quote:
                        daily_loss_stop = True
                        warnings = [
                            *warnings,
                            (
                                f"Daily loss {daily_pnl_quote:.8f} exceeds "
                                f"max_daily_loss_quote {runtime_cfg.risk.max_daily_loss_quote:.8f}"
                            ),
                        ]

                consecutive_problem_cycles = (
                    consecutive_problem_cycles + 1 if warnings else 0
                )
                auto_stop_triggered, auto_stop_reason = _monitor_auto_stop_decision(
                    auto_stop_enabled=runtime_cfg.alerts.auto_stop_enabled,
                    auto_stop_consecutive_errors=(
                        runtime_cfg.alerts.auto_stop_consecutive_errors
                    ),
                    daily_loss_stop=daily_loss_stop,
                    reconciliation_stop=reconciliation_stop,
                    consecutive_problem_cycles=consecutive_problem_cycles,
                )
                if auto_stop_triggered:
                    warnings = [
                        *warnings,
                        (
                            "Auto-stop triggered after "
                            f"{consecutive_problem_cycles} problem cycle(s)"
                        ),
                    ]

                elapsed = time.monotonic() - monotonic_started
                if not await state.is_running():
                    await state.set_paused()
                    continue
                await state.set_scan_result(
                    cfg=runtime_cfg,
                    poll_seconds=poll_seconds,
                    scan_count=scan_count,
                    started_at=started_at,
                    elapsed_ms=int(elapsed * 1000),
                    markets=rows,
                    quote_rates=quote_rates,
                    opportunities=opportunities,
                    warnings=warnings,
                    account_balances=account_balances_payload,
                    derivatives=derivatives_payload,
                    funding_basis=funding_basis_payload,
                    options_arbitrage=options_arbitrage_payload,
                    order_activity=order_activity_payload,
                    onchain=onchain_payload,
                    market_maker=market_maker_payload,
                    slow_execution=slow_execution_payload,
                    spot_grid=spot_grid_payload,
                    dca=dca_payload,
                    execution_algo=execution_algo_payload,
                    backtest=backtest_payload,
                    spot_arbitrage=spot_arbitrage_payload,
                    trading_console=trading_console_payload,
                    portfolio=portfolio_payload,
                )
                if warnings:
                    await alert_service.send(
                        level="critical" if auto_stop_triggered else "warning",
                        title="Crypto arbitrage monitor warning",
                        message="\n".join(warnings[:6]),
                        key="monitor:warnings:" + "|".join(warnings[:3]),
                        payload={
                            "status": "auto_stopped" if auto_stop_triggered else "degraded",
                            "scan_count": scan_count,
                            "warnings": warnings,
                        },
                    )
                due, report_day = _daily_report_due(
                    runtime_cfg,
                    last_report_day=last_daily_report_day,
                )
                if due:
                    auto_tasks = await state.auto_buy_sell_tasks()
                    await alert_service.send(
                        level="info",
                        title="Daily trading report",
                        message=build_daily_report_message(
                            runtime_cfg,
                            scan_count=scan_count,
                            order_activity=order_activity_payload,
                            account_balances=account_balances_payload,
                            trading_console=trading_console_payload,
                            auto_buy_sell_tasks=auto_tasks,
                            warnings=warnings,
                        ),
                        key=f"daily-report:{report_day}",
                        payload={
                            "daily_pnl": order_activity_payload.get("daily_pnl"),
                            "account_balances": account_balances_payload,
                            "auto_buy_sell_tasks": auto_tasks,
                        },
                        force=True,
                    )
                    last_daily_report_day = report_day
                if auto_stop_triggered:
                    await state.set_auto_stopped(
                        reason=auto_stop_reason,
                        warnings=warnings,
                    )
                    write_system_web_audit_event(
                        runtime_cfg,
                        action="auto_stop",
                        target="program",
                        detail=auto_stop_reason,
                        payload={
                            "scan_count": scan_count,
                            "warnings": warnings,
                            "daily_loss_stop": daily_loss_stop,
                            "reconciliation_stop_requested": (
                                reconciliation_stop_requested
                            ),
                            "reconciliation_stop": reconciliation_stop,
                            "reconciliation_warmup_active": (
                                reconciliation_warmup_active
                            ),
                            "reconciliation_reasons": reconciliation_reasons,
                            "consecutive_problem_cycles": consecutive_problem_cycles,
                        },
                    )
                    await alert_service.send(
                        level="critical",
                        title="Crypto arbitrage auto-stopped",
                        message="\n".join(warnings[:8]),
                        key="monitor:auto-stop",
                        payload={
                            "scan_count": scan_count,
                            "warnings": warnings,
                        },
                        force=True,
                    )
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - monotonic_started
                consecutive_problem_cycles += 1
                await state.set_error(
                    cfg=runtime_cfg,
                    poll_seconds=poll_seconds,
                    scan_count=scan_count,
                    started_at=started_at,
                    elapsed_ms=int(elapsed * 1000),
                    error=str(exc),
                )
                await alert_service.send(
                    level="error",
                    title="Crypto arbitrage monitor error",
                    message=f"{exc.__class__.__name__}: {exc}",
                    key=f"monitor:error:{exc.__class__.__name__}:{exc}",
                    payload={
                        "scan_count": scan_count,
                        "error": str(exc),
                    },
                )
                if (
                    runtime_cfg.alerts.auto_stop_enabled
                    and consecutive_problem_cycles
                    >= max(1, runtime_cfg.alerts.auto_stop_consecutive_errors)
                ):
                    auto_stop_reason = (
                        f"monitor exception after {consecutive_problem_cycles} "
                        f"consecutive error cycle(s)"
                    )
                    await state.set_auto_stopped(
                        reason=auto_stop_reason,
                        warnings=[f"{auto_stop_reason}: {exc}"],
                    )
                    write_system_web_audit_event(
                        runtime_cfg,
                        action="auto_stop",
                        target="program",
                        detail=auto_stop_reason,
                        payload={
                            "scan_count": scan_count,
                            "error": str(exc),
                            "consecutive_problem_cycles": consecutive_problem_cycles,
                        },
                    )
                    await alert_service.send(
                        level="critical",
                        title="Crypto arbitrage auto-stopped",
                        message=(
                            f"Stopped after {consecutive_problem_cycles} "
                            f"consecutive error cycle(s): {exc}"
                        ),
                        key="monitor:auto-stop",
                        payload={"scan_count": scan_count, "error": str(exc)},
                        force=True,
                    )

            sleep_for = max(0.0, poll_seconds - (time.monotonic() - monotonic_started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()
        if solana_client is not None:
            await solana_client.close()


async def auto_buy_sell_task_loop(
    cfg: BotConfig,
    state: MonitorState,
    tasks: AutoBuySellTaskService,
) -> None:
    manager = ExchangeManager()
    try:
        await state.set_auto_buy_sell_tasks(await tasks.snapshot())
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            strategy_pauses = await state.strategy_pauses()
            payload = await tasks.run_due_tasks(
                runtime_cfg,
                manager,
                strategy_paused=strategy_pauses.get("slow_execution", False),
                market_maker_paused=strategy_pauses.get("market_maker", False),
                program_running=await state.is_running(),
            )
            await state.set_auto_buy_sell_tasks(payload)
            await asyncio.sleep(1.0)
    finally:
        await manager.close()


def _stat_int(stats: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(stats.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _stat_float_or_none(stats: dict[str, Any], key: str) -> float | None:
    value = stats.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spot_grid_runtime_path(cfg: BotConfig) -> str:
    return cfg.spot_grid.runtime_path or "data/spot_grid_runtime.json"


def _tracked_spot_grid_order_ids(
    tracked_orders: list[TrackedGridOrder],
) -> list[str]:
    return [order.order_id for order in tracked_orders if order.order_id]


def _tracked_spot_grid_orders_for_ids(
    tracked_orders: list[TrackedGridOrder],
    order_ids: list[str],
) -> list[TrackedGridOrder]:
    order_id_set = {str(order_id) for order_id in order_ids if order_id}
    if not order_id_set:
        return []
    return [order for order in tracked_orders if order.order_id in order_id_set]


def _spot_grid_gate_status(
    cfg: BotConfig,
    *,
    strategy_paused: bool,
    program_running: bool,
) -> tuple[bool, str, str]:
    grid_cfg = cfg.spot_grid
    if not grid_cfg.enabled:
        return False, "disabled", "spot_grid.enabled is false"
    if not grid_cfg.exchange:
        return False, "config_error", "spot_grid.exchange is required"
    if not grid_cfg.symbol:
        return False, "config_error", "spot_grid.symbol is required"
    if not grid_cfg.live_enabled:
        return False, "dry_run", "spot_grid.live_enabled is false"
    if not program_running:
        return False, "program_paused", "program is paused"
    if strategy_paused:
        return False, "paused", "spot_grid strategy is paused"
    if not cfg.risk.enabled or not cfg.risk.trading_enabled:
        return False, "blocked_by_risk", "risk trading is disabled"
    if not cfg.risk.allow_live_trading:
        return False, "blocked_by_risk", "risk.allow_live_trading is false"
    if not _risk_strategy_enabled(cfg, "spot_grid"):
        return False, "blocked_by_risk", "spot_grid strategy is disabled"
    if grid_cfg.exchange and not _risk_account_enabled(cfg, grid_cfg.exchange):
        return False, "blocked_by_risk", f"{grid_cfg.exchange} account is disabled"
    return True, "live", "live"


async def _spot_grid_open_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    current_ids: list[str],
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    fallback_ids = sorted({order_id for order_id in current_ids if order_id})
    if not grid_cfg.exchange or not grid_cfg.symbol:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": None,
        }
    exchange = next(
        (
            item
            for item in _all_account_exchanges(cfg)
            if item.key == grid_cfg.exchange
        ),
        None,
    )
    if exchange is None:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"spot grid exchange is not configured: {grid_cfg.exchange}",
        }
    try:
        open_orders = await manager.fetch_open_orders(exchange, symbol=grid_cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    order_ids: set[str] = set()
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        order_id = _raw_order_id(order)
        if order_id:
            order_ids.add(order_id)
    return {
        "source": "exchange",
        "order_ids": sorted(order_ids),
        "open_orders": [order for order in open_orders if isinstance(order, dict)],
        "open_order_count": len(open_orders),
        "error": None,
    }


async def _spot_grid_closed_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
) -> tuple[list[dict[str, Any]], str | None]:
    grid_cfg = cfg.spot_grid
    if not grid_cfg.exchange or not grid_cfg.symbol:
        return [], None
    exchange = next(
        (
            item
            for item in _all_account_exchanges(cfg)
            if item.key == grid_cfg.exchange
        ),
        None,
    )
    if exchange is None:
        return [], f"spot grid exchange is not configured: {grid_cfg.exchange}"
    try:
        closed_orders = await manager.fetch_closed_orders(
            exchange,
            symbol=grid_cfg.symbol,
            limit=100,
        )
    except Exception as exc:  # noqa: BLE001
        return [], f"{exc.__class__.__name__}: {exc}"
    return [order for order in closed_orders if isinstance(order, dict)], None


async def _cancel_tracked_spot_grid_orders(
    cfg: BotConfig,
    manager: ExchangeManager,
    tracked_orders: list[TrackedGridOrder],
) -> tuple[dict[str, Any] | None, list[TrackedGridOrder]]:
    order_ids = _tracked_spot_grid_order_ids(tracked_orders)
    if not order_ids:
        payload = {
            "type": "spot_grid_cancel",
            "strategy": "spot_grid",
            "mode": "live",
            "status": "cancel_skipped",
            "exchange": cfg.spot_grid.exchange,
            "symbol": cfg.spot_grid.symbol,
            "order_ids": [],
            "canceled_count": 0,
            "errors": [
                {
                    "order_id": "tracked_orders",
                    "error": "tracked grid orders have no exchange order ids",
                }
            ],
            "remaining_open_order_ids": [],
        }
        return payload, tracked_orders

    payload = await cancel_spot_grid_order_ids(cfg, manager, order_ids)
    snapshot = await _spot_grid_open_order_snapshot(cfg, manager, order_ids)
    remaining_ids = [
        order_id
        for order_id in snapshot.get("order_ids", [])
        if order_id in set(order_ids)
    ]
    remaining_orders = (
        tracked_orders
        if snapshot.get("error")
        else _tracked_spot_grid_orders_for_ids(tracked_orders, remaining_ids)
    )
    if snapshot.get("error") or remaining_orders:
        payload["status"] = "cancel_retry"
        payload["remaining_open_order_ids"] = remaining_ids or order_ids
        payload["open_order_sync_error"] = snapshot.get("error")
        payload["reason"] = (
            "tracked grid orders must be fully canceled before rebuilding"
        )
    else:
        payload["remaining_open_order_ids"] = []
    return payload, remaining_orders


def _spot_grid_stats(
    *,
    placed_count: int,
    canceled_count: int,
    cycle_count: int,
    last_cancel_at: float | None,
    previous_mid_price: float | None,
) -> dict[str, Any]:
    return {
        "placed_count": placed_count,
        "canceled_count": canceled_count,
        "cycle_count": cycle_count,
        "last_cancel_at": last_cancel_at,
        "previous_mid_price": previous_mid_price,
    }


def _raw_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("order") or "")


def _raw_client_order_id(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    candidates = (
        raw.get("clientOrderId"),
        raw.get("client_order_id"),
        raw.get("clientOid"),
        info.get("clientOrderId"),
        info.get("client_order_id"),
        info.get("client_oid"),
    )
    for value in candidates:
        if value:
            return str(value)
    return ""


async def _market_maker_open_order_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    current_ids: list[str],
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    fallback_ids = sorted({order_id for order_id in current_ids if order_id})
    if not maker_cfg.exchange or not maker_cfg.symbol:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": None,
        }
    exchange = next(
        (
            item
            for item in _all_account_exchanges(cfg)
            if item.key == maker_cfg.exchange
        ),
        None,
    )
    if exchange is None:
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"market maker exchange is not configured: {maker_cfg.exchange}",
        }
    try:
        open_orders = await manager.fetch_open_orders(exchange, symbol=maker_cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        return {
            "source": "memory",
            "order_ids": fallback_ids,
            "open_orders": [],
            "open_order_count": len(fallback_ids),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    order_ids: set[str] = set()
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        order_id = _raw_order_id(order)
        if order_id:
            order_ids.add(order_id)
    return {
        "source": "exchange",
        "order_ids": sorted(order_ids),
        "open_orders": [order for order in open_orders if isinstance(order, dict)],
        "open_order_count": len(open_orders),
        "error": None,
    }


def _market_maker_order_sync_delta(
    previous_order_ids: list[str],
    open_order_snapshot: dict[str, Any],
) -> dict[str, Any]:
    previous_ids = {str(order_id) for order_id in previous_order_ids if order_id}
    snapshot_ids = {
        str(order_id)
        for order_id in open_order_snapshot.get("order_ids", []) or []
        if order_id
    }
    source = str(open_order_snapshot.get("source") or "memory")
    exchange_confirmed = source == "exchange" and not open_order_snapshot.get("error")
    missing_tracked_ids = sorted(previous_ids - snapshot_ids) if exchange_confirmed else []
    new_exchange_ids = sorted(snapshot_ids - previous_ids) if exchange_confirmed else []
    changed = bool(
        exchange_confirmed
        and previous_ids
        and (missing_tracked_ids or new_exchange_ids)
    )
    return {
        "source": source,
        "exchange_confirmed": exchange_confirmed,
        "tracked_before_sync": sorted(previous_ids),
        "exchange_order_ids": sorted(snapshot_ids),
        "missing_tracked_order_ids": missing_tracked_ids,
        "new_exchange_order_ids": new_exchange_ids,
        "changed": changed,
        "checked_at": time.time(),
    }


def _market_maker_force_replace_reason(
    open_order_ids: list[str],
    previous_plan: dict[str, Any] | None,
    *,
    order_sync: dict[str, Any] | None = None,
) -> str | None:
    if order_sync and order_sync.get("changed"):
        return "exchange open orders differ from tracked MM ids; assuming fill/cancel drift"
    if not open_order_ids or not previous_plan:
        return None
    previous_orders = previous_plan.get("orders")
    if not isinstance(previous_orders, list):
        return None
    if len(open_order_ids) != len(previous_orders):
        return "open order count differs from previous MM plan; assuming fill/cancel drift"
    return None


def _market_maker_should_force_replace(
    open_order_ids: list[str],
    previous_plan: dict[str, Any] | None,
    *,
    order_sync: dict[str, Any] | None = None,
) -> bool:
    return (
        _market_maker_force_replace_reason(
            open_order_ids,
            previous_plan,
            order_sync=order_sync,
        )
        is not None
    )



def _market_maker_gate_status(
    cfg: BotConfig,
    *,
    strategy_paused: bool,
    program_running: bool,
) -> tuple[bool, str, str]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.enabled:
        return False, "disabled", "market_maker.enabled is false"
    if not maker_cfg.live_enabled:
        return False, "dry_run", "market_maker.live_enabled is false"
    if not program_running:
        return False, "program_paused", "program is paused"
    if strategy_paused:
        return False, "paused", "market_maker strategy is paused"
    if not cfg.risk.enabled or not cfg.risk.trading_enabled:
        return False, "blocked_by_risk", "risk trading is disabled"
    if not cfg.risk.allow_live_trading:
        return False, "blocked_by_risk", "risk.allow_live_trading is false"
    if not cfg.risk.allow_market_maker or not _risk_strategy_enabled(
        cfg,
        "market_maker",
    ):
        return False, "blocked_by_risk", "market_maker strategy is disabled"
    if maker_cfg.exchange and not _risk_account_enabled(cfg, maker_cfg.exchange):
        return False, "blocked_by_risk", f"{maker_cfg.exchange} account is disabled"
    return True, "live", "live"


def _market_maker_cache_max_age_seconds(cfg: BotConfig) -> float:
    poll_age = max(1.0, cfg.market_maker.poll_seconds * 2)
    risk_age = cfg.risk.max_order_book_age_seconds
    if risk_age > 0:
        return min(risk_age, poll_age)
    return poll_age


async def _cached_market_maker_order_book(
    cfg: BotConfig,
    cache: OrderBookCache,
) -> tuple[OrderBookSnapshot | None, dict[str, Any]]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.exchange or not maker_cfg.symbol:
        return None, {}
    max_age_seconds = _market_maker_cache_max_age_seconds(cfg)
    try:
        exchange_cfg = _find_exchange_by_key(cfg, maker_cfg.exchange)
        depth = max(cfg.order_book_depth, maker_cfg.levels)
        await cache.ensure_watch(exchange_cfg, maker_cfg.symbol, depth)
        snapshot = cache.get(
            maker_cfg.exchange,
            maker_cfg.symbol,
            max_age_seconds=max_age_seconds,
        )
        status = cache.status(
            maker_cfg.exchange,
            maker_cfg.symbol,
            max_age_seconds=max_age_seconds,
        )
        status["using_cached"] = snapshot is not None
        return snapshot, status
    except Exception as exc:  # noqa: BLE001
        return None, {
            "exchange": maker_cfg.exchange,
            "symbol": maker_cfg.symbol,
            "source": None,
            "fresh": False,
            "using_cached": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


async def spot_grid_task_loop(
    cfg: BotConfig,
    state: MonitorState,
) -> None:
    manager = ExchangeManager()
    stored_state = load_spot_grid_runtime_state(_spot_grid_runtime_path(cfg))
    tracked_orders = tracked_orders_from_state(stored_state)
    open_order_exchange = str(stored_state.get("exchange") or "")
    open_order_symbol = str(stored_state.get("symbol") or "")
    stored_stats = (
        stored_state.get("stats") if isinstance(stored_state.get("stats"), dict) else {}
    )
    placed_count = _stat_int(stored_stats, "placed_count")
    canceled_count = _stat_int(stored_stats, "canceled_count")
    cycle_count = _stat_int(stored_stats, "cycle_count")
    last_cancel_at = _stat_float_or_none(stored_stats, "last_cancel_at")
    previous_mid_price = _stat_float_or_none(stored_stats, "previous_mid_price")
    runtime: dict[str, Any] = {
        "status": "starting",
        "mode": "dry_run",
        "open_order_ids": _tracked_spot_grid_order_ids(tracked_orders),
        "open_order_exchange": open_order_exchange,
        "open_order_symbol": open_order_symbol,
        "open_order_count": len(tracked_orders),
        "placed_count": placed_count,
        "canceled_count": canceled_count,
        "cycle_count": cycle_count,
        "runtime_path": _spot_grid_runtime_path(cfg),
        "last_error": None,
        "updated_at": time.time(),
    }
    try:
        await state.set_spot_grid_runtime(runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            grid_cfg = runtime_cfg.spot_grid
            interval = max(1.0, runtime_cfg.poll_seconds)
            started = time.monotonic()
            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            live_allowed, status, reason = _spot_grid_gate_status(
                runtime_cfg,
                strategy_paused=strategy_pauses.get("spot_grid", False),
                program_running=program_running,
            )
            try:
                current_tracking_key = (grid_cfg.exchange, grid_cfg.symbol)
                previous_tracking_key = (open_order_exchange, open_order_symbol)
                if (
                    tracked_orders
                    and previous_tracking_key != ("", "")
                    and previous_tracking_key != current_tracking_key
                ):
                    cancel_cfg = replace(
                        runtime_cfg,
                        spot_grid=replace(
                            runtime_cfg.spot_grid,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                    )
                    cancel_payload, remaining_orders = await _cancel_tracked_spot_grid_orders(
                        cancel_cfg,
                        manager,
                        tracked_orders,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                        last_cancel_at = time.time()
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="spot_grid_task",
                    )
                    tracked_orders = remaining_orders
                    if tracked_orders:
                        runtime = {
                            **runtime,
                            "status": "cancel_retry",
                            "mode": "live" if live_allowed else "dry_run",
                            "reason": (
                                "previous spot grid orders must be canceled "
                                "before switching exchange or symbol"
                            ),
                            "config": spot_grid_config_to_dict(grid_cfg),
                            "open_order_ids": _tracked_spot_grid_order_ids(
                                tracked_orders
                            ),
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(tracked_orders),
                            "last_execution": cancel_payload,
                            "last_error": cancel_payload.get("open_order_sync_error"),
                            "updated_at": time.time(),
                        }
                        await state.set_spot_grid_runtime(runtime)
                        save_spot_grid_runtime_state(
                            _spot_grid_runtime_path(cancel_cfg),
                            runtime_state_from_tracked(
                                tracked_orders,
                                exchange=open_order_exchange,
                                symbol=open_order_symbol,
                                stats=_spot_grid_stats(
                                    placed_count=placed_count,
                                    canceled_count=canceled_count,
                                    cycle_count=cycle_count,
                                    last_cancel_at=last_cancel_at,
                                    previous_mid_price=previous_mid_price,
                                ),
                            ),
                        )
                        sleep_for = max(
                            0.0,
                            interval - (time.monotonic() - started),
                        )
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    open_order_exchange = ""
                    open_order_symbol = ""
                    previous_mid_price = None

                open_order_snapshot: dict[str, Any] = {
                    "source": "memory",
                    "order_ids": _tracked_spot_grid_order_ids(tracked_orders),
                    "open_orders": [],
                    "open_order_count": len(tracked_orders),
                    "error": None,
                }
                order_sync: dict[str, Any] | None = None
                closed_order_error: str | None = None
                open_tracked_orders = list(tracked_orders)
                confirmed_fills = []
                missing_unconfirmed: list[TrackedGridOrder] = []
                if live_allowed or tracked_orders:
                    open_order_snapshot = await _spot_grid_open_order_snapshot(
                        runtime_cfg,
                        manager,
                        _tracked_spot_grid_order_ids(tracked_orders),
                    )
                    if open_order_snapshot.get("error"):
                        runtime = {
                            **runtime,
                            "status": "open_order_sync_error",
                            "mode": "live" if live_allowed else "dry_run",
                            "reason": "could not confirm current spot grid orders",
                            "config": spot_grid_config_to_dict(grid_cfg),
                            "open_order_ids": _tracked_spot_grid_order_ids(
                                tracked_orders
                            ),
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(tracked_orders),
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync_error": open_order_snapshot.get("error"),
                            "last_error": open_order_snapshot.get("error"),
                            "updated_at": time.time(),
                        }
                        await state.set_spot_grid_runtime(runtime)
                        sleep_for = max(
                            0.0,
                            interval - (time.monotonic() - started),
                        )
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    closed_orders, closed_order_error = (
                        await _spot_grid_closed_order_snapshot(runtime_cfg, manager)
                    )
                    if closed_order_error:
                        order_sync = {
                            "tracked_before_count": len(tracked_orders),
                            "exchange_open_count": open_order_snapshot.get(
                                "open_order_count",
                                0,
                            ),
                            "closed_order_error": closed_order_error,
                        }
                    else:
                        order_sync = sync_tracked_grid_orders(
                            tracked_orders,
                            open_order_snapshot.get("open_orders", []),
                            closed_orders,
                        )
                        open_tracked_orders = tracked_orders_from_sync(
                            order_sync,
                            "open_tracked_orders",
                        )
                        confirmed_fills = fills_from_sync(order_sync)
                        missing_unconfirmed = tracked_orders_from_sync(
                            order_sync,
                            "missing_unconfirmed",
                        )
                        tracked_orders = open_tracked_orders
                        if tracked_orders:
                            open_order_exchange = grid_cfg.exchange
                            open_order_symbol = grid_cfg.symbol

                if not live_allowed:
                    cancel_payload = None
                    if tracked_orders:
                        cancel_cfg = runtime_cfg
                        if open_order_exchange and open_order_symbol:
                            cancel_cfg = replace(
                                runtime_cfg,
                                spot_grid=replace(
                                    runtime_cfg.spot_grid,
                                    exchange=open_order_exchange,
                                    symbol=open_order_symbol,
                                ),
                            )
                        cancel_payload, tracked_orders = (
                            await _cancel_tracked_spot_grid_orders(
                                cancel_cfg,
                                manager,
                                tracked_orders,
                            )
                        )
                        canceled_count += int(
                            cancel_payload.get("canceled_count", 0) or 0
                        )
                        if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                            last_cancel_at = time.time()
                        write_trade_event(cancel_cfg.trade_log, cancel_payload)
                        write_strategy_timeline_from_payload(
                            cancel_cfg.strategy_timeline,
                            cancel_payload,
                            source="spot_grid_task",
                        )
                        if not tracked_orders:
                            open_order_exchange = ""
                            open_order_symbol = ""
                    runtime = {
                        "status": (
                            "cancel_retry"
                            if tracked_orders and cancel_payload
                            else status
                        ),
                        "mode": "paused" if status == "paused" else "dry_run",
                        "reason": (
                            cancel_payload.get("reason")
                            if tracked_orders and cancel_payload
                            else reason
                        ),
                        "config": spot_grid_config_to_dict(grid_cfg),
                        "open_order_ids": _tracked_spot_grid_order_ids(
                            tracked_orders
                        ),
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(tracked_orders),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": order_sync,
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "runtime_path": _spot_grid_runtime_path(runtime_cfg),
                        "last_execution": cancel_payload,
                        "last_error": None,
                        "updated_at": time.time(),
                    }
                    await state.set_spot_grid_runtime(runtime)
                    save_spot_grid_runtime_state(
                        _spot_grid_runtime_path(runtime_cfg),
                        runtime_state_from_tracked(
                            tracked_orders,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                            stats=_spot_grid_stats(
                                placed_count=placed_count,
                                canceled_count=canceled_count,
                                cycle_count=cycle_count,
                                last_cancel_at=last_cancel_at,
                                previous_mid_price=previous_mid_price,
                            ),
                        ),
                    )
                else:
                    if closed_order_error:
                        runtime = {
                            **runtime,
                            "status": "closed_order_sync_error",
                            "mode": "live",
                            "reason": "could not confirm recent spot grid fills",
                            "config": spot_grid_config_to_dict(grid_cfg),
                            "open_order_ids": _tracked_spot_grid_order_ids(
                                tracked_orders
                            ),
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(tracked_orders),
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync": order_sync,
                            "last_error": closed_order_error,
                            "updated_at": time.time(),
                        }
                        await state.set_spot_grid_runtime(runtime)
                        sleep_for = max(
                            0.0,
                            interval - (time.monotonic() - started),
                        )
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    cycle_count += 1
                    order_book = await load_spot_grid_order_book(runtime_cfg, manager)
                    preview_payload = await run_spot_grid_cycle(
                        runtime_cfg,
                        manager,
                        live=False,
                        previous_mid_price=previous_mid_price,
                        last_cancel_at=last_cancel_at,
                        order_book=order_book,
                    )
                    plan_payload = (
                        preview_payload.get("plan")
                        if isinstance(preview_payload.get("plan"), dict)
                        else None
                    )
                    if plan_payload and isinstance(
                        plan_payload.get("mid_price"),
                        (int, float),
                    ):
                        previous_mid_price = float(plan_payload["mid_price"])

                    payload: dict[str, Any] | None = None
                    action = "watching"
                    cancel_payload = None
                    if preview_payload.get("status") not in {
                        "planned",
                        "unchanged",
                    }:
                        if tracked_orders:
                            cancel_payload, tracked_orders = (
                                await _cancel_tracked_spot_grid_orders(
                                    runtime_cfg,
                                    manager,
                                    tracked_orders,
                                )
                            )
                            canceled_count += int(
                                cancel_payload.get("canceled_count", 0) or 0
                            )
                            if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                                last_cancel_at = time.time()
                            write_trade_event(runtime_cfg.trade_log, cancel_payload)
                            write_strategy_timeline_from_payload(
                                runtime_cfg.strategy_timeline,
                                cancel_payload,
                                source="spot_grid_task",
                            )
                            if not tracked_orders:
                                open_order_exchange = ""
                                open_order_symbol = ""
                        action = "cancel_out_of_range"
                    elif missing_unconfirmed:
                        if not grid_cfg.auto_rebuild:
                            cancel_payload, tracked_orders = (
                                await _cancel_tracked_spot_grid_orders(
                                    runtime_cfg,
                                    manager,
                                    tracked_orders,
                                )
                            )
                            canceled_count += int(
                                cancel_payload.get("canceled_count", 0) or 0
                            )
                            if int(cancel_payload.get("canceled_count", 0) or 0) > 0:
                                last_cancel_at = time.time()
                            write_trade_event(runtime_cfg.trade_log, cancel_payload)
                            write_strategy_timeline_from_payload(
                                runtime_cfg.strategy_timeline,
                                cancel_payload,
                                source="spot_grid_task",
                            )
                            action = "manual_rebuild_required"
                        else:
                            action = "full_rebuild_after_unknown_order_gap"
                            payload = await run_spot_grid_cycle(
                                runtime_cfg,
                                manager,
                                live=True,
                                replace_order_ids=_tracked_spot_grid_order_ids(
                                    tracked_orders
                                ),
                                previous_mid_price=previous_mid_price,
                                last_cancel_at=last_cancel_at,
                                order_book=order_book,
                            )
                    elif confirmed_fills:
                        action = "replace_filled_orders"
                        payload = await run_spot_grid_cycle(
                            runtime_cfg,
                            manager,
                            live=True,
                            tracked_orders=tracked_orders,
                            replacement_fills=confirmed_fills,
                            previous_mid_price=previous_mid_price,
                            last_cancel_at=last_cancel_at,
                            order_book=order_book,
                        )
                    elif not tracked_orders:
                        action = "place_initial_grid"
                        payload = await run_spot_grid_cycle(
                            runtime_cfg,
                            manager,
                            live=True,
                            previous_mid_price=previous_mid_price,
                            last_cancel_at=last_cancel_at,
                            order_book=order_book,
                        )

                    execution = {}
                    if payload is not None:
                        payload["runtime_strategy"] = "spot_grid"
                        payload["runtime_action"] = action
                        write_trade_event(runtime_cfg.trade_log, payload)
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            payload,
                            source="spot_grid_task",
                        )
                        execution = (
                            payload.get("execution")
                            if isinstance(payload.get("execution"), dict)
                            else {}
                        )
                        placed_count += int(execution.get("placed_count", 0) or 0)
                        canceled_count += int(execution.get("canceled_count", 0) or 0)
                        if int(execution.get("canceled_count", 0) or 0) > 0:
                            last_cancel_at = time.time()
                        placed_orders = [
                            TrackedGridOrder.from_dict(row)
                            for row in execution.get("placed_orders", [])
                            if isinstance(row, dict)
                        ]
                        if payload.get("status") == "placed":
                            if action == "replace_filled_orders":
                                tracked_orders = [*tracked_orders, *placed_orders]
                            else:
                                tracked_orders = placed_orders
                            open_order_exchange = grid_cfg.exchange if tracked_orders else ""
                            open_order_symbol = grid_cfg.symbol if tracked_orders else ""
                        elif payload.get("status") == "cancel_retry":
                            remaining_ids = [
                                str(order_id)
                                for order_id in execution.get(
                                    "remaining_open_order_ids",
                                    [],
                                )
                                if order_id
                            ]
                            tracked_orders = (
                                _tracked_spot_grid_orders_for_ids(
                                    tracked_orders,
                                    remaining_ids,
                                )
                                or tracked_orders
                            )
                        elif payload.get("status") == "execution_error":
                            remaining_ids = [
                                str(order_id)
                                for order_id in execution.get(
                                    "remaining_open_order_ids",
                                    [],
                                )
                                if order_id
                            ]
                            tracked_orders = [
                                *tracked_orders,
                                *_tracked_spot_grid_orders_for_ids(
                                    placed_orders,
                                    remaining_ids,
                                ),
                            ]
                        elif payload.get("status") in {
                            "no_replacement",
                            "unchanged",
                        }:
                            tracked_orders = list(tracked_orders)

                    status_payload = payload or preview_payload
                    if action == "manual_rebuild_required":
                        status_payload = {
                            **preview_payload,
                            "status": "manual_rebuild_required",
                            "reason": (
                                "a tracked grid order disappeared but "
                                "spot_grid.auto_rebuild is false"
                            ),
                            "execution": cancel_payload,
                        }
                    elif cancel_payload is not None and payload is None:
                        status_payload = {
                            **preview_payload,
                            "execution": cancel_payload,
                        }
                    runtime = {
                        "status": status_payload.get("status", "watching"),
                        "mode": "live",
                        "reason": status_payload.get("reason"),
                        "action": action,
                        "config": spot_grid_config_to_dict(grid_cfg),
                        "open_order_ids": _tracked_spot_grid_order_ids(
                            tracked_orders
                        ),
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(tracked_orders),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": order_sync,
                        "confirmed_fill_count": len(confirmed_fills),
                        "missing_unconfirmed_count": len(missing_unconfirmed),
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "runtime_path": _spot_grid_runtime_path(runtime_cfg),
                        "last_plan": status_payload.get("plan"),
                        "last_risk": status_payload.get("risk"),
                        "last_execution": status_payload.get("execution"),
                        "last_error": None,
                        "market_data": status_payload.get("market_data"),
                        "updated_at": time.time(),
                    }
                    await state.set_spot_grid_runtime(runtime)
                    save_spot_grid_runtime_state(
                        _spot_grid_runtime_path(runtime_cfg),
                        runtime_state_from_tracked(
                            tracked_orders,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                            stats=_spot_grid_stats(
                                placed_count=placed_count,
                                canceled_count=canceled_count,
                                cycle_count=cycle_count,
                                last_cancel_at=last_cancel_at,
                                previous_mid_price=previous_mid_price,
                            ),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                runtime = {
                    **runtime,
                    "status": "error",
                    "mode": "live" if live_allowed else "dry_run",
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                    "updated_at": time.time(),
                }
                await state.set_spot_grid_runtime(runtime)

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()


async def _market_maker_instance_task_loop(
    cfg: BotConfig,
    state: MonitorState,
    instance_id: str,
) -> None:
    manager = ExchangeManager()
    orderbook_cache = OrderBookCache(manager)
    open_order_ids: list[str] = []
    open_order_exchange = ""
    open_order_symbol = ""
    placed_count = 0
    canceled_count = 0
    cycle_count = 0
    last_cancel_at: float | None = None
    previous_mid_price: float | None = None
    previous_plan: dict[str, Any] | None = None
    last_maker_cfg = None
    runtime: dict[str, Any] = {
        "id": instance_id,
        "status": "starting",
        "mode": "dry_run",
        "open_order_ids": [],
        "open_order_exchange": "",
        "open_order_symbol": "",
        "open_order_count": 0,
        "placed_count": 0,
        "canceled_count": 0,
        "cycle_count": 0,
        "last_error": None,
        "market_data": None,
        "open_order_sync": None,
        "updated_at": time.time(),
    }
    try:
        await state.set_market_maker_instance_runtime(instance_id, runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            maker_cfg = next(
                (
                    item
                    for item in market_maker_configs_for_runtime(runtime_cfg)
                    if item.id == instance_id
                ),
                None,
            )
            if maker_cfg is None:
                cancel_payload = None
                if open_order_ids and last_maker_cfg is not None:
                    cancel_cfg = replace(
                        runtime_cfg,
                        market_maker=replace(
                            last_maker_cfg,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                        market_makers=[last_maker_cfg],
                    )
                    cancel_payload = await cancel_market_maker_order_ids(
                        cancel_cfg,
                        manager,
                        open_order_ids,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="market_maker_task",
                    )
                    open_order_ids = []
                    open_order_exchange = ""
                    open_order_symbol = ""
                runtime = {
                    **runtime,
                    "id": instance_id,
                    "status": "removed",
                    "mode": "paused",
                    "reason": "market maker instance removed",
                    "open_order_ids": [],
                    "open_order_count": 0,
                    "placed_count": placed_count,
                    "canceled_count": canceled_count,
                    "last_execution": cancel_payload,
                    "updated_at": time.time(),
                }
                await state.set_market_maker_instance_runtime(instance_id, runtime)
                return
            maker_cfg = replace(maker_cfg, id=instance_id)
            last_maker_cfg = maker_cfg
            runtime_cfg = replace(
                runtime_cfg,
                market_maker=maker_cfg,
                market_makers=[maker_cfg],
            )
            interval = max(1.0, maker_cfg.poll_seconds)
            started = time.monotonic()
            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            live_allowed, status, reason = _market_maker_gate_status(
                runtime_cfg,
                strategy_paused=strategy_pauses.get("market_maker", False),
                program_running=program_running,
            )
            try:
                current_tracking_key = (maker_cfg.exchange, maker_cfg.symbol)
                previous_tracking_key = (open_order_exchange, open_order_symbol)
                if (
                    open_order_ids
                    and previous_tracking_key != ("", "")
                    and previous_tracking_key != current_tracking_key
                ):
                    cancel_cfg = replace(
                        runtime_cfg,
                        market_maker=replace(
                            runtime_cfg.market_maker,
                            exchange=open_order_exchange,
                            symbol=open_order_symbol,
                        ),
                    )
                    cancel_payload = await cancel_market_maker_order_ids(
                        cancel_cfg,
                        manager,
                        open_order_ids,
                    )
                    canceled_count += int(cancel_payload.get("canceled_count", 0) or 0)
                    if cancel_payload.get("canceled_count"):
                        last_cancel_at = time.time()
                    write_trade_event(cancel_cfg.trade_log, cancel_payload)
                    write_strategy_timeline_from_payload(
                        cancel_cfg.strategy_timeline,
                        cancel_payload,
                        source="market_maker_task",
                    )
                    open_order_ids = []
                    open_order_exchange = ""
                    open_order_symbol = ""
                    previous_plan = None
                    previous_mid_price = None

                open_order_sync: dict[str, Any] | None = None
                if live_allowed or open_order_ids:
                    tracked_before_sync = list(open_order_ids)
                    open_order_snapshot = await _market_maker_open_order_snapshot(
                        runtime_cfg,
                        manager,
                        open_order_ids,
                    )
                    open_order_sync = _market_maker_order_sync_delta(
                        tracked_before_sync,
                        open_order_snapshot,
                    )
                    open_order_ids = [
                        str(order_id)
                        for order_id in open_order_snapshot.get("order_ids", [])
                        if order_id
                    ]
                    if open_order_ids:
                        open_order_exchange = maker_cfg.exchange
                        open_order_symbol = maker_cfg.symbol
                else:
                    open_order_snapshot = {
                        "source": "memory",
                        "order_ids": open_order_ids,
                        "open_orders": [],
                        "open_order_count": len(open_order_ids),
                        "error": None,
                    }
                    open_order_sync = _market_maker_order_sync_delta(
                        open_order_ids,
                        open_order_snapshot,
                    )
                if not live_allowed:
                    cancel_payload = None
                    if open_order_ids:
                        cancel_payload = await cancel_market_maker_order_ids(
                            runtime_cfg,
                            manager,
                            open_order_ids,
                        )
                        canceled_count += int(
                            cancel_payload.get("canceled_count", 0) or 0
                        )
                        if cancel_payload.get("canceled_count"):
                            last_cancel_at = time.time()
                        write_trade_event(runtime_cfg.trade_log, cancel_payload)
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            cancel_payload,
                            source="market_maker_task",
                        )
                        open_order_ids = []
                        open_order_exchange = ""
                        open_order_symbol = ""
                    runtime = {
                        "status": status,
                        "mode": "paused" if status == "paused" else "dry_run",
                        "reason": reason,
                        "config": market_maker_config_to_dict(maker_cfg),
                        "open_order_ids": open_order_ids,
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(open_order_ids),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": open_order_sync,
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "last_error": None,
                        "last_execution": cancel_payload,
                        "market_data": None,
                        "updated_at": time.time(),
                    }
                    await state.set_market_maker_instance_runtime(instance_id, runtime)
                else:
                    if open_order_snapshot.get("error"):
                        cycle_count += 1
                        runtime = {
                            "status": "open_order_sync_error",
                            "mode": "live",
                            "reason": "could not confirm current open orders",
                            "config": market_maker_config_to_dict(maker_cfg),
                            "open_order_ids": open_order_ids,
                            "open_order_exchange": open_order_exchange,
                            "open_order_symbol": open_order_symbol,
                            "open_order_count": len(open_order_ids),
                            "open_order_source": open_order_snapshot.get("source"),
                            "open_order_sync_error": open_order_snapshot.get("error"),
                            "open_order_sync": open_order_sync,
                            "placed_count": placed_count,
                            "canceled_count": canceled_count,
                            "cycle_count": cycle_count,
                            "last_error": open_order_snapshot.get("error"),
                            "market_data": None,
                            "updated_at": time.time(),
                        }
                        await state.set_market_maker_instance_runtime(instance_id, runtime)
                        sleep_for = max(0.0, interval - (time.monotonic() - started))
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)
                        continue
                    cycle_count += 1
                    order_book, market_data_status = (
                        await _cached_market_maker_order_book(
                            runtime_cfg,
                            orderbook_cache,
                        )
                    )
                    previous_plan_for_cycle = previous_plan
                    force_replace_reason = _market_maker_force_replace_reason(
                        open_order_ids,
                        previous_plan,
                        order_sync=open_order_sync,
                    )
                    force_replace = force_replace_reason is not None
                    if force_replace:
                        previous_plan_for_cycle = None
                    portfolio_snapshot = await state.portfolio_payload()
                    inventory_base = _portfolio_position_for_symbol(
                        portfolio_snapshot,
                        maker_cfg.symbol,
                        cfg=runtime_cfg,
                    )
                    # When force-replacing (fills detected) don't pass open_orders:
                    # _previous_plan_from_open_orders would otherwise reconstruct a
                    # "previous plan" from whatever orders remain, which can trick
                    # the reprice-threshold check into returning "unchanged" and
                    # skipping the full-grid rebuild we explicitly want.
                    existing_open_orders_for_cycle = (
                        None if force_replace else open_order_snapshot.get("open_orders")
                    )
                    payload = await run_market_maker_cycle(
                        runtime_cfg,
                        manager,
                        live=True,
                        replace_existing=False,
                        replace_order_ids=open_order_ids,
                        previous_plan=previous_plan_for_cycle,
                        existing_open_orders=existing_open_orders_for_cycle,
                        previous_mid_price=previous_mid_price,
                        last_cancel_at=last_cancel_at,
                        order_book=order_book,
                        inventory_base=inventory_base,
                    )
                    if force_replace:
                        payload["force_replace_reason"] = force_replace_reason
                    market_data = (
                        payload.get("market_data")
                        if isinstance(payload.get("market_data"), dict)
                        else {}
                    )
                    if market_data_status:
                        market_data = {
                            **market_data,
                            "cache": market_data_status,
                        }
                    payload["market_data"] = market_data
                    payload["runtime_strategy"] = "market_maker"
                    write_trade_event(runtime_cfg.trade_log, payload)
                    write_strategy_timeline_from_payload(
                        runtime_cfg.strategy_timeline,
                        payload,
                        source="market_maker_task",
                    )
                    plan_payload = (
                        payload.get("plan")
                        if isinstance(payload.get("plan"), dict)
                        else None
                    )
                    if plan_payload and isinstance(
                        plan_payload.get("mid_price"),
                        (int, float),
                    ) and payload.get("status") in {"placed", "unchanged"}:
                        previous_plan = plan_payload
                        previous_mid_price = float(plan_payload["mid_price"])
                    execution = (
                        payload.get("execution")
                        if isinstance(payload.get("execution"), dict)
                        else {}
                    )
                    placed_count += int(execution.get("placed_count", 0) or 0)
                    canceled_count += int(execution.get("canceled_count", 0) or 0)
                    if int(execution.get("canceled_count", 0) or 0) > 0:
                        last_cancel_at = time.time()
                    open_order_ids = [
                        str(order_id)
                        for order_id in execution.get("placed_order_ids", [])
                        if order_id
                    ] or [
                        str(order_id)
                        for order_id in execution.get("remaining_open_order_ids", [])
                        if order_id
                    ] or open_order_ids
                    if open_order_ids:
                        open_order_exchange = maker_cfg.exchange
                        open_order_symbol = maker_cfg.symbol
                    elif payload.get("status") == "placed":
                        open_order_exchange = ""
                        open_order_symbol = ""
                    runtime = {
                        "status": payload.get("status", "unknown"),
                        "mode": "live",
                        "reason": None,
                        "config": market_maker_config_to_dict(maker_cfg),
                        "open_order_ids": open_order_ids,
                        "open_order_exchange": open_order_exchange,
                        "open_order_symbol": open_order_symbol,
                        "open_order_count": len(open_order_ids),
                        "open_order_source": open_order_snapshot.get("source"),
                        "open_order_sync_error": open_order_snapshot.get("error"),
                        "open_order_sync": open_order_sync,
                        "force_replace": force_replace,
                        "force_replace_reason": force_replace_reason,
                        "placed_count": placed_count,
                        "canceled_count": canceled_count,
                        "cycle_count": cycle_count,
                        "last_plan": payload.get("plan"),
                        "last_risk": payload.get("risk"),
                        "last_execution": execution,
                        "last_error": None,
                        "market_data": payload.get("market_data"),
                        "updated_at": time.time(),
                    }
                    await state.set_market_maker_instance_runtime(instance_id, runtime)
            except Exception as exc:  # noqa: BLE001
                runtime = {
                    **runtime,
                    "status": "error",
                    "mode": "live" if live_allowed else "dry_run",
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                    "updated_at": time.time(),
                }
                await state.set_market_maker_instance_runtime(instance_id, runtime)

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await orderbook_cache.close()
        await manager.close()


async def market_maker_task_loop(
    cfg: BotConfig,
    state: MonitorState,
) -> None:
    tasks: dict[str, asyncio.Task[None]] = {}
    await state.set_market_maker_runtime(
        {
            "status": "starting",
            "mode": "dry_run",
            "instances": [],
            "instance_count": 0,
            "active_instance_count": 0,
            "open_order_count": 0,
            "placed_count": 0,
            "canceled_count": 0,
            "cycle_count": 0,
            "updated_at": time.time(),
        }
    )
    try:
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            maker_configs = market_maker_configs_for_runtime(runtime_cfg)
            configured_ids = {maker_cfg.id for maker_cfg in maker_configs}

            for instance_id, task in list(tasks.items()):
                if not task.done():
                    continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await state.set_market_maker_instance_runtime(
                        instance_id,
                        {
                            "id": instance_id,
                            "status": "error",
                            "mode": "dry_run",
                            "last_error": f"{exc.__class__.__name__}: {exc}",
                            "open_order_ids": [],
                            "open_order_count": 0,
                            "updated_at": time.time(),
                        },
                    )
                del tasks[instance_id]

            for maker_cfg in maker_configs:
                if maker_cfg.id in tasks:
                    continue
                tasks[maker_cfg.id] = asyncio.create_task(
                    _market_maker_instance_task_loop(cfg, state, maker_cfg.id)
                )

            for instance_id in list(tasks):
                if instance_id not in configured_ids and tasks[instance_id].done():
                    del tasks[instance_id]

            await asyncio.sleep(1.0)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)


__all__ = [
    "_daily_report_due",
    "_market_maker_force_replace_reason",
    "_market_maker_order_sync_delta",
    "auto_buy_sell_task_loop",
    "build_daily_report_message",
    "market_maker_task_loop",
    "monitor_loop",
    "spot_grid_task_loop",
]
