from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any


from ..constants import (
    SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
    STRATEGY_IDS,
)


from ...auto_buy_sell_task import (
    default_task_store_path,
)
from ...backtesting import run_paper_backtest
from ...config import (
    BotConfig,
    MarketMakerConfig,
    SlowExecutionConfig,
)
from ...contract_strategies import build_contract_strategies_payload
from ...exchanges import limit_order_features
from ...execution_algos import build_execution_algo_plan
from ...grid_trading import build_dca_plan, build_spot_grid_plan
from ...market_making import MarketMakerPlan, build_symmetric_market_maker_plan
from ...market_maker import (
    market_maker_quote_conversion,
    market_maker_risk_config,
    order_book_market_data,
)
from ...models import OrderBookSnapshot
from ...portfolio_metrics import (
    _base_currency_from_symbol,
)
from ...risk import (
    RiskMarketContext,
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)
from ...slow_execution import build_slow_execution_plan
from ...solana import (
    SolanaTokenClient,
    fetch_top_token_owners,
    load_cached_holder_snapshot,
    update_holder_history,
)
from ...web_config import (
    _auto_buy_sell_symbols_by_exchange,
    _cash_and_carry_pairs_from_payload,
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    _market_maker_symbols_by_exchange,
    _rebalance_symbols_by_exchange,
    _spot_markets_from_payload,
    backtest_config_to_dict,
    auto_buy_sell_exchanges,
    cash_and_carry_pairs_to_list,
    contract_strategies_config_to_dict,
    cross_exchange_rebalance_config_to_dict,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    exchange_configs_to_list,
    market_maker_config_to_dict,
    market_maker_configs_for_runtime,
    market_maker_configs_from_payload,
    market_maker_configs_to_list,
    market_maker_config_with_id,
    market_maker_symbols_for_accounts,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_grid_config_to_dict,
    spot_markets_to_list,
    strategy_universe_to_dict,
)


from .dashboard import (
    _dedupe_readiness_messages,
    build_operations_payload,
    build_readiness_payload,
    build_trading_console_payload,
)
from .exchange_data import _all_account_exchanges
from .workspace import build_strategy_center_payload

def _dataclass_overrides(raw: Any, model: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = {field.name for field in fields(model)}
    return {key: value for key, value in raw.items() if key in allowed}

def _load_runtime_overrides(path: Path, cfg: BotConfig) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"loaded": False, "path": str(path), "data": {}}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "loaded": False,
            "path": str(path),
            "error": f"{exc.__class__.__name__}: {exc}",
            "data": {},
        }
    if not isinstance(raw, dict):
        return {
            "loaded": False,
            "path": str(path),
            "error": "runtime override store must be a JSON object",
            "data": {},
        }

    data: dict[str, Any] = {
        "risk_overrides": _dataclass_overrides(
            raw.get("risk_overrides"),
            cfg.risk,
        ),
        "market_maker_overrides": _dataclass_overrides(
            raw.get("market_maker_overrides"),
            cfg.market_maker,
        ),
        "slow_execution_overrides": _dataclass_overrides(
            raw.get("slow_execution_overrides"),
            cfg.slow_execution,
        ),
        "cross_exchange_rebalance_overrides": _dataclass_overrides(
            raw.get("cross_exchange_rebalance_overrides"),
            cfg.cross_exchange_rebalance,
        ),
        "spot_grid_overrides": _dataclass_overrides(
            raw.get("spot_grid_overrides"),
            cfg.spot_grid,
        ),
        "dca_overrides": _dataclass_overrides(
            raw.get("dca_overrides"),
            cfg.dca,
        ),
        "execution_algo_overrides": _dataclass_overrides(
            raw.get("execution_algo_overrides"),
            cfg.execution_algo,
        ),
        "backtest_overrides": _dataclass_overrides(
            raw.get("backtest_overrides"),
            cfg.backtest,
        ),
        "strategy_paused": {
            key: bool(value)
            for key, value in (raw.get("strategy_paused") or {}).items()
            if key in STRATEGY_IDS
        },
    }
    if raw.get("market_maker_instances") is not None:
        try:
            data["market_maker_instances"] = market_maker_configs_to_list(
                market_maker_configs_from_payload(
                    raw.get("market_maker_instances"),
                    base_configs=market_maker_configs_for_runtime(cfg),
                )
            )
        except (TypeError, ValueError) as exc:
            return {
                "loaded": False,
                "path": str(path),
                "error": f"invalid market_maker_instances in runtime store: {exc}",
                "data": {},
            }
    program = raw.get("program")
    if isinstance(program, dict):
        program_state: dict[str, Any] = {}
        if isinstance(program.get("running"), bool):
            program_state["running"] = program["running"]
        if isinstance(program.get("auto_stopped"), bool):
            program_state["auto_stopped"] = program["auto_stopped"]
        if isinstance(program.get("updated_at"), (int, float)):
            program_state["updated_at"] = float(program["updated_at"])
        if isinstance(program.get("stopped_at"), (int, float)):
            program_state["stopped_at"] = float(program["stopped_at"])
        if program.get("stop_reason") is None or isinstance(
            program.get("stop_reason"),
            str,
        ):
            program_state["stop_reason"] = program.get("stop_reason")
        if program_state:
            data["program"] = program_state

    allowed_spot_exchanges = {exchange.key for exchange in cfg.spot_exchanges}
    if raw.get("spot_markets") is not None:
        try:
            data["spot_markets"] = spot_markets_to_list(
                _spot_markets_from_payload(
                    {"spot_markets": raw.get("spot_markets")},
                    allowed_exchanges=allowed_spot_exchanges,
                )
            )
        except (TypeError, ValueError) as exc:
            return {
                "loaded": False,
                "path": str(path),
                "error": f"invalid spot_markets in runtime store: {exc}",
                "data": {},
            }
    if raw.get("cash_and_carry_pairs") is not None:
        try:
            data["cash_and_carry_pairs"] = cash_and_carry_pairs_to_list(
                _cash_and_carry_pairs_from_payload(
                    {"cash_and_carry_pairs": raw.get("cash_and_carry_pairs")}
                )
            )
        except (TypeError, ValueError) as exc:
            return {
                "loaded": False,
                "path": str(path),
                "error": f"invalid cash_and_carry_pairs in runtime store: {exc}",
                "data": {},
            }

    return {
        "loaded": True,
        "path": str(path),
        "updated_at": raw.get("updated_at"),
        "data": data,
    }

