from __future__ import annotations

import unittest
from types import SimpleNamespace

from arbitrage_bot.strategy_performance import build_strategy_performance_payload


class StrategyPerformanceTest(unittest.TestCase):
    def test_metrics_are_grouped_by_instance_with_fill_slippage_and_latency(
        self,
    ) -> None:
        event = SimpleNamespace(
            strategy="market_maker",
            strategy_instance_id="coinbase-acs",
            event_id="event-1",
            mode="live",
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            status="placed",
            placed_order_ids=["order-1", "order-2"],
            placed_count=2,
            raw={
                "plan": {
                    "orders": [
                        {
                            "exchange": "coinbase-spot",
                            "symbol": "ACS/USDC",
                            "side": "buy",
                            "price": 0.10,
                        }
                    ]
                },
                "execution": {"opportunity_to_submit_ms": 18.0},
            },
        )
        fill = {
            "exchange": "coinbase-spot",
            "symbol": "ACS/USDC",
            "side": "buy",
            "order_id": "order-1",
            "price": 0.101,
            "amount": 100.0,
            "notional_common": 10.1,
            "fee_common": 0.02,
            "realized_pnl_common": 0.3,
            "strategy_instance_id": "coinbase-acs",
            "attribution": {
                "strategy": "market_maker",
                "strategy_instance_id": "coinbase-acs",
                "event_id": "event-1",
            },
        }

        payload = build_strategy_performance_payload(
            [event],
            [fill],
            currency="USD",
        )
        row = payload["rows"][0]

        self.assertEqual(row["instance_id"], "coinbase-acs")
        self.assertEqual(row["submitted_order_count"], 2)
        self.assertEqual(row["filled_order_count"], 1)
        self.assertEqual(row["fill_rate_pct"], 50.0)
        self.assertAlmostEqual(row["average_fill_price"], 0.101)
        self.assertAlmostEqual(row["average_slippage_bps"], 100.0)
        self.assertEqual(row["average_submit_latency_ms"], 18.0)
        self.assertEqual(payload["summary"]["fill_count"], 1)

    def test_auto_task_progress_and_paper_live_delta_are_reported(self) -> None:
        paper_event = SimpleNamespace(
            strategy="spot_spread",
            strategy_instance_id="default",
            event_id="paper-1",
            mode="dry_run",
            exchange="",
            symbol="ACS",
            status="planned",
            placed_order_ids=[],
            placed_count=0,
            raw={
                "paper_execution": {
                    "order_count": 2,
                    "estimated_profit_quote": 1.25,
                }
            },
        )
        payload = build_strategy_performance_payload(
            [paper_event],
            [],
            currency="USD",
            auto_buy_sell_tasks={
                "tasks": [
                    {
                        "id": "auto-1",
                        "progress_pct": 40.0,
                        "progress_mode": "quote",
                        "filled_base": 200.0,
                        "filled_quote": 20.0,
                        "config": {
                            "exchange": "bithumb-spot",
                            "symbol": "ACS/KRW",
                        },
                    }
                ]
            },
        )
        rows = {(row["strategy"], row["instance_id"]): row for row in payload["rows"]}

        self.assertEqual(rows[("slow_execution", "auto-1")]["progress_pct"], 40.0)
        self.assertAlmostEqual(
            rows[("slow_execution", "auto-1")]["task_average_fill_price"],
            0.1,
        )
        self.assertEqual(rows[("spot_spread", "default")]["paper_vs_live_delta"], -1.25)
