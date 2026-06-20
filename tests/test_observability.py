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
                "operations": {
                    "risk": {
                        "enabled": True,
                        "trading_enabled": True,
                        "allow_live_trading": False,
                    }
                },
                "order_activity": {
                    "status": "ok",
                    "open_order_count": 3,
                    "recent_trade_count": 2,
                },
                "market_maker": {
                    "runtime": {
                        "status": "running",
                        "mode": "live",
                        "open_order_count": 4,
                    }
                },
                "spot_grid": {
                    "runtime": {
                        "status": "blocked_by_risk",
                        "mode": "dry_run",
                        "open_order_count": 5,
                    }
                },
                "readiness": {
                    "status": "blocked",
                    "summary": {
                        "blocked_count": 2,
                        "warning_count": 1,
                        "action_count": 3,
                    },
                    "order_checks": {"reconciliation_issue_count": 1},
                    "accounts": [
                        {
                            "key": "coinbase-spot",
                            "status": "ready",
                            "used": True,
                        },
                        {
                            "key": "bybit-spot",
                            "status": "blocked",
                            "used": True,
                        },
                    ],
                    "strategies": [
                        {
                            "id": "market_maker",
                            "status": "live",
                            "mode": "live",
                            "live": True,
                            "paused": False,
                            "configured": True,
                        },
                        {
                            "id": "slow_execution",
                            "status": "paused",
                            "mode": "paused",
                            "live": False,
                            "paused": True,
                            "configured": True,
                        },
                    ],
                },
            }
        )

        self.assertIn('crypto_arb_status{status="running"} 1', text)
        self.assertIn("crypto_arb_scan_count 7", text)
        self.assertIn('crypto_arb_opportunity_count{status="running"} 1', text)
        self.assertIn("crypto_arb_risk_live_trading_allowed 0", text)
        self.assertIn("crypto_arb_readiness_blocked_count 2", text)
        self.assertIn("crypto_arb_reconciliation_issue_count 1", text)
        self.assertIn(
            'crypto_arb_readiness_status{status="blocked"} 1',
            text,
        )
        self.assertIn(
            (
                'crypto_arb_readiness_account_status{account="bybit-spot",'
                'status="blocked",used="true"} 1'
            ),
            text,
        )
        self.assertIn(
            (
                'crypto_arb_readiness_strategy_status{mode="paused",'
                'status="paused",strategy="slow_execution"} 1'
            ),
            text,
        )
        self.assertIn('crypto_arb_strategy_paused{strategy="slow_execution"} 1', text)
        self.assertIn('crypto_arb_market_maker_open_orders{mode="live"} 4', text)
        self.assertIn('crypto_arb_spot_grid_open_orders{mode="dry_run"} 5', text)
        self.assertIn(
            (
                'crypto_arb_runtime_status{mode="dry_run",'
                'status="blocked_by_risk",strategy="spot_grid"} 1'
            ),
            text,
        )


if __name__ == "__main__":
    unittest.main()
