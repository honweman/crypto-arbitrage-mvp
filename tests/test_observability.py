import unittest

from arbitrage_bot.observability import render_prometheus_metrics


class ObservabilityTest(unittest.TestCase):
    def test_prometheus_metrics_render_core_state(self) -> None:
        text = render_prometheus_metrics(
            {
                "status": "running",
                "scan": {"count": 7, "elapsed_ms": 123},
                "opportunities": [{"strategy": "spot-spread"}],
                "warnings": ["one warning"],
                "program": {"running": True},
                "order_activity": {
                    "open_order_count": 3,
                    "recent_trade_count": 2,
                },
                "market_maker": {
                    "runtime": {"mode": "live", "open_order_count": 4}
                },
                "spot_grid": {
                    "runtime": {"mode": "dry_run", "open_order_count": 5}
                },
            }
        )

        self.assertIn("crypto_arb_scan_count 7", text)
        self.assertIn('crypto_arb_opportunity_count{status="running"} 1', text)
        self.assertIn('crypto_arb_market_maker_open_orders{mode="live"} 4', text)
        self.assertIn('crypto_arb_spot_grid_open_orders{mode="dry_run"} 5', text)


if __name__ == "__main__":
    unittest.main()
