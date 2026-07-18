from __future__ import annotations

import asyncio
from dataclasses import replace

from arbitrage_bot.config import (
    BotConfig,
    CrossExchangeRebalanceConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
)
from arbitrage_bot.web.strategy_lifecycle import build_strategy_lifecycle_payload
from arbitrage_bot.web.state import MonitorState


def _config(**overrides: object) -> BotConfig:
    values = {
        "poll_seconds": 1.0,
        "order_book_depth": 5,
        "notional_quote": 1.0,
        "min_profit_quote": 0.0,
        "min_profit_bps": 1.0,
        "min_basis_bps": 1.0,
        "common_quote_currency": "USD",
        "quote_rates": {"USD": 1.0, "USDC": 1.0, "USDT": 1.0},
        "quote_rate_sources": [],
        "onchain_monitor": OnchainMonitorConfig(),
        "market_maker": MarketMakerConfig(),
        "slow_execution": SlowExecutionConfig(),
        "portfolio": PortfolioConfig(),
        "spot_symbols": [],
        "spot_markets": [],
        "cash_and_carry_pairs": [],
        "spot_exchanges": [
            ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ExchangeConfig(id="bybit", label="bybit-spot"),
        ],
        "derivative_exchanges": [],
    }
    values.update(overrides)
    return BotConfig(**values)


def _rows(payload: dict[str, object], strategy_id: str) -> list[dict[str, object]]:
    return [
        row
        for row in payload["instances"]  # type: ignore[index]
        if row["strategy_id"] == strategy_id
    ]


def test_market_maker_instances_keep_independent_actual_states() -> None:
    coinbase = MarketMakerConfig(
        id="coinbase-acs",
        enabled=True,
        live_enabled=True,
        exchange="coinbase-spot",
        symbol="ACS/USDC",
    )
    bybit = replace(
        coinbase,
        id="bybit-acs",
        exchange="bybit-spot",
        symbol="ACS/USDT",
    )
    cfg = _config(market_maker=coinbase, market_makers=[coinbase, bybit])
    lifecycle = build_strategy_lifecycle_payload(
        cfg,
        program={"running": True},
        market_maker={
            "runtime": {
                "instances": [
                    {
                        "id": "coinbase-acs",
                        "status": "unchanged",
                        "mode": "live",
                        "updated_at": 100.0,
                    },
                    {
                        "id": "bybit-acs",
                        "status": "blocked_by_risk",
                        "mode": "live",
                        "last_risk": {"reasons": ["order book depth is too low"]},
                        "updated_at": 101.0,
                    },
                ]
            }
        },
    )

    rows = {row["instance_id"]: row for row in _rows(lifecycle, "market_maker")}
    assert rows["coinbase-acs"]["desired_state"] == "running"
    assert rows["coinbase-acs"]["actual_state"] == "running"
    assert rows["coinbase-acs"]["converged"] is True
    assert rows["bybit-acs"]["actual_state"] == "blocked"
    assert rows["bybit-acs"]["convergence_state"] == "blocked"
    assert rows["bybit-acs"]["reason"] == "order book depth is too low"
    assert lifecycle["summary"]["blocked_count"] == 1


def test_market_maker_reconciliation_required_is_blocked_with_reason() -> None:
    maker = MarketMakerConfig(
        id="coinbase-acs",
        enabled=True,
        live_enabled=True,
        exchange="coinbase-spot",
        symbol="ACS/USDC",
    )
    lifecycle = build_strategy_lifecycle_payload(
        _config(market_maker=maker, market_makers=[maker]),
        program={"running": True},
        market_maker={
            "runtime": {
                "instances": [
                    {
                        "id": maker.id,
                        "status": "reconciliation_required",
                        "mode": "live",
                        "reason": "an earlier order result is still uncertain",
                    }
                ]
            }
        },
    )

    row = _rows(lifecycle, "market_maker")[0]
    assert row["actual_state"] == "blocked"
    assert row["convergence_state"] == "blocked"
    assert row["reason"] == "an earlier order result is still uncertain"


def test_auto_waiting_state_is_healthy_and_program_pause_converges() -> None:
    cfg = _config()
    task = {
        "id": "task-1",
        "status": "waiting_for_start_price",
        "last_status": "waiting_for_start_price",
        "config": {
            "exchange": "coinbase-spot",
            "symbol": "ACS/USDC",
            "side": "buy",
        },
        "updated_at": 200.0,
    }
    running = build_strategy_lifecycle_payload(
        cfg,
        program={"running": True},
        auto_buy_sell_tasks={"tasks": [task]},
    )
    running_row = _rows(running, "slow_execution")[0]
    assert running_row["desired_state"] == "running"
    assert running_row["actual_state"] == "waiting"
    assert running_row["converged"] is True

    paused = build_strategy_lifecycle_payload(
        cfg,
        program={"running": False},
        auto_buy_sell_tasks={"tasks": [{**task, "last_status": "program_paused"}]},
    )
    paused_row = _rows(paused, "slow_execution")[0]
    assert paused_row["desired_state"] == "paused"
    assert paused_row["actual_state"] == "paused"
    assert paused_row["converged"] is True


