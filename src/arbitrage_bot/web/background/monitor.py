from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from ..market_maker_alerts import market_maker_problem_warnings
from ..state import MonitorState

from ...alerts import AlertService
from ...asset_ledger import AssetLedgerStore, attach_ledger_checkpoint
from ...config import BotConfig
from ...exchanges import ExchangeManager
from ...main import (
    StrategyName,
    _quote_rates_from_sources,
    _symbols_for_configured_spot_markets,
    _symbols_for_triangular_routes,
    scan_with_manager,
)
from ...models import OrderBookSnapshot
from ...order_reconciliation import (
    RECONCILIATION_AUTO_STOP_WARMUP_SECONDS,
    _monitor_auto_stop_decision,
    _monitor_reconciliation_streak,
    _monitor_reconciliation_warmup_active,
)
from ...pnl import build_portfolio_pnl
from ...portfolio_metrics import (
    build_synced_portfolio_pnl,
)
from ...risk import current_daily_pnl_quote
from ...solana import SolanaTokenClient
from ...spot_arbitrage_executor import run_spot_arbitrage_execution_cycle
from ...strategy_center import StrategyCenterStore
from ...strategy_timeline import (
    strategy_timeline_event_from_payload,
    strategy_timeline_fingerprint,
    write_strategy_timeline_from_payload,
)
from ...strategies.spot_spread import find_converted_spot_spread_opportunities
from ...strategies.triangular import find_triangular_arbitrage_opportunities
from ...trade_log import write_trade_event
from ...web_config import (
    _auto_buy_sell_symbols_by_exchange,
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    backtest_config_to_dict,
    auto_buy_sell_exchanges,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_grid_config_to_dict,
)
from ..constants import (
    ACCOUNT_BALANCE_POLL_SECONDS,
    ORDER_ACTIVITY_POLL_SECONDS,
    SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
)
from ..security import write_system_web_audit_event
from ..core import (
    _all_account_exchanges,
    _build_initial_payload,
    _cached_onchain_payload,
    _global_scan_health_warnings,
    _missing_market_warnings,
    _onchain_error_payload,
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
)


_ASSET_CHECKPOINT_WRITTEN_AT: dict[str, float] = {}


