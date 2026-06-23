from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from arbitrage_bot.config import load_config
from arbitrage_bot.contract_strategies import build_contract_strategies_payload


def sample_funding_basis() -> dict:
    return {
        "status": "candidate",
        "mode": "paper",
        "rows": [
            {
                "pair_id": "btc-binance",
                "enabled": True,
                "spot_exchange": "binance-spot",
                "spot_symbol": "BTC/USDT",
                "derivative_exchange": "binance-usdm",
                "derivative_symbol": "BTC/USDT:USDT",
                "spot_mid": 100.0,
                "derivative_mid": 101.0,
                "basis_bps": 100.0,
                "funding_rate_bps": 2.0,
                "estimated_apr_pct": 21.9,
                "direction": "long_spot_short_perp",
                "status": "candidate",
                "reason": "entry conditions met",
                "thresholds": {
                    "min_funding_bps": 1.0,
                    "min_entry_basis_bps": 15.0,
                },
                "paper_execution": {
                    "mode": "paper",
                    "state": "would_open",
                    "notional_quote": 1000.0,
                    "suggested_legs": [
                        {
                            "exchange": "binance-spot",
                            "symbol": "BTC/USDT",
                            "side": "buy",
                            "type": "spot",
                            "quantity_base": 9.900990099,
                            "average_price": 100.0,
                            "notional_quote": 990.099,
                            "hedge_asset": "BTC",
                            "hedge_base_equivalent": 9.900990099,
                        },
                        {
                            "exchange": "binance-usdm",
                            "symbol": "BTC/USDT:USDT",
                            "side": "sell",
                            "type": "perp",
                            "quantity_base": 9.900990099,
                            "average_price": 101.0,
                            "notional_quote": 1000.0,
                            "hedge_asset": "BTC",
                            "hedge_base_equivalent": 9.900990099,
                        },
                    ],
                    "protection": {"status": "ok", "reasons": [], "warnings": []},
                },
                "observed_at": 1000.0,
            }
        ],
    }


class ContractStrategiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = load_config(
            Path(__file__).resolve().parents[1] / "config.example.json"
        )

    def test_funding_and_basis_candidates_are_paper_only(self) -> None:
        payload = build_contract_strategies_payload(
            self.cfg,
            funding_basis=sample_funding_basis(),
            derivatives={"status": "ok"},
            market_maker={},
            order_activity={},
            now=1000.0,
        )

        self.assertEqual(payload["status"], "candidate")
        funding = next(row for row in payload["rows"] if row["strategy_id"] == "funding_bot")
        basis = next(row for row in payload["rows"] if row["strategy_id"] == "basis_bot")
        self.assertEqual(funding["status"], "candidate")
        self.assertEqual(basis["status"], "candidate")
        self.assertFalse(funding["plan"]["auto_submit_live_orders"])
        self.assertFalse(payload["execution_controls"]["auto_submit_live_orders"])
        self.assertEqual(len(basis["plan"]["legs"]), 2)

    def test_futures_grid_generates_low_leverage_paper_orders(self) -> None:
        cfg = replace(
            self.cfg,
            contract_strategies=replace(
                self.cfg.contract_strategies,
                futures_grid_enabled=True,
                futures_grid_levels=3,
                futures_grid_quote_per_level=10.0,
                futures_grid_max_leverage=1.0,
            ),
        )

        payload = build_contract_strategies_payload(
            cfg,
            funding_basis=sample_funding_basis(),
            derivatives={"status": "ok"},
            market_maker={},
            order_activity={},
            now=1010.0,
        )

        grid = next(row for row in payload["rows"] if row["strategy_id"] == "futures_grid")
        self.assertEqual(grid["status"], "candidate")
        self.assertEqual(grid["plan"]["order_count"], 6)
        self.assertTrue(all(order["leverage"] == 1.0 for order in grid["plan"]["orders"]))
        self.assertTrue(all(not order["reduce_only"] for order in grid["plan"]["orders"]))

    def test_hedge_rebalancer_plans_opposite_perp_delta(self) -> None:
        cfg = replace(
            self.cfg,
            contract_strategies=replace(
                self.cfg.contract_strategies,
                hedge_rebalancer_enabled=True,
                hedge_threshold_base=0.1,
            ),
        )

        payload = build_contract_strategies_payload(
            cfg,
            funding_basis=sample_funding_basis(),
            derivatives={"status": "ok"},
            market_maker={},
            order_activity={
                "recent_trades": [
                    {
                        "source": "market_maker",
                        "symbol": "BTC/USDT",
                        "side": "buy",
                        "amount": 2.0,
                        "cost": 200.0,
                    },
                    {
                        "source": "market_maker",
                        "symbol": "BTC/USDT",
                        "side": "sell",
                        "amount": 0.5,
                        "cost": 50.0,
                    },
                ]
            },
            now=1010.0,
        )

        hedge = next(
            row for row in payload["rows"] if row["strategy_id"] == "hedge_rebalancer"
        )
        self.assertEqual(hedge["status"], "candidate")
        self.assertEqual(hedge["plan"]["order"]["side"], "sell")
        self.assertAlmostEqual(hedge["plan"]["order"]["quantity_base"], 1.5)
        self.assertFalse(hedge["plan"]["auto_submit_live_orders"])


if __name__ == "__main__":
    unittest.main()