def _save_runtime_overrides(path: Path, payload: dict[str, Any]) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError as exc:
        return f"{exc.__class__.__name__}: {exc}"
    return None

def _build_initial_payload(cfg: BotConfig, poll_seconds: float) -> dict[str, Any]:
    maker_configs = market_maker_configs_for_runtime(cfg)
    primary_maker = maker_configs[0] if maker_configs else cfg.market_maker
    primary_conversion = (
        market_maker_quote_conversion(cfg, primary_maker.symbol)
        if primary_maker.symbol
        else {
            "quote_currency": "",
            "common_quote_currency": cfg.common_quote_currency,
            "quote_to_common_rate": None,
            "available": False,
        }
    )
    return {
        "status": "starting",
        "config": {
            "poll_seconds": poll_seconds,
            "notional_quote": cfg.notional_quote,
            "min_profit_quote": cfg.min_profit_quote,
            "min_profit_bps": cfg.min_profit_bps,
            "common_quote_currency": cfg.common_quote_currency,
            "spot_markets": spot_markets_to_list(cfg.spot_markets),
            "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                cfg.cash_and_carry_pairs
            ),
            "triangular_arbitrage": asdict(cfg.triangular_arbitrage),
            "contract_strategies": contract_strategies_config_to_dict(
                cfg.contract_strategies
            ),
            "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
            "derivative_exchanges": exchange_configs_to_list(cfg.derivative_exchanges),
            "strategy_universe": strategy_universe_to_dict(cfg),
        },
        "scan": {
            "count": 0,
            "elapsed_ms": None,
            "last_started": None,
            "last_finished": None,
        },
        "markets": [],
        "quote_rates": cfg.quote_rates,
        "opportunities": [],
        "recent_opportunities": [],
        "account_balances": {
            "status": "starting",
            "accounts": [],
            "totals": [],
            "checked_account_count": 0,
            "total_account_count": len(_all_account_exchanges(cfg)),
            "last_finished": None,
            "errors": [],
        },
        "order_reliability": {
            "enabled": bool(os.environ.get("CRYPTO_ARB_ORDER_JOURNAL_PATH")),
            "status": "starting",
            "pending_count": 0,
            "unresolved_count": 0,
            "recovered_count": 0,
        },
        "derivatives": {
            "status": "disabled" if not cfg.derivative_exchanges else "starting",
            "accounts": [],
            "position_count": 0,
            "checked_account_count": 0,
            "total_account_count": len(cfg.derivative_exchanges),
            "funding_rate_count": 0,
            "limits": {
                "max_derivative_leverage": cfg.risk.max_derivative_leverage,
                "min_liquidation_buffer_pct": cfg.risk.min_liquidation_buffer_pct,
                "max_margin_usage_pct": cfg.risk.max_margin_usage_pct,
            },
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "funding_basis": {
            "status": "disabled",
            "mode": "paper",
            "rows": [],
            "candidate_count": 0,
            "configured_count": 0,
            "checked_count": 0,
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "options_arbitrage": {
            "status": "disabled" if not cfg.option_combos else "starting",
            "mode": "paper",
            "rows": [],
            "option_chain": [],
            "strategy_candidates": [],
            "risk": {
                "status": "disabled" if not cfg.option_combos else "starting",
                "total_delta": None,
                "total_gamma": None,
                "total_vega": None,
                "total_theta": None,
                "greeks_available_count": 0,
                "chain_option_count": 0,
                "expiry_concentration": [],
                "expiry_reminders": [],
                "blocked_new_open_count": 0,
                "max_loss_quote": None,
                "max_profit_quote": None,
                "break_even_points": [],
                "controls": {
                    "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
                    "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
                    "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
                    "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
                    "paper_mode_only": True,
                    "auto_submit_live_orders": False,
                },
                "updated_at": None,
            },
            "execution_controls": {
                "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
                "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
                "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
                "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
                "paper_mode_only": True,
                "auto_submit_live_orders": False,
            },
            "opportunities": [],
            "candidate_count": 0,
            "parity_candidate_count": 0,
            "enhanced_candidate_count": 0,
            "configured_count": len(cfg.option_combos),
            "checked_count": 0,
            "thresholds": {
                "notional_quote": cfg.options_arbitrage.notional_quote,
                "min_edge_quote": cfg.options_arbitrage.min_edge_quote,
                "min_edge_bps": cfg.options_arbitrage.min_edge_bps,
                "max_contracts": cfg.options_arbitrage.max_contracts,
                "max_days_to_expiry": cfg.options_arbitrage.max_days_to_expiry,
                "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
                "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
                "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
                "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
            },
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "contract_strategies": build_contract_strategies_payload(
            cfg,
            funding_basis={},
            derivatives={},
            market_maker={},
            order_activity={},
        ),
        "execution_protection": {
            "status": "disabled",
            "mode": "paper",
            "protection_count": 0,
            "ok_count": 0,
            "blocked_count": 0,
            "warning_count": 0,
            "manual_review_count": 0,
            "slippage_block_count": 0,
            "stale_block_count": 0,
            "rows": [],
            "top_reasons": [],
            "updated_at": None,
        },
        "order_activity": {
            "status": "starting",
            "accounts": [],
            "open_orders": [],
            "closed_orders": [],
            "recent_trades": [],
            "pnl_summary": {
                "currency": cfg.common_quote_currency,
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
                "enabled": cfg.pnl_store.enabled,
                "path": cfg.pnl_store.path,
                "stored_fill_count": 0,
                "daily": None,
            },
            "daily_pnl": {
                "enabled": cfg.pnl_store.enabled,
                "path": cfg.pnl_store.path,
                "day": None,
                "currency": cfg.common_quote_currency,
                "trade_count": 0,
                "total_realized_pnl": 0.0,
                "total_fees": 0.0,
                "total_notional": 0.0,
                "sources": {},
                "updated_at": None,
            },
            "strategy_performance": {
                "status": "starting",
                "currency": cfg.common_quote_currency,
                "window": "daily",
                "row_count": 0,
                "rows": [],
                "summary": {
                    "realized_pnl": 0.0,
                    "fees_common": 0.0,
                    "fill_count": 0,
                    "submitted_order_count": 0,
                },
                "updated_at": None,
            },
            "open_order_count": 0,
            "closed_order_count": 0,
            "recent_trade_count": 0,
            "reconciliation": {
                "status": "starting",
                "tracked_order_count": 0,
                "matched_open_count": 0,
                "matched_fill_count": 0,
                "untracked_open_count": 0,
                "unattributed_fill_count": 0,
                "issue_count": 0,
                "notice_count": 0,
                "total_item_count": 0,
                "level_counts": {"error": 0, "warning": 0, "info": 0},
                "critical_issue_count": 0,
                "auto_stop_recommended": False,
                "auto_stop_reasons": [],
                "recoverable_issue_count": 0,
                "automatic_retry_active": False,
                "recoverable_reasons": [],
                "issues": [],
                "checked_at": None,
            },
            "checked_account_count": 0,
            "total_account_count": len(_all_account_exchanges(cfg)),
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
        "trading_console": build_trading_console_payload(cfg),
        "readiness": build_readiness_payload(cfg),
        "runtime_store": {
            "enabled": False,
            "path": "",
            "loaded": False,
            "saved_at": None,
            "error": None,
        },
        "strategy_center": build_strategy_center_payload(cfg),
        "onchain": {
            "status": "disabled",
            "label": cfg.onchain_monitor.label,
            "mint": cfg.onchain_monitor.token_mint,
            "holders": [],
            "history": {
                "enabled": cfg.onchain_monitor.enabled,
                "path": cfg.onchain_monitor.history_path,
                "baseline_at": None,
                "updated_at": None,
                "event_count": 0,
                "new_event_count": 0,
                "recent_events": [],
            },
            "rpc": {
                "active_url": cfg.onchain_monitor.rpc_url,
                "endpoint_count": len(cfg.onchain_monitor.rpc_urls or []),
                "env": cfg.onchain_monitor.rpc_url_env,
            },
            "last_finished": None,
            "error": None,
        },
        "market_maker": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": market_maker_config_to_dict(primary_maker),
            "instances": market_maker_configs_to_list(maker_configs),
            "accounts": slow_execution_accounts(
                _all_account_exchanges(cfg),
                _market_maker_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": primary_conversion,
            "safety": build_market_maker_safety_payload(
                cfg,
                None,
                primary_conversion,
            ),
            "runtime": {},
            "quality": {},
            "error": None,
        },
        "slow_execution": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": slow_execution_config_to_dict(cfg.slow_execution),
            "accounts": slow_execution_accounts(
                auto_buy_sell_exchanges(cfg),
                _auto_buy_sell_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "tasks": {
                "status": "ok",
                "path": default_task_store_path(cfg),
                "tasks": [],
                "task_count": 0,
                "active_count": 0,
                "updated_at": time.time(),
            },
            "error": None,
        },
        "cross_exchange_rebalance": {
            "status": (
                "disabled" if not cfg.cross_exchange_rebalance.enabled else "starting"
            ),
            "mode": "dry_run",
            "plan": None,
            "config": cross_exchange_rebalance_config_to_dict(
                cfg.cross_exchange_rebalance
            ),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _rebalance_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "runtime": {
                "status": (
                    "disabled"
                    if not cfg.cross_exchange_rebalance.enabled
                    else "starting"
                ),
                "halted": False,
                "completed_quote_common": 0.0,
                "completed_destination_quote_common": 0.0,
                "completed_base": 0.0,
                "cycle_count": 0,
                "updated_at": time.time(),
            },
            "error": None,
        },
        "spot_grid": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": spot_grid_config_to_dict(cfg.spot_grid),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _grid_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.spot_grid.symbol)
                if cfg.spot_grid.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "safety": None,
            "error": None,
        },
        "dca": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": dca_config_to_dict(cfg.dca),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _grid_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.dca.symbol)
                if cfg.dca.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "safety": None,
            "error": None,
        },
        "execution_algo": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": execution_algo_config_to_dict(cfg.execution_algo),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _execution_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.execution_algo.symbol)
                if cfg.execution_algo.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "safety": None,
            "error": None,
        },
        "backtest": {
            "status": "disabled",
            "mode": "research",
            "result": None,
            "config": backtest_config_to_dict(cfg.backtest),
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                _execution_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            ),
            "quote_conversion": (
                market_maker_quote_conversion(cfg, cfg.backtest.symbol)
                if cfg.backtest.symbol
                else {
                    "quote_currency": "",
                    "common_quote_currency": cfg.common_quote_currency,
                    "quote_to_common_rate": None,
                    "available": False,
                }
            ),
            "error": None,
        },
        "spot_arbitrage": {
            "status": "starting" if cfg.spot_markets else "disabled",
            "mode": "dry_run",
            "plan": None,
            "risk": None,
            "execution": None,
            "error": None,
            "cooldown_seconds": SPOT_ARBITRAGE_EXECUTION_COOLDOWN_SECONDS,
        },
        "portfolio": {
            "status": "disabled",
            "asset": cfg.portfolio.asset,
            "quote_currency": cfg.common_quote_currency,
            "position_base": cfg.portfolio.position_base,
            "average_entry_price": cfg.portfolio.average_entry_price,
            "positions": [
                {
                    "asset": position.asset,
                    "position_base": position.position_base,
                    "average_entry_price": position.average_entry_price,
                    "mark_price": None,
                    "mark_source_count": 0,
                    "position_value": None,
                    "price_move_pnl": 0.0,
                    "status": "starting",
                }
                for position in cfg.portfolio.positions
            ],
            "position_missing_marks": [],
            "cash_balances": cfg.portfolio.cash_balances,
            "cash_balances_common": {},
            "cash_value": 0.0,
            "cash_missing_rates": [],
            "mark_price": None,
            "mark_source_count": 0,
            "position_value": None,
            "total_pnl": 0.0,
            "sources": {
                "market_maker": 0.0,
                "arbitrage": 0.0,
                "auto_buy_sell": 0.0,
                "manual": 0.0,
                "unattributed": 0.0,
                "price_move": 0.0,
            },
            "observed_at": None,
        },
        "program": {
            "running": True,
            "updated_at": time.time(),
            "auto_stopped": False,
            "stop_reason": None,
            "stopped_at": None,
        },
        "operations": build_operations_payload(cfg),
        "warnings": ["Waiting for first scan"],
    }

