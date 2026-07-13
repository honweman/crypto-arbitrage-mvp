from __future__ import annotations

import os
import tempfile
import unittest

from arbitrage_bot.config import StrategyTimelineConfig
from arbitrage_bot.strategy_timeline import (
    find_latest_strategy_timeline_entry,
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

    def test_finds_matching_event_outside_recent_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = StrategyTimelineConfig(
                enabled=True,
                path=os.path.join(tmp, "timeline.jsonl"),
                max_recent_events=1,
            )
            write_strategy_timeline_from_payload(
                cfg,
                {
                    "type": "cross_exchange_rebalance_execution",
                    "strategy": "cross_exchange_rebalance",
                    "mode": "live",
                    "status": "hedge_required",
                    "execution": {"fill_status": {"imbalance_base": -42.0}},
                },
                source="test",
            )
            for _ in range(3):
                write_strategy_timeline_from_payload(
                    cfg,
                    {
                        "type": "market_maker",
                        "strategy": "market_maker",
                        "mode": "live",
                        "status": "placed",
                    },
                    source="test",
                )

            entry = find_latest_strategy_timeline_entry(
                cfg,
                strategy="cross_exchange_rebalance",
                status="hedge_required",
            )

        self.assertIsNotNone(entry)
        self.assertEqual(entry.metrics["imbalance_base"], -42.0)

    def test_strategy_timeline_rotates_large_jsonl_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "timeline.jsonl")
            cfg = StrategyTimelineConfig(
                enabled=True,
                path=path,
                max_recent_events=10,
                rotate_max_bytes=1,
                rotate_keep_files=3,
                rotate_compress=False,
            )

            write_strategy_timeline_from_payload(
                cfg,
                {"type": "market_maker", "status": "placed"},
                source="test",
            )
            write_strategy_timeline_from_payload(
                cfg,
                {"type": "slow_execution", "status": "blocked_by_risk"},
                source="test",
            )
            entries = read_recent_strategy_timeline_entries(cfg)
            rotated = sorted(
                name for name in os.listdir(tmp) if name.startswith("timeline.jsonl.")
            )

        self.assertEqual([entry.event_type for entry in entries], ["slow_execution"])
        self.assertEqual(len(rotated), 1)

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

    def test_execution_reason_includes_emergency_cancel_errors(self) -> None:
        event = strategy_timeline_event_from_payload(
            {
                "type": "market_maker",
                "strategy": "market_maker",
                "mode": "live",
                "status": "execution_error",
                "plan": {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                },
                "execution": {
                    "placed_count": 1,
                    "partial_create": True,
                    "emergency_cancel": True,
                    "emergency_cancel_errors": [
                        {
                            "order_id": "mm-1",
                            "error": "cancel timeout",
                        }
                    ],
                },
            },
            source="test",
        )

        self.assertEqual(event["action"], "execution_error")
        self.assertIn("mm-1", event["reason"])
        self.assertIn("cancel timeout", event["reason"])

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
