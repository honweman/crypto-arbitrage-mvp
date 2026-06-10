from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from arbitrage_bot.config import (
    AssetPosition,
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
    TradeLogConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.pnl import build_portfolio_pnl
from arbitrage_bot.web import (
    HTML,
    MonitorState,
    _slow_execution_overrides_from_payload,
    build_market_maker_payload,
    build_market_rows,
    build_operations_payload,
    build_slow_execution_payload,
    fetch_account_balances_payload,
    slow_execution_accounts,
)


def make_config(
    *,
    market_maker: MarketMakerConfig | None = None,
    slow_execution: SlowExecutionConfig | None = None,
    portfolio: PortfolioConfig | None = None,
    spot_markets: list[SpotMarketConfig] | None = None,
    spot_exchanges: list[ExchangeConfig] | None = None,
    trade_log: TradeLogConfig | None = None,
) -> BotConfig:
    return BotConfig(
        poll_seconds=1.0,
        order_book_depth=20,
        notional_quote=200.0,
        min_profit_quote=0.1,
        min_profit_bps=1.0,
        min_basis_bps=15.0,
        common_quote_currency="USD",
        quote_rates={"USD": 1.0},
        quote_rate_sources=[],
        onchain_monitor=OnchainMonitorConfig(),
        market_maker=market_maker or MarketMakerConfig(),
        slow_execution=slow_execution or SlowExecutionConfig(),
        portfolio=portfolio or PortfolioConfig(),
        spot_symbols=[],
        spot_markets=spot_markets or [],
        cash_and_carry_pairs=[],
        spot_exchanges=spot_exchanges or [],
        derivative_exchanges=[],
        trade_log=trade_log or TradeLogConfig(enabled=False),
    )


class WebMonitorTest(unittest.TestCase):
    def test_page_uses_auto_buy_sell_label(self) -> None:
        self.assertIn("Auto Buy/Sell", HTML)
        self.assertIn("/api/auto-buy-sell", HTML)
        self.assertNotIn("Slow Execution", HTML)

    def test_page_includes_account_balances(self) -> None:
        self.assertIn("Account Balances", HTML)
        self.assertIn('id="account-balances"', HTML)

    def test_build_market_rows_converts_top_of_book(self) -> None:
        markets = [
            SpotMarketConfig(
                asset="ACS",
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                quote_currency="KRW",
            )
        ]
        books = {
            ("bithumb-spot", "ACS/KRW"): OrderBookSnapshot(
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                bids=[BookLevel(price=0.20, amount=100_000)],
                asks=[BookLevel(price=0.21, amount=90_000)],
            )
        }

        rows = build_market_rows(markets, books, {"KRW": 0.00075})

        self.assertEqual(rows[0]["status"], "ok")
        self.assertAlmostEqual(rows[0]["bid_common"], 0.00015)
        self.assertAlmostEqual(rows[0]["ask_common"], 0.0001575)

    def test_build_market_maker_payload_returns_plan(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=10,
                price_band_pct=10.0,
                quote_per_level=1.0,
            )
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }

        payload = build_market_maker_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(len(payload["plan"]["orders"]), 20)

    def test_build_slow_execution_payload_returns_midpoint_order(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                side="sell",
                total_base=10_000.0,
                slice_base=1_000.0,
                interval_seconds=30.0,
            )
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }

        payload = build_slow_execution_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["plan"]["side"], "sell")
        self.assertAlmostEqual(payload["plan"]["mid_price"], 0.00015)
        self.assertAlmostEqual(payload["plan"]["order"]["amount"], 1_000.0)
        self.assertAlmostEqual(payload["plan"]["order"]["quote_notional"], 0.15)

    def test_slow_execution_payload_uses_range_config(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                side="buy",
                total_base=10_000.0,
                slice_base_min=1_000.0,
                slice_base_max=2_000.0,
                randomize_slice=False,
                interval_seconds=30.0,
                order_ttl_seconds=5.0,
                stop_price=0.001,
            )
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }

        payload = build_slow_execution_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["config"]["slice_base_min"], 1_000.0)
        self.assertEqual(payload["config"]["slice_base_max"], 2_000.0)
        self.assertEqual(payload["config"]["order_ttl_seconds"], 5.0)
        self.assertEqual(payload["plan"]["order"]["amount"], 1_000.0)

    def test_slow_execution_payload_includes_configured_accounts(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(exchange="bybit-spot"),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
        )

        payload = build_slow_execution_payload(cfg, {})

        self.assertEqual(
            payload["accounts"],
            [
                {
                    "key": "bybit-spot",
                    "label": "bybit-spot",
                    "id": "bybit",
                    "market_type": "spot",
                },
                {
                    "key": "coinbase-spot",
                    "label": "coinbase-spot",
                    "id": "coinbase",
                    "market_type": "spot",
                },
            ],
        )

    def test_slow_execution_accounts_uses_key_fallback(self) -> None:
        accounts = slow_execution_accounts([ExchangeConfig(id="bybit")])

        self.assertEqual(accounts[0]["key"], "bybit:spot")
        self.assertEqual(accounts[0]["label"], "bybit:spot")

    def test_slow_execution_update_payload_is_sanitized(self) -> None:
        overrides = _slow_execution_overrides_from_payload(
            {
                "enabled": True,
                "exchange": "bybit-spot",
                "side": "buy",
                "total_base": "1000",
                "slice_base_min": "10",
                "slice_base_max": "20",
                "randomize_slice": True,
                "interval_seconds": "5",
                "order_ttl_seconds": "2",
                "stop_price": "0.01",
            },
            allowed_exchanges={"bybit-spot"},
        )

        self.assertTrue(overrides["enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["side"], "buy")
        self.assertEqual(overrides["slice_base"], 0.0)
        self.assertEqual(overrides["slice_quote"], 0.0)
        self.assertEqual(overrides["slice_base_min"], 10.0)
        self.assertEqual(overrides["slice_base_max"], 20.0)
        self.assertTrue(overrides["randomize_slice"])

    def test_slow_execution_update_payload_rejects_unknown_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown exchange account"):
            _slow_execution_overrides_from_payload(
                {"exchange": "coinbase-spot"},
                allowed_exchanges={"bybit-spot"},
            )

    def test_operations_payload_includes_risk_and_recent_events(self) -> None:
        payload = build_operations_payload(make_config())

        self.assertIn("risk", payload)
        self.assertIn("trade_log", payload)
        self.assertIn("alerts", payload)
        self.assertFalse(payload["risk"]["allow_live_trading"])
        self.assertEqual(payload["trade_log"]["recent_events"], [])
        self.assertEqual(payload["trade_log"]["recent_entries"], [])
        self.assertEqual(payload["trade_log"]["summary"]["event_count"], 0)

    def test_build_portfolio_pnl_splits_sources(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                asset="ACS",
                position_base=10_000.0,
                average_entry_price=0.00010,
                cash_balances={"USDC": 10.0, "USDT": 20.0, "KRW": 10_000.0},
                realized_pnl={"market_maker": 1.25, "arbitrage": 2.50},
            ),
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }

        payload = build_portfolio_pnl(
            cfg,
            books,
            {"USDC": 1.0, "USDT": 1.0, "KRW": 0.00075},
        )

        self.assertEqual(payload["status"], "ok")
        self.assertAlmostEqual(payload["mark_price"], 0.00015)
        self.assertEqual(payload["positions"][0]["asset"], "ACS")
        self.assertAlmostEqual(payload["cash_balances_common"]["USDC"], 10.0)
        self.assertAlmostEqual(payload["cash_balances_common"]["USDT"], 20.0)
        self.assertAlmostEqual(payload["cash_balances_common"]["KRW"], 7.5)
        self.assertAlmostEqual(payload["cash_value"], 37.5)
        self.assertAlmostEqual(payload["sources"]["price_move"], 0.5)
        self.assertAlmostEqual(payload["sources"]["market_maker"], 1.25)
        self.assertAlmostEqual(payload["sources"]["arbitrage"], 2.5)
        self.assertAlmostEqual(payload["total_pnl"], 4.25)

    def test_build_portfolio_pnl_sums_multiple_assets(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                positions=[
                    AssetPosition(
                        asset="ACS",
                        position_base=10_000.0,
                        average_entry_price=0.00010,
                    ),
                    AssetPosition(
                        asset="XYZ",
                        position_base=2.0,
                        average_entry_price=2.0,
                    ),
                ],
                realized_pnl={"market_maker": 1.0, "arbitrage": 2.0},
            ),
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
                SpotMarketConfig(
                    asset="XYZ",
                    exchange="bybit-spot",
                    symbol="XYZ/USDT",
                    quote_currency="USDT",
                ),
            ],
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            ),
            ("bybit-spot", "XYZ/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="XYZ/USDT",
                bids=[BookLevel(price=2.9, amount=10)],
                asks=[BookLevel(price=3.1, amount=10)],
            ),
        }

        payload = build_portfolio_pnl(cfg, books, {"USDT": 1.0})

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["positions"]), 2)
        self.assertAlmostEqual(payload["positions"][0]["position_value"], 1.5)
        self.assertAlmostEqual(payload["positions"][1]["position_value"], 6.0)
        self.assertAlmostEqual(payload["position_value"], 7.5)
        self.assertAlmostEqual(payload["sources"]["price_move"], 2.5)
        self.assertAlmostEqual(payload["total_pnl"], 5.5)

    def test_build_portfolio_pnl_reports_missing_cash_rates(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                asset="ACS",
                cash_balances={"EUR": 100.0, "USDT": 5.0},
            )
        )

        payload = build_portfolio_pnl(cfg, {}, {"USDT": 1.0})

        self.assertEqual(payload["cash_missing_rates"], ["EUR"])
        self.assertAlmostEqual(payload["cash_value"], 5.0)


class WebMonitorStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_account_balances_payload_summarizes_totals(self) -> None:
        class FakeBalanceManager:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                self.calls += 1
                return {
                    "free": {"ACS": 1000.0, "USDT": 20.0},
                    "used": {"ACS": 0.0, "USDT": 1.0},
                    "total": {"ACS": 1000.0, "USDT": 21.0},
                }

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
        )
        manager = FakeBalanceManager()

        with patch.dict(
            os.environ,
            {"BYBIT_API_KEY": "key", "BYBIT_SECRET": "secret"},
            clear=True,
        ):
            payload = await fetch_account_balances_payload(cfg, manager)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checked_account_count"], 1)
        self.assertEqual(manager.calls, 1)
        totals = {row["currency"]: row for row in payload["totals"]}
        self.assertEqual(totals["ACS"]["total"], 1000.0)
        self.assertEqual(totals["USDT"]["free"], 20.0)
        self.assertEqual(payload["accounts"][0]["status"], "ok")

    async def test_fetch_account_balances_skips_missing_api_env(self) -> None:
        class FakeBalanceManager:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                self.calls += 1
                return {}

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
        )
        manager = FakeBalanceManager()

        with patch.dict(os.environ, {}, clear=True):
            payload = await fetch_account_balances_payload(cfg, manager)

        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["checked_account_count"], 0)
        self.assertEqual(manager.calls, 0)
        self.assertEqual(
            payload["accounts"][0]["balance"]["skipped_reason"],
            "api env vars missing",
        )

    async def test_program_switch_updates_running_state(self) -> None:
        state = MonitorState(make_config(), 1.0)

        paused = await state.set_running(False)
        self.assertFalse(await state.is_running())
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(paused["program"]["running"])

        resumed = await state.set_running(True)
        self.assertTrue(await state.is_running())
        self.assertEqual(resumed["status"], "starting")
        self.assertTrue(resumed["program"]["running"])

if __name__ == "__main__":
    unittest.main()
