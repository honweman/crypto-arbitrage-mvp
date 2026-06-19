import unittest

from arbitrage_bot.config import ExecutionAlgoConfig
from arbitrage_bot.execution_algos import build_execution_algo_plan
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


def book(bid: float = 99.0, ask: float = 101.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange="coinbase-spot",
        symbol="ACS/USDC",
        bids=[
            BookLevel(price=bid, amount=100.0),
            BookLevel(price=bid - 1, amount=50.0),
        ],
        asks=[
            BookLevel(price=ask, amount=100.0),
            BookLevel(price=ask + 1, amount=50.0),
        ],
    )


class ExecutionAlgoTest(unittest.TestCase):
    def test_twap_builds_equal_slices_at_taker_price(self) -> None:
        plan = build_execution_algo_plan(
            book(),
            ExecutionAlgoConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                side="buy",
                algo="twap",
                total_quote=120.0,
                slice_count=3,
                duration_seconds=900.0,
                interval_seconds=300.0,
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.next_slice.slice_index, 1)
        self.assertEqual([item.quote_notional for item in plan.schedule], [40.0, 40.0, 40.0])
        self.assertEqual(plan.execution_price, 101.0)

    def test_vwap_uses_weighted_schedule(self) -> None:
        plan = build_execution_algo_plan(
            book(),
            ExecutionAlgoConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                side="sell",
                algo="vwap",
                total_quote=100.0,
                slice_count=5,
                duration_seconds=500.0,
                interval_seconds=100.0,
            ),
        )

        quotes = [item.quote_notional for item in plan.schedule]
        self.assertEqual(plan.execution_price, 99.0)
        self.assertGreater(quotes[0], quotes[2])
        self.assertGreater(quotes[-1], quotes[2])
        self.assertAlmostEqual(sum(quotes), 100.0)

    def test_pov_caps_each_slice_by_participation(self) -> None:
        plan = build_execution_algo_plan(
            book(),
            ExecutionAlgoConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                side="buy",
                algo="pov",
                total_quote=100.0,
                slice_count=3,
                duration_seconds=300.0,
                interval_seconds=100.0,
                participation_rate=0.001,
            ),
        )

        self.assertGreater(plan.schedule[0].expected_market_volume_quote, 0.0)
        self.assertLess(plan.schedule[0].quote_notional, 100.0)
        self.assertAlmostEqual(plan.schedule[0].participation_rate, 0.001)

    def test_price_gates_wait_and_stop(self) -> None:
        waiting = build_execution_algo_plan(
            book(),
            ExecutionAlgoConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                side="buy",
                total_quote=10.0,
                start_price=100.0,
            ),
        )
        stopped = build_execution_algo_plan(
            book(),
            ExecutionAlgoConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                side="sell",
                total_quote=10.0,
                stop_price=100.0,
            ),
        )

        self.assertEqual(waiting.status, "waiting_for_start")
        self.assertIsNone(waiting.next_slice)
        self.assertEqual(stopped.status, "stopped_by_price")


if __name__ == "__main__":
    unittest.main()