def _missing_market_warnings(rows: Iterable[dict[str, Any]]) -> list[str]:
    return [
        f"Missing {row['exchange']} {row['symbol']}"
        for row in rows
        if row["status"] != "ok"
    ]

def build_market_maker_safety_payload(
    cfg: BotConfig,
    plan: MarketMakerPlan | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    risk_cfg = market_maker_risk_config(cfg)
    limits = {
        "max_order_quote": risk_cfg.max_order_quote,
        "max_cycle_quote": risk_cfg.max_cycle_quote,
        "max_orders_per_cycle": risk_cfg.max_orders_per_cycle,
        "max_open_orders": risk_cfg.max_open_orders,
        "max_cancels_per_cycle": risk_cfg.max_cancels_per_cycle,
        "min_seconds_between_cancels": risk_cfg.min_seconds_between_cancels,
        "max_daily_loss_quote": risk_cfg.max_daily_loss_quote,
        "max_exposure_quote": risk_cfg.max_exposure_quote,
        "min_order_book_depth_quote": risk_cfg.min_order_book_depth_quote,
        "max_slippage_bps": risk_cfg.max_slippage_bps,
        "max_order_book_age_seconds": risk_cfg.max_order_book_age_seconds,
        "max_order_book_gap_bps": risk_cfg.max_order_book_gap_bps,
        "max_price_jump_bps": risk_cfg.max_price_jump_bps,
    }
    base_payload: dict[str, Any] = {
        "approved": False,
        "level": "blocked" if error else "disabled",
        "currency": cfg.common_quote_currency,
        "quote_conversion": conversion,
        "limits": limits,
        "order_count": 0,
        "buy_order_count": 0,
        "sell_order_count": 0,
        "total_quote_notional": 0.0,
        "max_order_quote_notional": 0.0,
        "min_order_quote_notional": 0.0,
        "reasons": [error] if error else [],
        "warnings": [],
        "risk": None,
    }
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    quote_values = [order.quote_notional * quote_rate_for_risk for order in plan.orders]
    risk_orders = [
        RiskOrder(
            strategy="market_maker",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price * quote_rate_for_risk,
            quote_notional=order.quote_notional * quote_rate_for_risk,
            distance_bps=order.distance_bps,
        )
        for order in plan.orders
    ]
    market = RiskMarketContext(
        exchange=plan.exchange,
        symbol=plan.symbol,
        best_bid=plan.best_bid * quote_rate_for_risk,
        best_ask=plan.best_ask * quote_rate_for_risk,
        mid_price=plan.mid_price * quote_rate_for_risk,
        bid_depth_quote=plan.bid_depth_quote * quote_rate_for_risk,
        ask_depth_quote=plan.ask_depth_quote * quote_rate_for_risk,
        max_level_gap_bps=plan.max_level_gap_bps,
        order_book_timestamp_ms=plan.order_book_timestamp_ms,
        order_book_received_at=plan.order_book_received_at,
    )
    risk = evaluate_order_batch(
        risk_cfg,
        risk_orders,
        strategy="market_maker",
        live=True,
        existing_spread_bps=plan.existing_spread_bps,
        plan_observed_at=plan.observed_at,
        market=market,
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        post_only=cfg.market_maker.post_only,
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    approved = len(reasons) == 0
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(plan.orders),
        "buy_order_count": sum(1 for order in plan.orders if order.side == "buy"),
        "sell_order_count": sum(1 for order in plan.orders if order.side == "sell"),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": reasons,
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": reasons,
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "existing_spread_bps": plan.existing_spread_bps,
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }

def _build_market_maker_instance_payload(
    cfg: BotConfig,
    maker_cfg: MarketMakerConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    instance_cfg = replace(cfg, market_maker=maker_cfg)
    config_payload = market_maker_config_to_dict(maker_cfg)
    conversion = (
        market_maker_quote_conversion(cfg, maker_cfg.symbol)
        if maker_cfg.symbol
        else {
            "quote_currency": "",
            "common_quote_currency": cfg.common_quote_currency,
            "quote_to_common_rate": None,
            "available": False,
        }
    )
    exchange_cfg = next(
        (
            exchange
            for exchange in _all_account_exchanges(cfg)
            if exchange.key == maker_cfg.exchange
        ),
        None,
    )
    exchange_features = (
        limit_order_features(exchange_cfg).to_dict() if exchange_cfg else {}
    )
    if not maker_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "exchange_features": exchange_features,
            "safety": build_market_maker_safety_payload(instance_cfg, None, conversion),
            "market_data": None,
            "runtime": {},
            "error": None,
        }

    book = books.get((maker_cfg.exchange, maker_cfg.symbol))
    if book is None:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "exchange_features": exchange_features,
            "safety": build_market_maker_safety_payload(
                instance_cfg,
                None,
                conversion,
                error=f"Missing {maker_cfg.exchange} {maker_cfg.symbol}",
            ),
            "market_data": None,
            "runtime": {},
            "error": f"Missing {maker_cfg.exchange} {maker_cfg.symbol}",
        }

    try:
        inventory_base = portfolio_positions_base(cfg.portfolio).get(
            _base_currency_from_symbol(maker_cfg.symbol),
        )
        plan = build_symmetric_market_maker_plan(
            book,
            maker_cfg,
            inventory_base=inventory_base,
        )
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "exchange_features": exchange_features,
            "safety": build_market_maker_safety_payload(
                instance_cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "market_data": order_book_market_data(book),
            "runtime": {},
            "error": str(exc),
        }

    safety = build_market_maker_safety_payload(instance_cfg, plan, conversion)
    return {
        "status": "planned",
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "exchange_features": exchange_features,
        "safety": safety,
        "market_data": order_book_market_data(book),
        "runtime": {},
        "error": None,
    }

