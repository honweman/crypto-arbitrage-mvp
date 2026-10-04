from __future__ import annotations

import asyncio
import copy
import time

import pytest

from arbitrage_bot.asset_ledger import AssetLedgerStore
from arbitrage_bot.config import AssetLedgerConfig, ExchangeConfig, RiskConfig
from arbitrage_bot.contract_portfolio import (
    apply_contract_portfolio,
    balance_equity_adjustments,
    contract_snapshot,
)
from arbitrage_bot.derivatives import normalize_derivative_position
from arbitrage_bot.user_account_check import check_workspace_api_connection
from arbitrage_bot.user_workspace import UserApiConnection
from arbitrage_bot.web.services.workspace import (
    _merge_workspace_account_balances,
    _sync_portfolio_with_account_balances,
)


def position(**overrides):
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 2,
        "contractSize": 0.01,
        "markPrice": 50000,
        "entryPrice": 49000,
        "unrealizedPnl": 20,
        "initialMargin": 100,
        **overrides,
    }


def account(*positions, key="one", adjustments=None):
    snapshot = contract_snapshot(
        ExchangeConfig(id="bybit", market_type="swap"), list(positions)
    )
    snapshot["equity_adjustments"] = adjustments or {}
    return {
        "exchange": key,
        "label": key,
        "id": "bybit",
        "market_type": "swap",
        "contract_snapshot": snapshot,
        "balance": {
            "checked": True,
            "currencies": [
                {"currency": "USDT", "total": 100, "free": 50, "used": 50},
            ],
        },
    }


def portfolio():
    return {
        "status": "ok",
        "quote_currency": "USD",
        "total_asset_value": 100,
        "cash_balances": {"USDT": 100},
        "cash_balances_common": {"USDT": 100},
        "positions": [],
        "sources": {"market_maker": 3},
    }


def test_long_short_hedge_positions_remain_separate_and_notional_is_not_equity():
    a = account(position(), position(side="short", unrealizedPnl=-8))
    result = apply_contract_portfolio(portfolio(), [a], {"USDT": 0.99})
    assert result["total_asset_value"] == 100
    assert result["contracts"]["gross_notional"] == 1980
    assert result["contracts"]["unrealized_pnl"] == pytest.approx(11.88)
    assert result["total_pnl"] == pytest.approx(14.88)
    assert [p["side"] for p in result["contracts"]["positions"]] == ["long", "short"]
    assert result["contracts"]["positions"][0]["base_amount"] == 0.02


def test_inverse_face_value_and_settlement_pnl():
    a = account(
        position(
            symbol="BTC/USD:BTC",
            contracts=10,
            contractSize=100,
            notional=0.02,
            unrealizedPnl=0.001,
            initialMargin=0.002,
        )
    )
    result = apply_contract_portfolio(portfolio(), [a], {"USD": 1})
    row = result["contracts"]["positions"][0]
    assert row["base_amount"] == 0.02
    assert row["notional_quote_common"] == 1000
    assert row["unrealized_pnl_common"] == 50
    assert row["initial_margin_common"] == 100


def test_zero_pnl_does_not_fall_back_to_old_raw_value():
    row = normalize_derivative_position(
        ExchangeConfig(id="bybit", market_type="swap"),
        position(unrealizedPnl=0, initialMargin=0, info={"unRealizedProfit": "7"}),
        risk=RiskConfig(),
    )
    assert row["unrealized_pnl"] == 0
    assert row["initial_margin"] == 0


def test_missing_rate_or_failed_snapshot_is_not_zero_profit():
    a = account(position(symbol="BTC/EUR:EUR"))
    result = apply_contract_portfolio(portfolio(), [a], {})
    assert result["contracts"]["unrealized_pnl"] is None
    assert result["total_asset_value"] is None
    assert result["contracts"]["missing_rates"] == ["EUR"]
    a["contract_snapshot"]["status"] = "error"
    result = apply_contract_portfolio(portfolio(), [a], {"EUR": 1.1})
    assert result["contracts"]["position_count"] == 1
    assert result["contracts"]["unrealized_pnl"] is None
    assert result["contracts"]["unavailable_accounts"] == ["one"]


def test_stale_snapshot_preserves_rows_but_invalidates_total():
    a = account(position())
    a["contract_snapshot"]["checked_at"] = time.time() - 601
    result = apply_contract_portfolio(portfolio(), [a], {"USDT": 1})
    assert len(result["contracts"]["positions"]) == 1
    assert result["total_asset_value"] is None


def test_bybit_equity_difference_only_added_once():
    exchange = ExchangeConfig(id="bybit", market_type="swap")
    balance = {
        "total": {"USDT": 100},
        "info": {
            "result": {
                "list": [
                    {
                        "coin": [
                            {"coin": "USDT", "walletBalance": "100", "equity": "112"}
                        ]
                    },
                ]
            }
        },
    }
    adjustments = balance_equity_adjustments(exchange, balance)
    assert adjustments == {"USDT": 12}
    a = account(position(unrealizedPnl=12), adjustments=adjustments)
    result = apply_contract_portfolio(portfolio(), [a], {"USDT": 1})
    assert result["total_asset_value"] == 112
    balance["total"]["USDT"] = 112
    assert balance_equity_adjustments(exchange, balance) == {"USDT": 0}
    assert (
        balance_equity_adjustments(
            ExchangeConfig(id="binanceusdm", market_type="swap"), balance
        )
        == {}
    )


