from __future__ import annotations

import time
from dataclasses import replace

from arbitrage_bot.config import CrossExchangeRebalanceConfig, RiskConfig, load_config
from arbitrage_bot.web.strategy_preflight import build_strategy_preflight


def _config():
    cfg = load_config("config.acs.example.json")
    return replace(
        cfg,
        cross_exchange_rebalance=CrossExchangeRebalanceConfig(
            enabled=True,
            live_enabled=True,
            buy_exchange="coinbase-spot",
            buy_symbol="ACS/USDC",
            sell_exchange="bithumb-spot",
            sell_symbol="ACS/KRW",
            total_quote_common=100.0,
            quote_per_cycle_common=10.0,
            coordinate_market_maker=True,
        ),
        risk=RiskConfig(
            enabled=True,
            trading_enabled=True,
            allow_live_trading=True,
            account_enabled={
                "coinbase-spot": True,
                "bithumb-spot": True,
            },
            strategy_enabled={"cross_exchange_rebalance": True},
            max_order_quote=100.0,
            max_cycle_quote=100.0,
            max_orders_per_cycle=10,
            max_open_orders=100,
            max_order_book_age_seconds=10.0,
            max_order_book_gap_bps=500.0,
        ),
    )


def _state_payload(order_id: str, *, tracked_order_ids: list[str]):
    now = time.time()
    return {
        "quote_rates": {"USDC": 1.0, "KRW": 0.0007},
        "markets": [
            {
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "status": "ok",
                "bid": 0.00012,
                "ask": 0.000121,
                "bid_size": 10_000_000.0,
                "ask_size": 10_000_000.0,
            },
            {
                "exchange": "bithumb-spot",
                "symbol": "ACS/KRW",
                "status": "ok",
                "bid": 0.18,
                "ask": 0.181,
                "bid_size": 10_000_000.0,
                "ask_size": 10_000_000.0,
            },
        ],
        "account_balances": {
            "last_finished": now,
            "accounts": [
                {
                    "exchange": "coinbase-spot",
                    "auth": {"configured": True, "missing_env": []},
                    "errors": [],
                    "balance": {
                        "checked": True,
                        "currencies": [
                            {"currency": "USDC", "free": 1_000.0},
                            {"currency": "ACS", "free": 10_000_000.0},
                        ],
                    },
                    "markets": [
                        {
                            "symbol": "ACS/USDC",
                            "status": "ok",
                            "market": {
                                "found": True,
                                "active": True,
                                "limits": {"cost_min": 1.0},
                            },
                        }
                    ],
                },
                {
                    "exchange": "bithumb-spot",
                    "auth": {"configured": True, "missing_env": []},
                    "errors": [],
                    "balance": {
                        "checked": True,
                        "currencies": [
                            {"currency": "KRW", "free": 10_000_000.0},
                            {"currency": "ACS", "free": 10_000_000.0},
                        ],
                    },
                    "markets": [
                        {
                            "symbol": "ACS/KRW",
                            "status": "ok",
                            "market": {
                                "found": True,
                                "active": True,
                                "limits": {"cost_min": 1.0},
                            },
                        }
                    ],
                },
            ],
        },
        "order_activity": {
            "last_finished": now,
            "open_orders": [
                {
                    "id": order_id,
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "side": "sell",
                }
            ],
            "daily_pnl": {"total_realized_pnl": 0.0},
        },
        "market_maker": {
            "runtime": {
                "instances": [
                    {
                        "id": "coinbase-acs",
                        "open_order_exchange": "coinbase-spot",
                        "open_order_symbol": "ACS/USDC",
                        "open_order_ids": tracked_order_ids,
                    }
                ]
            }
        },
        "slow_execution": {"tasks": {"tasks": []}},
    }


def _candidate(*, coordinate_market_maker: bool = True):
    return {
        "enabled": True,
        "live_enabled": True,
        "buy_exchange": "coinbase-spot",
        "buy_symbol": "ACS/USDC",
        "sell_exchange": "bithumb-spot",
        "sell_symbol": "ACS/KRW",
        "total_quote_common": 100.0,
        "quote_per_cycle_common": 10.0,
        "coordinate_market_maker": coordinate_market_maker,
    }


def test_rebalance_preflight_allows_tracked_mm_orders_when_coordinated() -> None:
    result = build_strategy_preflight(
        _config(),
        strategy_id="cross_exchange_rebalance",
        candidate=_candidate(),
        state_payload=_state_payload("mm-order-1", tracked_order_ids=["mm-order-1"]),
    )

    assert result["ready"], result["blockers"]
    assert any("will be canceled" in warning for warning in result["warnings"])


def test_rebalance_preflight_still_blocks_untracked_open_orders() -> None:
    result = build_strategy_preflight(
        _config(),
        strategy_id="cross_exchange_rebalance",
        candidate=_candidate(),
        state_payload=_state_payload("manual-order-1", tracked_order_ids=[]),
    )

    assert not result["ready"]
    assert any("manual-order-1" in blocker for blocker in result["blockers"])


def test_rebalance_preflight_blocks_mm_orders_when_coordination_is_off() -> None:
    result = build_strategy_preflight(
        _config(),
        strategy_id="cross_exchange_rebalance",
        candidate=_candidate(coordinate_market_maker=False),
        state_payload=_state_payload("mm-order-1", tracked_order_ids=["mm-order-1"]),
    )

    assert not result["ready"]
    assert any("mm-order-1" in blocker for blocker in result["blockers"])


def _slow_execution_config():
    cfg = _config()
    return replace(
        cfg,
        risk=replace(
            cfg.risk,
            strategy_enabled={"slow_execution": True},
        ),
    )


def _slow_candidate(*, coordinate_market_maker: bool = True):
    return {
        "enabled": True,
        "exchange": "coinbase-spot",
        "symbol": "ACS/USDC",
        "side": "buy",
        "slice_quote": 10.0,
        "block_conflicting_market_maker": True,
        "coordinate_market_maker": coordinate_market_maker,
    }


def test_auto_buy_preflight_allows_tracked_mm_orders_when_coordinated() -> None:
    result = build_strategy_preflight(
        _slow_execution_config(),
        strategy_id="slow_execution",
        candidate=_slow_candidate(),
        state_payload=_state_payload("mm-order-1", tracked_order_ids=["mm-order-1"]),
    )

    assert result["ready"], result["blockers"]
    assert any("tracked conflicting MM" in warning for warning in result["warnings"])


def test_auto_buy_preflight_still_blocks_untracked_orders_when_coordinated() -> None:
    result = build_strategy_preflight(
        _slow_execution_config(),
        strategy_id="slow_execution",
        candidate=_slow_candidate(),
        state_payload=_state_payload("manual-order-1", tracked_order_ids=[]),
    )

    assert not result["ready"]
    assert any("manual-order-1" in blocker for blocker in result["blockers"])


def test_auto_buy_preflight_blocks_mm_orders_when_coordination_is_off() -> None:
    result = build_strategy_preflight(
        _slow_execution_config(),
        strategy_id="slow_execution",
        candidate=_slow_candidate(coordinate_market_maker=False),
        state_payload=_state_payload("mm-order-1", tracked_order_ids=["mm-order-1"]),
    )

    assert not result["ready"]
    assert any("mm-order-1" in blocker for blocker in result["blockers"])