def build_market_maker_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    *,
    base_cfg: BotConfig | None = None,
) -> dict[str, Any]:
    maker_configs = market_maker_configs_for_runtime(cfg)
    accounts = slow_execution_accounts(
        _all_account_exchanges(cfg),
        market_maker_symbols_for_accounts(cfg, base_cfg=base_cfg),
        spot_markets=cfg.spot_markets,
    )
    instances = [
        _build_market_maker_instance_payload(cfg, maker_cfg, books, accounts)
        for maker_cfg in maker_configs
    ]
    if not instances:
        maker_cfg = market_maker_config_with_id(cfg.market_maker)
        instances = [
            _build_market_maker_instance_payload(cfg, maker_cfg, books, accounts)
        ]
    primary = dict(instances[0])
    primary["instances"] = instances
    primary["instance_count"] = len(instances)
    primary["active_instance_count"] = sum(
        1 for item in instances if item.get("status") not in {"disabled", "paused"}
    )
    return primary

def build_slow_execution_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    config_payload = slow_execution_config_to_dict(exec_cfg)
    accounts = slow_execution_accounts(
        auto_buy_sell_exchanges(cfg),
        _auto_buy_sell_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    if not exec_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": None,
        }

    book = books.get((exec_cfg.exchange, exec_cfg.symbol))
    if book is None:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": f"Missing {exec_cfg.exchange} {exec_cfg.symbol}",
        }

    try:
        plan = build_slow_execution_plan(book, exec_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "error": None,
    }

