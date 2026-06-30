from __future__ import annotations

import unittest

from arbitrage_bot.config import RiskConfig
from arbitrage_bot.execution_protection import (
    build_multileg_execution_protection,
    summarize_multileg_execution_protections,
)


class ExecutionProtectionTest(unittest.TestCase):
    def test_balanced_spot_pair_models_one_leg_hedge(self) -> None:
        payload = build_multileg_execution_protection(
            strategy="spot_spread",
            legs=[
                {
                    "exchange": "a",
                    "symbol": "ACS/USDC",
                    "side": "buy",
                    "quantity_base": 100.0,
                    "slippage_bps": 2.0,
                },
                {
                    "exchange": "b",
                    "symbol": "ACS/USDT",
                    "side": "sell",
                    "quantity_base": 100.0,
                    "slippage_bps": 1.0,
                },
            ],
            risk=RiskConfig(max_slippage_bps=5.0, max_plan_age_seconds=10.0),
            observed_at=90.0,
            now=91.0,
        )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["would_submit_if_live"])
        self.assertFalse(payload["live_submit_allowed"])
        first_leg = payload["paper_failure_scenarios"][1]
        self.assertEqual(first_leg["name"], "first_leg_only")
        self.assertTrue(first_leg["hedge_required"])
        self.assertEqual(first_leg["hedge_side"], "sell")
        self.assertAlmostEqual(first_leg["hedge_base"], 100.0)

    def test_slippage_limit_blocks_submission(self) -> None:
        payload = build_multileg_execution_protection(
            strategy="funding_arbitrage",
            legs=[
                {
                    "exchange": "spot",
                    "symbol": "BTC/USDT",
                    "side": "buy",
                    "quantity_base": 1.0,
                    "slippage_bps": 20.0,
                },
                {
                    "exchange": "perp",
                    "symbol": "BTC/USDT:USDT",
                    "side": "sell",
                    "quantity_base": 1.0,
                    "slippage_bps": 2.0,
                },
            ],
            risk=RiskConfig(max_slippage_bps=5.0, max_plan_age_seconds=10.0),
            observed_at=90.0,
            now=91.0,
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["would_submit_if_live"])
        self.assertTrue(any("slippage" in reason for reason in payload["reasons"]))

    def test_option_legs_require_manual_review_for_residuals(self) -> None:
        payload = build_multileg_execution_protection(
            strategy="options_arbitrage",
            legs=[
                {
                    "exchange": "deribit",
                    "symbol": "BTC-100-C",
                    "type": "option",
                    "side": "sell",
                    "quantity_base": 1.0,
                    "hedge_asset": "BTC",
                    "hedge_base_equivalent": 1.0,
                    "slippage_bps": 1.0,
                },
                {
                    "exchange": "deribit",
                    "symbol": "BTC-100-P",
                    "type": "option",
                    "side": "buy",
                    "quantity_base": 1.0,
                    "hedge_asset": "BTC",
                    "hedge_base_equivalent": 1.0,
                    "slippage_bps": 1.0,
                },
                {
                    "exchange": "spot",
                    "symbol": "BTC/USDT",
                    "type": "spot",
                    "side": "buy",
                    "quantity_base": 1.0,
                    "slippage_bps": 1.0,
                },
            ],
            risk=RiskConfig(max_slippage_bps=5.0, max_plan_age_seconds=10.0),
            observed_at=90.0,
            now=91.0,
        )

        self.assertEqual(payload["status"], "warning")
        self.assertTrue(payload["requires_manual_review"])
        self.assertTrue(
            any("assignment" in warning for warning in payload["warnings"])
        )
        self.assertEqual(
            payload["paper_failure_scenarios"][1]["status"],
            "manual_review",
        )

    def test_summary_counts_protection_rows_from_strategy_payloads(self) -> None:
        protection = build_multileg_execution_protection(
            strategy="funding_arbitrage",
            legs=[
                {
                    "exchange": "spot",
                    "symbol": "BTC/USDT",
                    "side": "buy",
                    "quantity_base": 1.0,
                    "slippage_bps": 20.0,
                },
                {
                    "exchange": "perp",
                    "symbol": "BTC/USDT:USDT",
                    "side": "sell",
                    "quantity_base": 1.0,
                    "slippage_bps": 1.0,
                },
            ],
            risk=RiskConfig(max_slippage_bps=5.0),
            observed_at=100.0,
            now=100.0,
        )

        summary = summarize_multileg_execution_protections(
            funding_basis={
                "rows": [
                    {
                        "pair_id": "BTC basis",
                        "paper_execution": {"protection": protection},
                    }
                ]
            }
        )

        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["protection_count"], 1)
        self.assertEqual(summary["blocked_count"], 1)
        self.assertEqual(summary["slippage_block_count"], 1)
        self.assertIn("slippage", summary["top_reasons"][0])


if __name__ == "__main__":
    unittest.main()
