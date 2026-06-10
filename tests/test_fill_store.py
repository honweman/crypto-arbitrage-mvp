import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.config import (
    BotConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PnlStoreConfig,
    PortfolioConfig,
    SlowExecutionConfig,
)
from arbitrage_bot.fill_store import load_daily_pnl_summary, persist_fill_pnl
from arbitrage_bot.risk import current_daily_pnl_quote


class FillStoreTest(unittest.TestCase):
    def test_persist_fill_pnl_deduplicates_and_summarizes_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = PnlStoreConfig(
                enabled=True,
                path=str(Path(temp_dir) / "fills.sqlite3"),
            )
            trade = {
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "id": "trade-1",
                "order_id": "order-1",
                "side": "sell",
                "source": "market_maker",
                "base_currency": "ACS",
                "quote_currency": "USDC",
                "price": 0.00015,
                "amount": 1_000.0,
                "cost": 0.15,
                "notional_common": 0.15,
                "fee": {"cost": 0.0001, "currency": "USDC"},
                "fee_common": 0.0001,
                "realized_pnl_common": 0.0499,
                "timestamp": 1_781_067_979_000,
            }

            first = persist_fill_pnl(cfg, [trade], currency="USD")
            second = persist_fill_pnl(cfg, [trade], currency="USD")
            daily = load_daily_pnl_summary(
                cfg,
                currency="USD",
                day=first["daily"]["day"],
            )

            self.assertTrue(first["enabled"])
            self.assertEqual(second["stored_fill_count"], 1)
            self.assertEqual(daily["trade_count"], 1)
            self.assertAlmostEqual(daily["total_realized_pnl"], 0.0499)
            self.assertAlmostEqual(
                daily["sources"]["market_maker"]["realized_pnl"],
                0.0499,
            )
            self.assertAlmostEqual(daily["total_fees"], 0.0001)

    def test_disabled_store_returns_empty_daily_summary(self) -> None:
        cfg = PnlStoreConfig(enabled=False, path="unused.sqlite3")

        payload = persist_fill_pnl(cfg, [], currency="USD")

        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["daily"]["trade_count"], 0)
        self.assertEqual(payload["daily"]["total_realized_pnl"], 0.0)

    def test_current_daily_pnl_quote_uses_store_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_cfg = PnlStoreConfig(
                enabled=True,
                path=str(Path(temp_dir) / "fills.sqlite3"),
            )
            persist_fill_pnl(
                store_cfg,
                [
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                        "id": "trade-loss",
                        "order_id": "order-loss",
                        "side": "buy",
                        "source": "auto_buy_sell",
                        "base_currency": "ACS",
                        "quote_currency": "USDC",
                        "price": 0.00015,
                        "amount": 1_000.0,
                        "cost": 0.15,
                        "notional_common": 0.15,
                        "fee": {"cost": 0.01, "currency": "USDC"},
                        "fee_common": 0.01,
                        "realized_pnl_common": -0.01,
                    }
                ],
                currency="USD",
            )
            cfg = BotConfig(
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
                market_maker=MarketMakerConfig(),
                slow_execution=SlowExecutionConfig(),
                portfolio=PortfolioConfig(
                    realized_pnl={"market_maker": 0.25},
                ),
                spot_symbols=[],
                spot_markets=[],
                cash_and_carry_pairs=[],
                spot_exchanges=[],
                derivative_exchanges=[],
                pnl_store=store_cfg,
            )

            self.assertAlmostEqual(current_daily_pnl_quote(cfg), 0.24)


if __name__ == "__main__":
    unittest.main()