def _strategy_quote_conversion(cfg: BotConfig, symbol: str) -> dict[str, Any]:
    if not symbol:
        return {
            "quote_currency": "",
            "common_quote_currency": cfg.common_quote_currency,
            "quote_to_common_rate": None,
            "available": False,
        }
    return market_maker_quote_conversion(cfg, symbol)

def _converted_market_context(
    *,
    exchange: str,
    symbol: str,
    best_bid: float,
    best_ask: float,
    mid_price: float,
    bid_depth_quote: float,
    ask_depth_quote: float,
    max_level_gap_bps: float,
    order_book_timestamp_ms: int | None,
    order_book_received_at: float | None,
    quote_rate_for_risk: float,
) -> RiskMarketContext:
    return RiskMarketContext(
        exchange=exchange,
        symbol=symbol,
        best_bid=best_bid * quote_rate_for_risk,
        best_ask=best_ask * quote_rate_for_risk,
        mid_price=mid_price * quote_rate_for_risk,
        bid_depth_quote=bid_depth_quote * quote_rate_for_risk,
        ask_depth_quote=ask_depth_quote * quote_rate_for_risk,
        max_level_gap_bps=max_level_gap_bps,
        order_book_timestamp_ms=order_book_timestamp_ms,
        order_book_received_at=order_book_received_at,
    )

def _strategy_safety_base(
    cfg: BotConfig,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "approved": False,
        "level": "blocked" if error else "disabled",
        "currency": cfg.common_quote_currency,
        "quote_conversion": conversion,
        "order_count": 0,
        "buy_order_count": 0,
        "sell_order_count": 0,
        "total_quote_notional": 0.0,
        "max_order_quote_notional": 0.0,
        "min_order_quote_notional": 0.0,
        "reasons": [error] if error else [],
        "warnings": [],
        "risk": None,
    }