def test_terminal_auto_task_exposes_cleanup_action() -> None:
    lifecycle = build_strategy_lifecycle_payload(
        _config(),
        auto_buy_sell_tasks={
            "tasks": [
                {
                    "id": "task-complete",
                    "status": "complete",
                    "config": {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                    },
                }
            ]
        },
    )
    row = _rows(lifecycle, "slow_execution")[0]
    assert row["desired_state"] == "stopped"
    assert row["actual_state"] == "complete"
    assert row["converged"] is True
    assert row["allowed_actions"] == ["cleanup"]


def test_rebalance_waiting_for_cost_is_running_and_converged() -> None:
    cfg = _config(
        cross_exchange_rebalance=CrossExchangeRebalanceConfig(
            enabled=True,
            live_enabled=True,
            buy_exchange="bithumb-spot",
            buy_symbol="ACS/KRW",
            sell_exchange="coinbase-spot",
            sell_symbol="ACS/USDC",
            total_quote_common=100.0,
        )
    )
    lifecycle = build_strategy_lifecycle_payload(
        cfg,
        cross_exchange_rebalance={
            "runtime": {
                "status": "waiting_for_cost",
                "mode": "live",
                "last_payload": {"risk": {"reasons": ["expected cost exceeds limit"]}},
            }
        },
    )
    row = _rows(lifecycle, "cross_exchange_rebalance")[0]
    assert row["desired_state"] == "running"
    assert row["actual_state"] == "waiting"
    assert row["converged"] is True
    assert row["reason"] is None


def test_rebalance_waiting_for_market_data_is_retrying_and_converged() -> None:
    cfg = _config(
        cross_exchange_rebalance=CrossExchangeRebalanceConfig(
            enabled=True,
            live_enabled=True,
            buy_exchange="coinbase-spot",
            buy_symbol="ACS/USDC",
            sell_exchange="bithumb-spot",
            sell_symbol="ACS/KRW",
            total_quote_common=100.0,
        )
    )
    lifecycle = build_strategy_lifecycle_payload(
        cfg,
        cross_exchange_rebalance={
            "runtime": {
                "status": "waiting_for_market_data",
                "mode": "live",
            }
        },
    )

    row = _rows(lifecycle, "cross_exchange_rebalance")[0]
    assert row["desired_state"] == "running"
    assert row["actual_state"] == "waiting"
    assert row["converged"] is True


def test_spot_arbitrage_dry_run_scanner_is_a_running_strategy() -> None:
    cfg = _config(
        spot_markets=[
            SpotMarketConfig("ACS", "coinbase-spot", "ACS/USDC", "USDC"),
            SpotMarketConfig("ACS", "bybit-spot", "ACS/USDT", "USDT"),
        ]
    )
    lifecycle = build_strategy_lifecycle_payload(
        cfg,
        spot_arbitrage={"status": "no_opportunity", "mode": "dry_run"},
    )
    row = _rows(lifecycle, "spot_spread")[0]
    assert row["desired_state"] == "running"
    assert row["actual_state"] == "waiting"
    assert row["mode"] == "dry_run"
    assert row["converged"] is True
    assert row["allowed_actions"] == ["pause"]
    rebalance = _rows(lifecycle, "cross_exchange_rebalance")[0]
    assert rebalance["allowed_actions"] == ["start"]


def test_monitor_state_exposes_lifecycle_and_tracks_pause_transition() -> None:
    maker = MarketMakerConfig(
        id="coinbase-acs",
        enabled=True,
        live_enabled=True,
        exchange="coinbase-spot",
        symbol="ACS/USDC",
    )
    cfg = _config(market_maker=maker, market_makers=[maker])

    async def run() -> None:
        state = MonitorState(cfg, 1.0)
        await state.set_market_maker_instance_runtime(
            "coinbase-acs",
            {
                "status": "placed",
                "mode": "live",
                "open_order_count": 20,
                "updated_at": 300.0,
            },
        )
        running = await state.get(view="trading")
        running_row = _rows(running["strategy_lifecycle"], "market_maker")[0]
        assert running_row["desired_state"] == "running"
        assert running_row["actual_state"] == "running"
        assert running_row["converged"] is True

        await state.set_strategy_paused("market_maker", True, cfg=cfg)
        transitioning = await state.strategy_lifecycle()
        transitioning_row = _rows(transitioning, "market_maker")[0]
        assert transitioning_row["desired_state"] == "paused"
        assert transitioning_row["actual_state"] == "pausing"
        assert transitioning_row["convergence_state"] == "transitioning"

        await state.set_market_maker_instance_runtime(
            "coinbase-acs",
            {
                "status": "paused",
                "mode": "paused",
                "open_order_count": 0,
                "updated_at": 301.0,
            },
        )
        paused = await state.strategy_lifecycle()
        paused_row = _rows(paused, "market_maker")[0]
        assert paused_row["desired_state"] == "paused"
        assert paused_row["actual_state"] == "paused"
        assert paused_row["converged"] is True

    asyncio.run(run())
