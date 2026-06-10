import unittest

from arbitrage_bot.config import SlowExecutionConfig
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.slow_execution import build_slow_execution_plan


class SlowExecutionTest(unittest.TestCase):
    def test_sell_order_uses_best_bid(self) -> None:
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
        self.assertAlmostEqual(plan.order.price, 90.0)
        self.assertAlmostEqual(plan.order.amount, 2.0)
        self.assertAlmostEqual(plan.order.quote_notional, 180.0)
        self.assertAlmostEqual(plan.order.submitted_base_after, 6.0)

    def test_buy_order_uses_best_ask_and_slice_quote_converts_at_order_price(self) -> None:
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
        self.assertAlmostEqual(plan.order.price, 101.0)
        self.assertAlmostEqual(plan.order.amount, 250.0 / 101.0)
        self.assertAlmostEqual(plan.order.quote_notional, 250.0)

    def test_total_quote_caps_last_order(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=100.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_base=10.0,
            total_quote=250.0,
            slice_base=10.0,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg, submitted_quote=200.0)

        self.assertEqual(plan.progress_mode, "quote")
        self.assertEqual(plan.remaining_quote, 50.0)
        self.assertIsNotNone(plan.order)
        self.assertAlmostEqual(plan.order.amount, 0.5)
        self.assertAlmostEqual(plan.order.quote_notional, 50.0)
        self.assertAlmostEqual(plan.order.submitted_quote_before, 200.0)
        self.assertAlmostEqual(plan.order.submitted_quote_after, 250.0)

    def test_total_quote_only_is_valid_target(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=100.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_quote=250.0,
            slice_quote=100.0,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.progress_mode, "quote")
        self.assertEqual(plan.total_base, 0.0)
        self.assertAlmostEqual(plan.remaining_base, 2.5)
        self.assertIsNotNone(plan.order)
        self.assertAlmostEqual(plan.order.amount, 1.0)
        self.assertAlmostEqual(plan.order.quote_notional, 100.0)

    def test_complete_when_submitted_quote_reaches_total_quote(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=100.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_quote=250.0,
            slice_quote=100.0,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg, submitted_quote=250.0)

        self.assertEqual(plan.status, "complete")
        self.assertEqual(plan.remaining_quote, 0.0)
        self.assertIsNone(plan.order)

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

        with self.assertRaisesRegex(ValueError, "exactly one"):
            build_slow_execution_plan(book, cfg)

    def test_randomized_slice_uses_configured_range(self) -> None:
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
            side="sell",
            total_base=10.0,
            slice_base_min=2.0,
            slice_base_max=6.0,
            randomize_slice=True,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg, random_fn=lambda: 0.25)

        self.assertIsNotNone(plan.order)
        self.assertAlmostEqual(plan.order.amount, 3.0)
        self.assertTrue(plan.randomize_slice)
        self.assertEqual(plan.slice_base_min, 2.0)
        self.assertEqual(plan.slice_base_max, 6.0)

    def test_fixed_range_uses_minimum_slice(self) -> None:
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
            total_base=10.0,
            slice_base_min=2.0,
            slice_base_max=6.0,
            randomize_slice=False,
            interval_seconds=60.0,
        )

        plan = build_slow_execution_plan(book, cfg, random_fn=lambda: 1.0)

        self.assertIsNotNone(plan.order)
        self.assertAlmostEqual(plan.order.amount, 2.0)

    def test_stop_price_blocks_sell_below_floor(self) -> None:
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
            side="sell",
            total_base=10.0,
            slice_base=1.0,
            interval_seconds=60.0,
            stop_price=100.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "stopped_by_price")
        self.assertIsNone(plan.order)

    def test_stop_price_blocks_buy_above_ceiling(self) -> None:
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
            total_base=10.0,
            slice_base=1.0,
            interval_seconds=60.0,
            stop_price=100.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "stopped_by_price")
        self.assertIsNone(plan.order)


if __name__ == "__main__":
    unittest.main()
