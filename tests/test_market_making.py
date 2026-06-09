import unittest

from arbitrage_bot.config import MarketMakerConfig
from arbitrage_bot.market_making import build_symmetric_market_maker_plan
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


class MarketMakingTest(unittest.TestCase):
    def test_builds_symmetric_quote_depth_around_mid(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=90.0, amount=10.0)],
            asks=[BookLevel(price=110.0, amount=10.0)],
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=2,
            price_band_pct=10.0,
            quote_per_level=100.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)

        self.assertEqual(plan.mid_price, 100.0)
        self.assertEqual(len(plan.orders), 4)
        self.assertEqual(plan.orders[0].side, "buy")
        self.assertAlmostEqual(plan.orders[0].price, 95.0)
        self.assertAlmostEqual(plan.orders[0].amount, 100.0 / 95.0)
        self.assertEqual(plan.orders[1].side, "sell")
        self.assertAlmostEqual(plan.orders[1].price, 105.0)
        self.assertAlmostEqual(plan.orders[2].price, 90.0)
        self.assertAlmostEqual(plan.orders[3].price, 110.0)

    def test_min_distance_filters_inner_levels(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=101.0, amount=10.0)],
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=10,
            price_band_pct=10.0,
            quote_per_level=10.0,
            min_distance_bps=500.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)

        self.assertEqual(len(plan.orders), 12)
        self.assertEqual(plan.orders[0].level, 5)
        self.assertAlmostEqual(plan.orders[0].distance_bps, 500.0)


if __name__ == "__main__":
    unittest.main()