def _checkpoint_asset_state(
    cfg: BotConfig,
    account_balances: dict[str, Any],
    order_activity: dict[str, Any],
    portfolio: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = time.time()
    ledger_key = str(Path(cfg.asset_ledger.path).expanduser().resolve())
    last_written = _ASSET_CHECKPOINT_WRITTEN_AT.get(ledger_key, 0.0)
    if now - last_written < cfg.asset_ledger.checkpoint_interval_seconds:
        if portfolio is not None and cfg.asset_ledger.enabled:
            try:
                AssetLedgerStore(cfg.asset_ledger).apply_portfolio_performance(
                    portfolio,
                    account_balances,
                    scope_key="platform",
                    observed_at=now,
                )
            except Exception:  # noqa: BLE001
                # A ledger refresh must not interrupt the monitor loop. The next
                # cycle or checkpoint will retry while the browser retains the
                # last reliable value.
                pass
        return account_balances, order_activity
    try:
        balances, activity, _ = attach_ledger_checkpoint(
            cfg.asset_ledger,
            account_balances,
            order_activity,
            portfolio=portfolio,
        )
        _ASSET_CHECKPOINT_WRITTEN_AT[ledger_key] = now
        return balances, activity
    except Exception as exc:  # noqa: BLE001
        if portfolio is not None and cfg.asset_ledger.enabled:
            try:
                AssetLedgerStore(cfg.asset_ledger).apply_portfolio_performance(
                    portfolio,
                    account_balances,
                    scope_key="platform",
                    observed_at=now,
                )
            except Exception:  # noqa: BLE001
                pass
        ledger_error = {
            "enabled": cfg.asset_ledger.enabled,
            "status": "error",
            "path": cfg.asset_ledger.path,
            "error": f"{exc.__class__.__name__}: {exc}",
            "checked_at": time.time(),
        }
        return (
            {**account_balances, "ledger": ledger_error},
            {**order_activity, "ledger": ledger_error},
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


async def _refresh_uncertain_order_intents(
    cfg: BotConfig,
    manager: ExchangeManager,
    state: MonitorState,
) -> dict[str, Any] | None:
    summary = manager.order_reliability_summary()
    if int(summary.get("pending_count") or 0) <= 0:
        return None
    recovery = await manager.recover_pending_order_intents(
        _all_account_exchanges(cfg),
        resolve_confirmed_absent=True,
    )
    await state.set_order_reliability(recovery)
    return recovery


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
    initial_payload = _build_initial_payload(cfg, poll_seconds)
    onchain_payload = _cached_onchain_payload(cfg) or initial_payload["onchain"]
    account_balances_payload = initial_payload["account_balances"]
    derivatives_payload = initial_payload["derivatives"]
    funding_basis_payload = initial_payload["funding_basis"]
    options_arbitrage_payload = initial_payload["options_arbitrage"]
    order_activity_payload = initial_payload["order_activity"]
    trading_console_payload = initial_payload["trading_console"]
    market_maker_payload = initial_payload["market_maker"]
    slow_execution_payload = initial_payload["slow_execution"]
    spot_grid_payload = initial_payload["spot_grid"]
    dca_payload = initial_payload["dca"]
    execution_algo_payload = initial_payload["execution_algo"]
    backtest_payload = initial_payload["backtest"]
    spot_arbitrage_payload = initial_payload["spot_arbitrage"]
    portfolio_payload = initial_payload["portfolio"]
    alert_service = AlertService(cfg.alerts)
    next_onchain_scan = 0.0
    next_balance_scan = 0.0
    next_order_activity_scan = 0.0
    consecutive_reconciliation_cycles = 0
    reconciliation_stop_fingerprint = ""
    reconciliation_stop_observation = ""
    consecutive_monitor_exception_cycles = 0
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
                            account_balances_payload = (
                                await fetch_account_balances_payload(
                                    runtime_cfg,
                                    manager,
                                    runtime_slow_execution,
                                )
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
                            await _refresh_uncertain_order_intents(
                                runtime_cfg,
                                manager,
                                state,
                            )
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
                                "critical_issue_count": 0,
                                "auto_stop_recommended": False,
                                "auto_stop_reasons": [],
                                "recoverable_issue_count": 1,
                                "automatic_retry_active": True,
                                "recoverable_reasons": [
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
                        next_order_activity_scan = now + ORDER_ACTIVITY_POLL_SECONDS
                    account_balances_payload, order_activity_payload = (
                        _checkpoint_asset_state(
                            runtime_cfg,
                            account_balances_payload,
                            order_activity_payload,
                            portfolio_payload,
                        )
                    )
                    if account_balances_payload.get("status") == "error":
                        errors = account_balances_payload.get("errors") or [
                            "unavailable"
                        ]
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
                    derivative_targets: dict[str, set[str]] = {}
                    if (
                        runtime_slow_execution.enabled
                        and runtime_slow_execution.instrument_type == "perpetual"
                        and runtime_slow_execution.exchange in derivative_keys
                        and runtime_slow_execution.symbol
                    ):
                        derivative_targets.setdefault(
                            runtime_slow_execution.exchange,
                            set(),
                        ).add(runtime_slow_execution.symbol)
                    if (
                        runtime_cfg.market_maker.enabled
                        and runtime_cfg.market_maker.exchange in derivative_keys
                        and runtime_cfg.market_maker.symbol
                    ):
                        derivative_targets.setdefault(
                            runtime_cfg.market_maker.exchange,
                            set(),
                        ).add(runtime_cfg.market_maker.symbol)
                    if derivative_targets:
                        derivative_books = await manager.fetch_order_books(
                            runtime_cfg.derivative_exchanges,
                            derivative_targets,
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
                                spot_markets=runtime_cfg.spot_markets,
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
                                spot_markets=runtime_cfg.spot_markets,
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
                                spot_markets=runtime_cfg.spot_markets,
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
                                spot_markets=runtime_cfg.spot_markets,
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
                                auto_buy_sell_exchanges(runtime_cfg),
                                _auto_buy_sell_symbols_by_exchange(runtime_cfg),
                                spot_markets=runtime_cfg.spot_markets,
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
                            spot_arbitrage_payload = (
                                await run_spot_arbitrage_execution_cycle(
                                    runtime_cfg,
                                    manager,
                                    opportunities=opportunities,
                                    books=books,
                                    quote_rates=quote_rates,
                                    live=True,
                                )
                            )
                            write_trade_event(
                                runtime_cfg.trade_log, spot_arbitrage_payload
                            )
                            if spot_arbitrage_payload.get("status") != "no_opportunity":
                                last_spot_arbitrage_execution_at = time.monotonic()
                    timeline_event = strategy_timeline_event_from_payload(
                        spot_arbitrage_payload,
                        source="monitor",
                    )
                    timeline_fingerprint = strategy_timeline_fingerprint(timeline_event)
                    if timeline_fingerprint != last_spot_arbitrage_timeline_fingerprint:
                        write_strategy_timeline_from_payload(
                            runtime_cfg.strategy_timeline,
                            spot_arbitrage_payload,
                            source="monitor",
                        )
                        last_spot_arbitrage_timeline_fingerprint = timeline_fingerprint
                else:
                    opportunities = await scan_with_manager(
                        runtime_cfg,
                        strategy,
                        manager,
                    )
                    rows = []
                    quote_rates = runtime_cfg.quote_rates
                    warnings = []
                    market_maker_payload = build_market_maker_payload(
                        runtime_cfg,
                        {},
                        base_cfg=cfg,
                    )
                    slow_execution_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": slow_execution_config_to_dict(runtime_slow_execution),
                        "accounts": slow_execution_accounts(
                            auto_buy_sell_exchanges(runtime_cfg),
                            _auto_buy_sell_symbols_by_exchange(runtime_cfg),
                            spot_markets=runtime_cfg.spot_markets,
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
                            spot_markets=runtime_cfg.spot_markets,
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
                            spot_markets=runtime_cfg.spot_markets,
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
                            spot_markets=runtime_cfg.spot_markets,
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
                            spot_markets=runtime_cfg.spot_markets,
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
                        await _refresh_uncertain_order_intents(
                            runtime_cfg,
                            manager,
                            state,
                        )
                        auto_tasks_snapshot = await state.auto_buy_sell_tasks()
                        market_maker_runtime_snapshot = (
                            await state.market_maker_runtime()
                        )
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
                                "critical_issue_count": 0,
                                "auto_stop_recommended": False,
                                "auto_stop_reasons": [],
                                "recoverable_issue_count": 1,
                                "automatic_retry_active": True,
                                "recoverable_reasons": [
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
                account_balances_payload, order_activity_payload = (
                    _checkpoint_asset_state(
                        runtime_cfg,
                        account_balances_payload,
                        order_activity_payload,
                        portfolio_payload,
                    )
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
                    next_onchain_scan = now + max(
                        1.0, runtime_cfg.onchain_monitor.poll_seconds
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
                market_maker_runtime_warnings = market_maker_problem_warnings(
                    await state.market_maker_runtime()
                )
                if market_maker_runtime_warnings:
                    warnings = [*warnings, *market_maker_runtime_warnings[:3]]
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

                (
                    consecutive_reconciliation_cycles,
                    reconciliation_stop_fingerprint,
                    reconciliation_stop_observation,
                ) = _monitor_reconciliation_streak(
                    current_count=consecutive_reconciliation_cycles,
                    previous_fingerprint=reconciliation_stop_fingerprint,
                    previous_observation=reconciliation_stop_observation,
                    reconciliation_stop=reconciliation_stop,
                    reasons=reconciliation_reasons,
                    observation=reconciliation_payload.get("checked_at"),
                )
                consecutive_monitor_exception_cycles = 0
                auto_stop_triggered, auto_stop_reason = _monitor_auto_stop_decision(
                    auto_stop_enabled=runtime_cfg.alerts.auto_stop_enabled,
                    auto_stop_consecutive_errors=(
                        runtime_cfg.alerts.auto_stop_consecutive_errors
                    ),
                    daily_loss_stop=daily_loss_stop,
                    reconciliation_stop=reconciliation_stop,
                    consecutive_problem_cycles=consecutive_reconciliation_cycles,
                )
                if auto_stop_triggered:
                    warnings = [
                        *warnings,
                        (
                            "Auto-stop triggered after "
                            f"{consecutive_reconciliation_cycles} distinct "
                            "reconciliation observation(s)"
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
                            "status": "auto_stopped"
                            if auto_stop_triggered
                            else "degraded",
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
                            "consecutive_reconciliation_cycles": (
                                consecutive_reconciliation_cycles
                            ),
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
                consecutive_monitor_exception_cycles += 1
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
                    and consecutive_monitor_exception_cycles
                    >= max(1, runtime_cfg.alerts.auto_stop_consecutive_errors)
                ):
                    auto_stop_reason = (
                        "monitor exception after "
                        f"{consecutive_monitor_exception_cycles} "
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
                            "consecutive_monitor_exception_cycles": (
                                consecutive_monitor_exception_cycles
                            ),
                        },
                    )
                    await alert_service.send(
                        level="critical",
                        title="Crypto arbitrage auto-stopped",
                        message=(
                            f"Stopped after {consecutive_monitor_exception_cycles} "
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