def test_merged_connection_replaces_runtime_snapshot_without_duplicate_positions():
    a = account(position(), key="workspace:connection:swap")
    a["workspace_connection_id"] = "connection"
    workspace = {
        "connections": [
            {
                "id": "connection",
                "exchange": "bybit",
                "label": "My account",
                "market_type": "swap",
                "status": "healthy",
                "checked_at": time.time(),
                "balances": a["balance"]["currencies"],
                "contract_snapshot": a["contract_snapshot"],
            }
        ]
    }
    balances = _merge_workspace_account_balances({"accounts": [a]}, workspace)
    assert len(balances["accounts"]) == 1
    result = _sync_portfolio_with_account_balances(
        portfolio(), balances, quote_rates={"USDT": 1}
    )
    assert result["contracts"]["position_count"] == 1
    assert result["contracts"]["positions"][0]["account"] == "My account"
    assert result["total_asset_value"] == 100
    # Rebuilding the same snapshot must not accumulate floating P/L.
    again = _sync_portfolio_with_account_balances(
        result, balances, quote_rates={"USDT": 1}
    )
    assert again["total_pnl"] == result["total_pnl"]


def test_existing_pnl_baseline_does_not_jump_when_contract_equity_is_first_added(
    tmp_path,
):
    store = AssetLedgerStore(
        AssetLedgerConfig(enabled=True, path=str(tmp_path / "assets.db"))
    )
    a = account(position())
    balances = {"accounts": [a]}
    old = store.apply_portfolio_performance(
        portfolio(), balances, scope_key="user:test", observed_at=1000
    )
    assert old["performance"]["since_inception"]["pnl"] == 0
    new = {
        **portfolio(),
        "total_asset_value": 120,
        "contract_equity_adjustments": {"one": 20},
    }
    updated = store.apply_portfolio_performance(
        new, balances, scope_key="user:test", observed_at=1040
    )
    assert updated["performance"]["since_inception"]["pnl"] == 0
    assert updated["performance"]["valuation_coverage_flow"] == 20
    later = {
        **portfolio(),
        "total_asset_value": 125,
        "contract_equity_adjustments": {"one": 25},
    }
    updated = store.apply_portfolio_performance(
        later, balances, scope_key="user:test", observed_at=1080
    )
    assert updated["performance"]["since_inception"]["pnl"] == 5


def test_api_connection_reads_all_positions_even_without_strategy_and_persists():
    class Manager:
        def __init__(self, **_):
            self.has = {"fetchPositions": True}

        def client(self, _):
            return self

        async def load_markets(self):
            return {
                "BTC/USDT:USDT": {
                    "symbol": "BTC/USDT:USDT",
                    "type": "swap",
                    "swap": True,
                }
            }

        async def fetch_balance(self, _):
            return {"USDT": {"total": 100, "free": 50, "used": 50}}

        async def fetch_positions(self, cfg, symbols=None):
            assert symbols is None
            return [position(), position(side="short", unrealizedPnl=-5)]

        async def fetch_open_orders(self):
            return []

        async def close(self):
            pass

    connection = UserApiConnection.from_dict(
        {
            "exchange": "binance",
            "market_types": ["swap"],
            "owner_email": "owner@example.com",
        }
    )
    check = asyncio.run(
        check_workspace_api_connection(
            api_connection=connection,
            credentials={"api_key": "test", "secret": "test"},
            manager_factory=Manager,
        )
    )
    assert check["contract_snapshot"]["status"] == "ok"
    assert len(check["contract_snapshot"]["positions"]) == 2
    restored = UserApiConnection.from_dict(
        {**connection.to_dict(), "contract_snapshot": check["contract_snapshot"]}
    )
    assert restored.contract_snapshot == check["contract_snapshot"]


def test_option_and_other_contract_scopes_are_not_counted():
    markets = {"BTC/USDT:USDT": {"type": "future"}}
    result = contract_snapshot(
        ExchangeConfig(id="bybit", market_type="swap"),
        [position(), position(option=True)],
        markets=markets,
    )
    assert result["positions"] == []


def test_only_passed_owner_accounts_are_used():
    existing = apply_contract_portfolio(
        portfolio(), [account(position(), key="admin")], {"USDT": 1}
    )
    result = apply_contract_portfolio(
        copy.deepcopy(existing),
        [account(position(unrealizedPnl=5), key="owner")],
        {"USDT": 1},
    )
    assert [row["exchange"] for row in result["contracts"]["positions"]] == ["owner"]
    assert result["contracts"]["unrealized_pnl"] == 5
