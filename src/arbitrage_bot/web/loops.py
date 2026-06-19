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
from ..strategy_timeline import (
    strategy_timeline_event_from_payload,
    strategy_timeline_fingerprint,
    write_strategy_timeline_from_payload,
)
from ..strategies.spot_spread import find_converted_spot_spread_opportunities
from ..trade_log import write_trade_event
from ..web_config import (
    _grid_symbols_by_exchange,
    _spot_symbols_by_exchange,
    dca_config_to_dict,
    market_maker_config_to_dict,
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
    build_dca_payload,
    build_slow_execution_payload,
    build_spot_grid_payload,
    build_trading_console_payload,
    fetch_account_balances_payload,
    fetch_onchain_payload,
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
                    if order_activity_payload.get("status") == "error":
                        errors = order_activity_payload.get("errors") or ["unavailable"]
                        readonly_warnings.append(f"Orders: {errors[0]}")
                    await state.set_readonly_health(
                        cfg=runtime_cfg,
                        exec_cfg=runtime_slow_execution,
                        account_balances=account_balances_payload,
                        order_activity=order_activity_payload,
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
                    warnings = [*_missing_market_warnings(rows), *extra_warnings]
                    if strategy_pauses.get("market_maker", False):
                        market_maker_payload = {
                            "status": "paused",
                            "mode": "paused",
                            "plan": None,
                            "error": None,
                        }
                    else:
                        market_maker_payload = build_market_maker_payload(
                            runtime_cfg,
                            books,
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
                    order_activity=order_activity_payload,
                    onchain=onchain_payload,
                    market_maker=market_maker_payload,
                    slow_execution=slow_execution_payload,
                    spot_grid=spot_grid_payload,
                    dca=dca_payload,
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
                program_running=await state.is_running(),
            )
            await state.set_auto_buy_sell_tasks(payload)
            await asyncio.sleep(1.0)
    finally:
        await manager.close()


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


async def market_maker_task_loop(
    cfg: BotConfig,
    state: MonitorState,
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
    runtime: dict[str, Any] = {
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
        await state.set_market_maker_runtime(runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            maker_cfg = runtime_cfg.market_maker
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
                    await state.set_market_maker_runtime(runtime)
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
                        await state.set_market_maker_runtime(runtime)
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
                    payload = await run_market_maker_cycle(
                        runtime_cfg,
                        manager,
                        live=True,
                        replace_existing=False,
                        replace_order_ids=open_order_ids,
                        previous_plan=previous_plan_for_cycle,
                        existing_open_orders=open_order_snapshot.get("open_orders"),
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
                    await state.set_market_maker_runtime(runtime)
            except Exception as exc:  # noqa: BLE001
                runtime = {
                    **runtime,
                    "status": "error",
                    "mode": "live" if live_allowed else "dry_run",
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                    "updated_at": time.time(),
                }
                await state.set_market_maker_runtime(runtime)

            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await orderbook_cache.close()
        await manager.close()


__all__ = [
    "_daily_report_due",
    "_market_maker_force_replace_reason",
    "_market_maker_order_sync_delta",
    "auto_buy_sell_task_loop",
    "build_daily_report_message",
    "market_maker_task_loop",
    "monitor_loop",
]
