import unittest

from arbitrage_bot.config import DcaConfig, SpotGridConfig
from arbitrage_bot.grid_trading import build_dca_plan, build_spot_grid_plan
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


def book(bid: float = 99.0, ask: float = 101.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange="bybit-spot",
        symbol="ACS/USDT",
        bids=[
            BookLevel(price=bid, amount=100.0),
            BookLevel(price=bid - 1, amount=100.0),
        ],
        asks=[
            BookLevel(price=ask, amount=100.0),
            BookLevel(price=ask + 1, amount=100.0),
        ],
    )


class GridTradingTest(unittest.TestCase):
    def test_spot_grid_builds_buy_and_sell_orders_around_mid(self) -> None:
        cfg = SpotGridConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            lower_price=90.0,
            upper_price=110.0,
            grid_count=4,
            quote_per_grid=10.0,
            max_open_orders=3,
            min_grid_step_bps=1.0,
        )

        plan = build_spot_grid_plan(book(), cfg)

        self.assertEqual(plan.status, "planned")
        self.assertEqual(len(plan.orders), 3)
        self.assertEqual(plan.orders[0].price, 95.0)
        self.assertEqual(plan.orders[0].side, "buy")
        self.assertEqual(plan.orders[1].price, 105.0)
        self.assertEqual(plan.orders[1].side, "sell")
        self.assertAlmostEqual(plan.orders[0].amount, 10.0 / 95.0)

    def test_spot_grid_blocks_price_outside_range_and_tight_step(self) -> None:
        below = build_spot_grid_plan(
            book(bid=79.0, ask=81.0),
            SpotGridConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                lower_price=90.0,
                upper_price=110.0,
                grid_count=4,
                quote_per_grid=10.0,
            ),
        )
        tight = build_spot_grid_plan(
            book(),
            SpotGridConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                lower_price=99.0,
                upper_price=101.0,
                grid_count=10,
                quote_per_grid=10.0,
                min_grid_step_bps=50.0,
            ),
        )

        self.assertEqual(below.status, "below_range")
        self.assertEqual(below.orders, [])
        self.assertEqual(tight.status, "blocked_by_min_grid_step")

    def test_spot_grid_supports_geometric_spacing(self) -> None:
        plan = build_spot_grid_plan(
            book(),
            SpotGridConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                lower_price=80.0,
                upper_price=125.0,
                grid_count=2,
                spacing="geometric",
                quote_per_grid=10.0,
            ),
        )

        self.assertEqual(plan.status, "planned")
        self.assertGreater(plan.grid_step_bps, 0.0)
        self.assertTrue(any(order.side == "buy" for order in plan.orders))
        self.assertTrue(any(order.side == "sell" for order in plan.orders))

    def test_dca_waits_for_buy_trigger_then_builds_multiplier_schedule(self) -> None:
        cfg = DcaConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            trigger_price=98.0,
            quote_per_order=10.0,
            size_multiplier=2.0,
            max_orders=3,
            interval_seconds=60.0,
        )

        waiting = build_dca_plan(book(), cfg)
        ready = build_dca_plan(book(bid=95.0, ask=97.0), cfg)

        self.assertEqual(waiting.status, "waiting_for_trigger")
        self.assertIsNone(waiting.next_order)
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.next_order.side, "buy")
        self.assertAlmostEqual(ready.next_order.quote_notional, 10.0)
        self.assertEqual(
            [row["quote_notional"] for row in ready.order_schedule],
            [10.0, 20.0, 40.0],
        )

    def test_dca_sell_trigger_uses_bid_price(self) -> None:
        plan = build_dca_plan(
            book(bid=105.0, ask=106.0),
            DcaConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                side="sell",
                trigger_price=104.0,
                quote_per_order=12.0,
                max_orders=2,
                interval_seconds=60.0,
                price_mode="taker",
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.next_order.side, "sell")
        self.assertEqual(plan.next_order.price, 105.0)


if __name__ == "__main__":
    unittest.main()