def build_spot_grid_safety_payload(
    cfg: BotConfig,
    plan: Any | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    base_payload = _strategy_safety_base(cfg, conversion, error=error)
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    quote_values = [order.quote_notional * quote_rate_for_risk for order in plan.orders]
    risk_orders = [
        RiskOrder(
            strategy="spot_grid",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price * quote_rate_for_risk,
            quote_notional=order.quote_notional * quote_rate_for_risk,
            distance_bps=order.distance_bps,
        )
        for order in plan.orders
    ]
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="spot_grid",
        live=True,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=_converted_market_context(
            exchange=plan.exchange,
            symbol=plan.symbol,
            best_bid=plan.best_bid,
            best_ask=plan.best_ask,
            mid_price=plan.mid_price,
            bid_depth_quote=plan.bid_depth_quote,
            ask_depth_quote=plan.ask_depth_quote,
            max_level_gap_bps=plan.max_level_gap_bps,
            order_book_timestamp_ms=plan.order_book_timestamp_ms,
            order_book_received_at=plan.order_book_received_at,
            quote_rate_for_risk=quote_rate_for_risk,
        ),
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        expected_cancel_count=len(plan.orders),
        post_only=cfg.spot_grid.post_only,
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    if plan.status != "planned":
        reasons.append(plan.reason)
    if cfg.spot_grid.max_position_base > 0:
        base_asset = _base_currency_from_symbol(plan.symbol)
        current_base = portfolio_positions_base(cfg.portfolio).get(base_asset, 0.0)
        buy_base = sum(order.amount for order in plan.orders if order.side == "buy")
        projected_base = current_base + buy_base
        if projected_base > cfg.spot_grid.max_position_base:
            reasons.append(
                f"{base_asset} projected grid position {projected_base:.8f} exceeds "
                f"spot_grid.max_position_base {cfg.spot_grid.max_position_base:.8f}"
            )
    approved = len(reasons) == 0
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(plan.orders),
        "buy_order_count": sum(1 for order in plan.orders if order.side == "buy"),
        "sell_order_count": sum(1 for order in plan.orders if order.side == "sell"),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": _dedupe_readiness_messages(reasons),
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": _dedupe_readiness_messages(reasons),
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "grid_step_bps": plan.grid_step_bps,
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }

def build_dca_safety_payload(
    cfg: BotConfig,
    plan: Any | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    base_payload = _strategy_safety_base(cfg, conversion, error=error)
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    order = plan.next_order
    risk_orders = (
        [
            RiskOrder(
                strategy="dca",
                exchange=plan.exchange,
                symbol=plan.symbol,
                side=order.side,
                amount=order.amount,
                price=order.price * quote_rate_for_risk,
                quote_notional=order.quote_notional * quote_rate_for_risk,
            )
        ]
        if order is not None
        else []
    )
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="dca",
        live=True,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=_converted_market_context(
            exchange=plan.exchange,
            symbol=plan.symbol,
            best_bid=plan.best_bid,
            best_ask=plan.best_ask,
            mid_price=plan.mid_price,
            bid_depth_quote=plan.bid_depth_quote,
            ask_depth_quote=plan.ask_depth_quote,
            max_level_gap_bps=plan.max_level_gap_bps,
            order_book_timestamp_ms=plan.order_book_timestamp_ms,
            order_book_received_at=plan.order_book_received_at,
            quote_rate_for_risk=quote_rate_for_risk,
        ),
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        post_only=plan.price_mode == "maker",
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    if plan.status not in {"ready", "waiting_for_trigger"}:
        reasons.append(plan.reason)
    if cfg.dca.max_position_base > 0 and order is not None:
        base_asset = _base_currency_from_symbol(plan.symbol)
        current_base = portfolio_positions_base(cfg.portfolio).get(base_asset, 0.0)
        projected_base = (
            current_base + order.amount
            if order.side == "buy"
            else max(0.0, current_base - order.amount)
        )
        if projected_base > cfg.dca.max_position_base:
            reasons.append(
                f"{base_asset} projected DCA position {projected_base:.8f} exceeds "
                f"dca.max_position_base {cfg.dca.max_position_base:.8f}"
            )
    if cfg.dca.max_loss_quote > 0 and cfg.dca.average_entry_price > 0:
        base_asset = _base_currency_from_symbol(plan.symbol)
        current_base = portfolio_positions_base(cfg.portfolio).get(base_asset, 0.0)
        unrealized_loss = (
            max(
                0.0,
                (cfg.dca.average_entry_price - plan.mid_price) * current_base,
            )
            * quote_rate_for_risk
        )
        if unrealized_loss > cfg.dca.max_loss_quote:
            reasons.append(
                f"DCA unrealized loss {unrealized_loss:.8f} exceeds "
                f"dca.max_loss_quote {cfg.dca.max_loss_quote:.8f}"
            )
    approved = len(reasons) == 0
    quote_values = [
        row["quote_notional"] * quote_rate_for_risk for row in plan.order_schedule
    ]
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(risk_orders),
        "buy_order_count": sum(
            1 for risk_order in risk_orders if risk_order.side == "buy"
        ),
        "sell_order_count": sum(
            1 for risk_order in risk_orders if risk_order.side == "sell"
        ),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": _dedupe_readiness_messages(reasons),
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": _dedupe_readiness_messages(reasons),
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }

def build_execution_algo_safety_payload(
    cfg: BotConfig,
    plan: Any | None,
    conversion: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    base_payload = _strategy_safety_base(cfg, conversion, error=error)
    if plan is None:
        return base_payload

    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    next_slice = plan.next_slice
    risk_orders = (
        [
            RiskOrder(
                strategy="execution_algo",
                exchange=plan.exchange,
                symbol=plan.symbol,
                side=next_slice.side,
                amount=next_slice.amount,
                price=next_slice.price * quote_rate_for_risk,
                quote_notional=next_slice.quote_notional * quote_rate_for_risk,
            )
        ]
        if next_slice is not None
        else []
    )
    risk = evaluate_order_batch(
        cfg.risk,
        risk_orders,
        strategy="execution_algo",
        live=True,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=_converted_market_context(
            exchange=plan.exchange,
            symbol=plan.symbol,
            best_bid=plan.best_bid,
            best_ask=plan.best_ask,
            mid_price=plan.mid_price,
            bid_depth_quote=plan.bid_depth_quote,
            ask_depth_quote=plan.ask_depth_quote,
            max_level_gap_bps=plan.max_level_gap_bps,
            order_book_timestamp_ms=plan.order_book_timestamp_ms,
            order_book_received_at=plan.order_book_received_at,
            quote_rate_for_risk=quote_rate_for_risk,
        ),
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=0,
        post_only=plan.price_mode == "maker",
    )
    risk_payload = risk.to_dict()
    reasons = list(risk_payload.get("reasons", []))
    warnings = list(risk_payload.get("warnings", []))
    if quote_rate is None:
        reasons.append(
            f"missing quote rate for {conversion.get('quote_currency') or '?'} -> "
            f"{cfg.common_quote_currency}"
        )
    if plan.status not in {"ready", "waiting_for_start"}:
        reasons.append(plan.reason)
    if plan.max_slippage_bps > cfg.risk.max_slippage_bps:
        warnings.append(
            f"execution max_slippage_bps {plan.max_slippage_bps:.4f} exceeds "
            f"risk.max_slippage_bps {cfg.risk.max_slippage_bps:.4f}"
        )
    approved = len(reasons) == 0
    quote_values = [item.quote_notional * quote_rate_for_risk for item in plan.schedule]
    return {
        **base_payload,
        "approved": approved,
        "level": "ok" if approved else "blocked",
        "order_count": len(risk_orders),
        "buy_order_count": sum(
            1 for risk_order in risk_orders if risk_order.side == "buy"
        ),
        "sell_order_count": sum(
            1 for risk_order in risk_orders if risk_order.side == "sell"
        ),
        "total_quote_notional": sum(quote_values),
        "max_order_quote_notional": max(quote_values) if quote_values else 0.0,
        "min_order_quote_notional": min(quote_values) if quote_values else 0.0,
        "reasons": _dedupe_readiness_messages(reasons),
        "warnings": warnings,
        "risk": {
            **risk_payload,
            "approved": approved,
            "level": "ok" if approved else "blocked",
            "reasons": _dedupe_readiness_messages(reasons),
            "warnings": warnings,
            "currency": cfg.common_quote_currency,
            "quote_conversion": conversion,
        },
        "market": {
            "bid_depth_quote": plan.bid_depth_quote * quote_rate_for_risk,
            "ask_depth_quote": plan.ask_depth_quote * quote_rate_for_risk,
            "max_level_gap_bps": plan.max_level_gap_bps,
            "order_book_timestamp_ms": plan.order_book_timestamp_ms,
            "order_book_received_at": plan.order_book_received_at,
        },
    }

def build_spot_grid_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    config_payload = spot_grid_config_to_dict(grid_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _grid_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, grid_cfg.symbol)
    if not grid_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_spot_grid_safety_payload(cfg, None, conversion),
            "error": None,
        }

    book = books.get((grid_cfg.exchange, grid_cfg.symbol))
    if book is None:
        error = f"Missing {grid_cfg.exchange} {grid_cfg.symbol}"
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_spot_grid_safety_payload(
                cfg, None, conversion, error=error
            ),
            "error": error,
        }

    try:
        plan = build_spot_grid_plan(book, grid_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_spot_grid_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "safety": build_spot_grid_safety_payload(cfg, plan, conversion),
        "error": None,
    }

def build_dca_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    dca_cfg = cfg.dca
    config_payload = dca_config_to_dict(dca_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _grid_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, dca_cfg.symbol)
    if not dca_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_dca_safety_payload(cfg, None, conversion),
            "error": None,
        }

    book = books.get((dca_cfg.exchange, dca_cfg.symbol))
    if book is None:
        error = f"Missing {dca_cfg.exchange} {dca_cfg.symbol}"
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_dca_safety_payload(cfg, None, conversion, error=error),
            "error": error,
        }

    try:
        plan = build_dca_plan(book, dca_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_dca_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "safety": build_dca_safety_payload(cfg, plan, conversion),
        "error": None,
    }

def build_execution_algo_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    exec_cfg = cfg.execution_algo
    config_payload = execution_algo_config_to_dict(exec_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _execution_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, exec_cfg.symbol)
    if not exec_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_execution_algo_safety_payload(cfg, None, conversion),
            "error": None,
        }

    book = books.get((exec_cfg.exchange, exec_cfg.symbol))
    if book is None:
        error = f"Missing {exec_cfg.exchange} {exec_cfg.symbol}"
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_execution_algo_safety_payload(
                cfg,
                None,
                conversion,
                error=error,
            ),
            "error": error,
        }

    try:
        plan = build_execution_algo_plan(book, exec_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "safety": build_execution_algo_safety_payload(
                cfg,
                None,
                conversion,
                error=str(exc),
            ),
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "safety": build_execution_algo_safety_payload(cfg, plan, conversion),
        "error": None,
    }

def build_backtest_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    backtest_cfg = cfg.backtest
    config_payload = backtest_config_to_dict(backtest_cfg)
    accounts = slow_execution_accounts(
        cfg.spot_exchanges,
        _execution_symbols_by_exchange(cfg),
        spot_markets=cfg.spot_markets,
    )
    conversion = _strategy_quote_conversion(cfg, backtest_cfg.symbol)
    if not backtest_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "research",
            "result": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "error": None,
        }

    current_mid = None
    book = books.get((backtest_cfg.exchange, backtest_cfg.symbol))
    if book is not None and book.bids and book.asks:
        current_mid = (book.bids[0].price + book.asks[0].price) / 2

    try:
        result = run_paper_backtest(
            backtest_cfg,
            spot_grid=cfg.spot_grid,
            dca=cfg.dca,
            execution_algo=cfg.execution_algo,
            current_mid=current_mid,
        )
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "research",
            "result": None,
            "config": config_payload,
            "accounts": accounts,
            "quote_conversion": conversion,
            "error": str(exc),
        }

    return {
        "status": result.status,
        "mode": "research",
        "result": result.to_dict(),
        "config": config_payload,
        "accounts": accounts,
        "quote_conversion": conversion,
        "error": None,
    }

