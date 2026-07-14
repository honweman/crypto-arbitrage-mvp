from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arbitrage_bot.account_worker import AccountWorker, _isolated_config
from arbitrage_bot.config import AssetLedgerConfig, load_config


class AccountWorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = load_config("config.acs.example.json")
        self.cfg = replace(
            base,
            asset_ledger=AssetLedgerConfig(
                enabled=True,
                path=str(Path(self.temp_dir.name) / "ledger.sqlite3"),
                worker_interval_seconds=5,
                worker_timeout_seconds=3,
            ),
            pnl_store=replace(base.pnl_store, enabled=False),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_isolated_config_contains_only_selected_account(self) -> None:
        isolated = _isolated_config(self.cfg, "coinbase-spot")
        self.assertEqual([row.key for row in isolated.spot_exchanges], ["coinbase-spot"])
        self.assertEqual(isolated.derivative_exchanges, [])

    async def test_one_worker_cycle_records_heartbeat_and_snapshot(self) -> None:
        balances = {
            "exchange": "coinbase-spot",
            "status": "ok",
            "errors": [],
            "balance": {
                "checked": True,
                "currencies": [
                    {"currency": "USDC", "free": 10, "used": 0, "total": 10}
                ],
            },
        }
        activity = {
            "exchange": "coinbase-spot",
            "status": "ok",
            "errors": [],
            "open_orders": [],
            "closed_orders": [],
            "recent_trades": [],
        }
        worker = AccountWorker(self.cfg, "coinbase-spot", once=True)
        with (
            patch(
                "arbitrage_bot.account_worker.fetch_account_balances_snapshot",
                new=AsyncMock(return_value=balances),
            ),
            patch(
                "arbitrage_bot.account_worker.fetch_order_activity_snapshot",
                new=AsyncMock(return_value=activity),
            ),
        ):
            result = await worker.run()

        self.assertEqual(result["status"], "ok")
        summary = worker.ledger.summary()
        self.assertEqual(summary["counts"]["balance_snapshots"], 1)
        self.assertEqual(summary["counts"]["order_snapshots"], 1)
        heartbeat = summary["workers"][0]
        self.assertEqual(heartbeat["status"], "stopped")
        self.assertEqual(heartbeat["cycle_count"], 1)
        self.assertTrue(heartbeat["metadata"]["read_only"])

    async def test_timeout_is_contained_in_worker_heartbeat(self) -> None:
        worker = AccountWorker(
            self.cfg,
            "upbit-spot",
            timeout_seconds=3,
            once=True,
        )
        worker.timeout_seconds = 0.01

        async def slow(*args, **kwargs):
            import asyncio

            await asyncio.sleep(0.1)
            return {}

        with (
            patch(
                "arbitrage_bot.account_worker.fetch_account_balances_snapshot",
                side_effect=slow,
            ),
            patch(
                "arbitrage_bot.account_worker.fetch_order_activity_snapshot",
                side_effect=slow,
            ),
        ):
            result = await worker.run()

        self.assertEqual(result["status"], "error")
        heartbeat = worker.ledger.summary()["workers"][0]
        self.assertEqual(heartbeat["error_count"], 1)
        self.assertEqual(heartbeat["cycle_count"], 1)


if __name__ == "__main__":
    unittest.main()
