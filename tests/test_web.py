import unittest

from arbitrage_bot.config import (
    BotConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    SpotMarketConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.web import build_market_maker_payload, build_market_rows


class WebMonitorTest(unittest.TestCase):
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
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=10,
                price_band_pct=10.0,
                quote_per_level=1.0,
            ),
            spot_symbols=[],
            spot_markets=[],
            cash_and_carry_pairs=[],
            spot_exchanges=[],
            derivative_exchanges=[],
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


if __name__ == "__main__":
    unittest.main()