async def fetch_onchain_payload(
    cfg: BotConfig,
    client: SolanaTokenClient | None,
) -> dict[str, Any]:
    onchain_cfg = cfg.onchain_monitor
    if not onchain_cfg.enabled:
        return {
            "status": "disabled",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "holders": [],
            "history": {
                "enabled": False,
                "path": onchain_cfg.history_path,
                "event_count": 0,
                "recent_events": [],
            },
            "last_finished": None,
            "rpc": {
                "active_url": onchain_cfg.rpc_url,
                "endpoint_count": len(onchain_cfg.rpc_urls or []),
                "env": onchain_cfg.rpc_url_env,
            },
            "error": None,
        }
    if onchain_cfg.network.lower() != "solana":
        return {
            "status": "error",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "holders": [],
            "history": {
                "enabled": False,
                "path": onchain_cfg.history_path,
                "event_count": 0,
                "recent_events": [],
            },
            "last_finished": time.time(),
            "rpc": {
                "active_url": onchain_cfg.rpc_url,
                "endpoint_count": len(onchain_cfg.rpc_urls or []),
                "env": onchain_cfg.rpc_url_env,
            },
            "error": f"Unsupported network: {onchain_cfg.network}",
        }
    if client is None:
        return {
            "status": "error",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "holders": [],
            "history": {
                "enabled": False,
                "path": onchain_cfg.history_path,
                "event_count": 0,
                "recent_events": [],
            },
            "last_finished": time.time(),
            "rpc": {
                "active_url": onchain_cfg.rpc_url,
                "endpoint_count": len(onchain_cfg.rpc_urls or []),
                "env": onchain_cfg.rpc_url_env,
            },
            "error": "Solana client is not configured",
        }

    data = await fetch_top_token_owners(
        client,
        onchain_cfg.token_mint,
        top_n=onchain_cfg.top_n,
    )
    holders = data["holders"]
    labels = onchain_cfg.address_labels
    for holder in holders:
        label = labels.get(holder["owner"])
        holder["label"] = label or "Unknown"
        holder["is_labeled"] = label is not None

    observed_at = time.time()
    history = update_holder_history(
        path=onchain_cfg.history_path,
        mint=onchain_cfg.token_mint,
        label=onchain_cfg.label,
        holders=holders,
        address_labels=labels,
        observed_at=observed_at,
    )
    return {
        "status": "running",
        "label": onchain_cfg.label,
        "mint": onchain_cfg.token_mint,
        "supply": data["supply"],
        "decimals": data["decimals"],
        "holders": holders,
        "history": history,
        "source_account_count": data["source_account_count"],
        "last_finished": observed_at,
        "rpc": {
            "active_url": client.active_rpc_url,
            "endpoint_count": len(client.rpc_urls),
            "env": onchain_cfg.rpc_url_env,
        },
        "error": None,
    }

def _cached_onchain_payload(
    cfg: BotConfig,
    *,
    status: str = "cached",
    error: str | None = None,
) -> dict[str, Any] | None:
    onchain_cfg = cfg.onchain_monitor
    if not onchain_cfg.enabled:
        return None
    snapshot = load_cached_holder_snapshot(
        path=onchain_cfg.history_path,
        mint=onchain_cfg.token_mint,
        label=onchain_cfg.label,
        address_labels=onchain_cfg.address_labels,
        top_n=onchain_cfg.top_n,
    )
    if snapshot is None:
        return None
    return {
        **snapshot,
        "status": status,
        "error": error,
        "rpc": {
            "active_url": onchain_cfg.rpc_url,
            "endpoint_count": len(onchain_cfg.rpc_urls or []),
            "env": onchain_cfg.rpc_url_env,
        },
        "stale": status != "running",
    }

def _onchain_error_payload(
    cfg: BotConfig,
    previous_payload: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    error = str(exc)
    cached = _cached_onchain_payload(cfg, status="error", error=error)
    if cached is not None:
        return cached
    return {
        **previous_payload,
        "status": "error",
        "label": cfg.onchain_monitor.label,
        "mint": cfg.onchain_monitor.token_mint,
        "holders": previous_payload.get("holders", []),
        "history": previous_payload.get(
            "history",
            {
                "enabled": True,
                "path": cfg.onchain_monitor.history_path,
                "event_count": 0,
                "recent_events": [],
            },
        ),
        "last_finished": previous_payload.get("last_finished") or time.time(),
        "error": error,
        "stale": bool(previous_payload.get("holders")),
    }

def _global_scan_health_warnings(
    *,
    onchain_payload: dict[str, Any] | None = None,
    account_balances_payload: dict[str, Any] | None = None,
    order_activity_payload: dict[str, Any] | None = None,
) -> list[str]:
    warnings: list[str] = []
    # On-chain holder monitoring is informational and can be rate-limited by
    # public RPC providers. Keep its error inside the On-chain panel without
    # degrading the trading dashboard's global status.
    _ = onchain_payload
    if (account_balances_payload or {}).get("status") == "error":
        errors = (account_balances_payload or {}).get("errors") or ["unavailable"]
        warnings.append(f"Account balances: {errors[0]}")
    if (order_activity_payload or {}).get("status") == "error":
        errors = (order_activity_payload or {}).get("errors") or ["unavailable"]
        warnings.append(f"Orders: {errors[0]}")
    return warnings
