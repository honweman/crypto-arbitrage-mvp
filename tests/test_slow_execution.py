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

    def test_maker_sell_order_uses_best_ask_plus_offset(self) -> None:
        book = OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.31, amount=10_000_000.0)],
            asks=[BookLevel(price=0.311, amount=10_000_000.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            side="sell",
            total_base=1_000_000.0,
            slice_base=100_000.0,
            interval_seconds=10.0,
            start_price=0.31,
            stop_price=0.3,
            price_mode="maker",
            price_offset_bps=1.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "planned")
        self.assertEqual(plan.price_mode, "maker")
        self.assertAlmostEqual(plan.trigger_price, 0.31)
        self.assertIsNotNone(plan.order)
        self.assertAlmostEqual(plan.order.price, 0.3110311)

    def test_unlimited_top_level_sell_uses_best_ask_amount(self) -> None:
        book = OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.31, amount=250_000.0)],
            asks=[BookLevel(price=0.311, amount=123_456.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            side="sell",
            unlimited_total=True,
            slice_mode="top_level",
            interval_seconds=10.0,
            start_price=0.31,
            stop_price=0.3,
            price_mode="maker",
            price_offset_bps=1.0,
        )

        plan = build_slow_execution_plan(book, cfg)
        payload = plan.to_dict()

        self.assertEqual(plan.status, "planned")
        self.assertEqual(plan.progress_mode, "unlimited")
        self.assertTrue(plan.unlimited_total)
        self.assertEqual(plan.slice_mode, "top_level")
        self.assertIsNotNone(plan.order)
        self.assertAlmostEqual(plan.order.amount, 123_456.0)
        self.assertIsNone(payload["remaining_base"])
        self.assertIsNone(payload["remaining_quote"])

    def test_maker_sell_start_and_stop_use_bid_not_offset_ask(self) -> None:
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            side="sell",
            total_base=1_000_000.0,
            slice_base=100_000.0,
            interval_seconds=10.0,
            start_price=0.31,
            stop_price=0.3,
            price_mode="maker",
            price_offset_bps=1.0,
        )
        waiting_book = OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.3099, amount=10_000_000.0)],
            asks=[BookLevel(price=0.31, amount=10_000_000.0)],
        )
        stopped_book = OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.3, amount=10_000_000.0)],
            asks=[BookLevel(price=0.301, amount=10_000_000.0)],
        )

        waiting = build_slow_execution_plan(waiting_book, cfg)
        stopped = build_slow_execution_plan(
            stopped_book,
            cfg,
            start_price_triggered=True,
        )

        self.assertEqual(waiting.status, "waiting_for_start_price")
        self.assertEqual(stopped.status, "stopped_by_price")

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

    def test_start_price_waits_for_sell_trigger(self) -> None:
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
            start_price=100.0,
            stop_price=95.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "waiting_for_start_price")
        self.assertIsNone(plan.order)
        self.assertEqual(plan.start_price, 100.0)

    def test_start_price_allows_sell_at_or_above_trigger(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=100.0, amount=10.0)],
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
            start_price=100.0,
            stop_price=95.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "planned")
        self.assertIsNotNone(plan.order)
        self.assertEqual(plan.order.price, 100.0)

    def test_triggered_sell_continues_below_start_until_stop(self) -> None:
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
            start_price=100.0,
            stop_price=95.0,
        )

        plan = build_slow_execution_plan(book, cfg, start_price_triggered=True)

        self.assertEqual(plan.status, "planned")
        self.assertIsNotNone(plan.order)
        self.assertEqual(plan.order.price, 99.0)

    def test_triggered_sell_stops_below_stop_price(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=94.0, amount=10.0)],
            asks=[BookLevel(price=95.0, amount=10.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="sell",
            total_base=10.0,
            slice_base=1.0,
            interval_seconds=60.0,
            start_price=100.0,
            stop_price=95.0,
        )

        plan = build_slow_execution_plan(book, cfg, start_price_triggered=True)

        self.assertEqual(plan.status, "stopped_by_price")
        self.assertIsNone(plan.order)

    def test_stop_price_blocks_buy_at_or_above_ceiling(self) -> None:
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

    def test_buy_stop_price_takes_priority_over_start_price(self) -> None:
        book = OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.23, amount=10_000_000.0)],
            asks=[BookLevel(price=0.231, amount=10_000_000.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            side="buy",
            total_base=10.0,
            slice_base=1.0,
            interval_seconds=60.0,
            start_price=0.225,
            stop_price=0.23,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "stopped_by_price")
        self.assertIsNone(plan.order)

    def test_buy_waits_above_start_below_stop_before_start_trigger(self) -> None:
        book = OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.227, amount=10_000_000.0)],
            asks=[BookLevel(price=0.228, amount=10_000_000.0)],
        )
        cfg = SlowExecutionConfig(
            enabled=True,
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            side="buy",
            total_base=10.0,
            slice_base=1.0,
            interval_seconds=60.0,
            start_price=0.225,
            stop_price=0.23,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "waiting_for_start_price")
        self.assertIsNone(plan.order)

    def test_start_price_waits_for_buy_trigger(self) -> None:
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
            start_price=100.0,
        )

        plan = build_slow_execution_plan(book, cfg)

        self.assertEqual(plan.status, "waiting_for_start_price")
        self.assertIsNone(plan.order)


if __name__ == "__main__":
    unittest.main()
