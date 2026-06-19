from __future__ import annotations

import os
import tempfile
import unittest

from arbitrage_bot.config import StrategyTimelineConfig
from arbitrage_bot.strategy_timeline import (
    read_recent_strategy_timeline_entries,
    strategy_timeline_event_from_payload,
    summarize_strategy_timeline_entries,
    write_strategy_timeline_from_payload,
)


class StrategyTimelineTest(unittest.TestCase):
    def test_extracts_blocked_reason_accounts_symbols_and_metrics(self) -> None:
        event = strategy_timeline_event_from_payload(
            {
                "type": "spot_spread_execution",
                "strategy": "spot_spread",
                "mode": "live",
                "status": "blocked_by_risk",
                "opportunity": {
                    "profit_quote": 1.25,
                    "profit_bps": 42.0,
                },
                "plan": {
                    "exchange": "multi",
                    "symbol": "ACS",
                    "orders": [
                        {
                            "exchange": "coinbase-spot",
                            "symbol": "ACS/USDC",
                            "side": "buy",
                            "slippage_bps": 12.5,
                        }
                    ],
                },
                "risk": {
                    "reasons": [
                        "coinbase-spot ACS/USDC slippage 12.50 bps exceeds max_slippage_bps 10.00"
                    ],
                    "warnings": ["open order count unavailable"],
                    "total_quote_notional": 5.0,
                },
                "timing": {"opportunity_age_ms": 123.0},
            },
            source="test",
        )

        self.assertEqual(event["action"], "blocked")
        self.assertEqual(event["accounts"], ["coinbase-spot"])
        self.assertEqual(event["symbols"], ["ACS", "ACS/USDC"])
        self.assertIn("slippage", event["reason"])
        self.assertEqual(event["metrics"]["profit_quote"], 1.25)
        self.assertEqual(event["metrics"]["max_slippage_bps"], 12.5)
        self.assertEqual(event["metrics"]["total_quote_notional"], 5.0)
        self.assertEqual(event["source"], "test")
        self.assertEqual(len(event["risk_triggers"]), 1)

    def test_persists_and_summarizes_recent_timeline_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = StrategyTimelineConfig(
                enabled=True,
                path=os.path.join(tmp, "timeline.jsonl"),
                max_recent_events=10,
            )
            write_strategy_timeline_from_payload(
                cfg,
                {
                    "type": "market_maker",
                    "strategy": "market_maker",
                    "mode": "live",
                    "status": "placed",
                    "plan": {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                    },
                    "execution": {"placed_count": 1},
                },
                source="test",
            )
            write_strategy_timeline_from_payload(
                cfg,
                {
                    "type": "spot_spread_execution",
                    "strategy": "spot_spread",
                    "mode": "live",
                    "status": "no_opportunity",
                },
                source="test",
            )

            entries = read_recent_strategy_timeline_entries(cfg)
            summary = summarize_strategy_timeline_entries(entries)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].action, "no_order")
        self.assertEqual(entries[1].action, "place")
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["no_order_count"], 1)

    def test_cancel_payload_without_status_is_still_cancel_action(self) -> None:
        event = strategy_timeline_event_from_payload(
            {
                "type": "market_maker_cancel",
                "strategy": "market_maker",
                "mode": "live",
                "cancel_reason": "replace_existing",
                "plan": {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                },
            },
            source="test",
        )

        self.assertEqual(event["action"], "cancel")
        self.assertEqual(event["reason"], "cancel: replace_existing")

    def test_event_type_is_strategy_fallback(self) -> None:
        event = strategy_timeline_event_from_payload(
            {
                "type": "slow_execution",
                "mode": "live",
                "status": "waiting_for_start_price",
            },
            source="test",
        )

        self.assertEqual(event["strategy"], "slow_execution")


if __name__ == "__main__":
    unittest.main()
