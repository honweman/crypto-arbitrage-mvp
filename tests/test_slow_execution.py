import unittest

from arbitrage_bot.config import SlowExecutionConfig
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.slow_execution import build_slow_execution_plan


class SlowExecutionTest(unittest.TestCase):
    def test_builds_midpoint_slice_order(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=90.0, amount=10.0)],
            asks=[BookLevel(price=110.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="sell",
            total_base=10.0,
            slice_base=2.0,
            interval_seconds=30.0,
        )

        plan = build_slow_execution_plan(book, cfg, submitted_base=4.0)

        self.assertEqual(plan.status, "planned")
        self.assertEqual(plan.mid_price, 100.0)
        self.assertEqual(plan.remaining_base, 6.0)
        self.assertIsNotNone(plan.order)
        self.assertEqual(plan.order.side, "sell")
        self.assertAlmostEqual(plan.order.price, 100.0)
        self.assertAlmostEqual(plan.order.amount, 2.0)
        self.assertAlmostEqual(plan.order.quote_notional, 200.0)
        self.assertAlmostEqual(plan.order.submitted_base_after, 6.0)

    def test_slice_quote_converts_to_base_amount(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=101.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_base=5.0,
            slice_quote=250.0,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertIsNotNone(plan.order)
        self.assertEqual(plan.order.side, "buy")
        self.assertAlmostEqual(plan.order.price, 100.0)
        self.assertAlmostEqual(plan.order.amount, 2.5)
        self.assertAlmostEqual(plan.order.quote_notional, 250.0)

    def test_complete_when_submitted_reaches_total(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=101.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_base=5.0,
            slice_base=1.0,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg, submitted_base=5.0)

        self.assertEqual(plan.status, "complete")
        self.assertIsNone(plan.order)
        self.assertEqual(plan.remaining_base, 0.0)

    def test_rejects_ambiguous_slice_units(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=101.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_base=5.0,
            slice_base=1.0,
            slice_quote=100.0,
            interval_seconds=60.0,
        )

        with self.assertRaisesRegex(ValueError, "only one"):
            build_slow_execution_plan(book, cfg)


if __name__ == "__main__":
    unittest.main()
