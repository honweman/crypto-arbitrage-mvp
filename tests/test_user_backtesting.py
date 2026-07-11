from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from arbitrage_bot.user_backtesting import (
    UserBacktestService,
    UserBacktestStore,
    completed_ohlcv_rows,
    fetch_public_ohlcv,
    fill_ohlcv_gaps,
    normalize_ohlcv_rows,
)
from arbitrage_bot.user_strategies import UserStrategy
from arbitrage_bot.user_workspace import (
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)


class UserBacktestStoreTest(unittest.TestCase):
    def test_normalize_ohlcv_sorts_deduplicates_and_skips_invalid_rows(self) -> None:
        rows = normalize_ohlcv_rows(
            [
                [2_000, 1.0, 1.2, 0.9, 1.1, 10.0],
                [1_000, 0.9, 1.1, 0.8, 1.0, 8.0],
                [2_000, 1.0, 1.3, 0.9, 1.2, 12.0],
                [3_000, 1.0, 1.1, 0.0, 1.0, 1.0],
                ["bad"],
            ]
        )

        self.assertEqual([row["timestamp_ms"] for row in rows], [1_000, 2_000])
        self.assertEqual(rows[-1]["close"], 1.2)

    def test_completed_ohlcv_rows_excludes_open_candle(self) -> None:
        rows = [
            {"timestamp_ms": 1_000, "close": 1.0},
            {"timestamp_ms": 61_000, "close": 1.1},
        ]

        completed = completed_ohlcv_rows(
            rows,
            timeframe="1m",
            now_ms=120_999,
        )

        self.assertEqual([row["timestamp_ms"] for row in completed], [1_000])

    def test_fill_ohlcv_gaps_uses_previous_close_and_zero_volume(self) -> None:
        rows = [
            {
                "timestamp_ms": 60_000,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 5.0,
            },
            {
                "timestamp_ms": 180_000,
                "open": 1.2,
                "high": 1.3,
                "low": 1.1,
                "close": 1.2,
                "volume": 7.0,
            },
        ]

        filled = fill_ohlcv_gaps(
            rows,
            timeframe="1m",
            limit=3,
            now_ms=241_000,
        )

        self.assertEqual(
            [row["timestamp_ms"] for row in filled],
            [60_000, 120_000, 180_000],
        )
        self.assertTrue(filled[1]["gap_filled"])
        self.assertEqual(filled[1]["close"], 1.0)
        self.assertEqual(filled[1]["volume"], 0.0)


class PublicOhlcvFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_fetch_backfills_sparse_exchange_candles(self) -> None:
        timeframe_ms = 3_600_000
        now_ms = int(time.time() * 1000)
        last_bucket = now_ms // timeframe_ms * timeframe_ms - timeframe_ms

        class FakeManager:
            last_instance = None

            def __init__(self) -> None:
                self.calls = []
                self.closed = False
                FakeManager.last_instance = self

            async def fetch_ohlcv(
                self,
                _cfg,
                *,
                symbol,
                timeframe,
                since_ms,
                limit,
            ):
                self.calls.append((symbol, timeframe, since_ms, limit))
                return [
                    [
                        last_bucket - 19 * timeframe_ms,
                        1.0,
                        1.1,
                        0.9,
                        1.0,
                        5.0,
                    ],
                    [last_bucket, 1.2, 1.3, 1.1, 1.2, 7.0],
                ]

            async def close(self) -> None:
                self.closed = True

        account = UserExchangeAccount.from_dict(
            {
                "id": "account-public",
                "owner_email": "trader@example.com",
                "project_id": "project-public",
                "label": "Coinbase Public",
                "exchange": "coinbase",
                "market_type": "spot",
                "symbol": "ACS/USDC",
            }
        )

        rows = await fetch_public_ohlcv(
            account,
            timeframe="1h",
            limit=20,
            manager_factory=FakeManager,
        )

        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(bool(row["gap_filled"]) for row in rows), 18)
        instance = FakeManager.last_instance
        assert instance is not None
        self.assertEqual(instance.calls[0][3], 300)
        self.assertTrue(instance.closed)

    def test_store_scopes_runs_and_marks_inflight_run_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "backtests.sqlite3"
            store = UserBacktestStore(path)
            now = time.time()
            payload = {
                "id": "run-a",
                "owner_email": "a@example.com",
                "project_id": "project-a",
                "status": "running",
                "created_at": now,
                "updated_at": now,
            }
            store.create(payload)

            restarted = UserBacktestStore(path)
            run = restarted.get("run-a")
            assert run is not None
            self.assertEqual(run["status"], "interrupted")
            self.assertEqual(
                restarted.list(
                    owner_email="b@example.com",
                    is_admin=False,
                ),
                [],
            )
            self.assertEqual(
                len(
                    restarted.list(
                        owner_email="admin@example.com",
                        is_admin=True,
                    )
                ),
                1,
            )


class UserBacktestServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.workspace = UserWorkspaceStore(
            base / "workspace.sqlite3",
            master_key_env="",
        )
        self.store = UserBacktestStore(base / "backtests.sqlite3")
        owner = "trader@example.com"
        self.project = self.workspace.upsert_project(
            UserProject.from_dict(
                {
                    "id": "project-acs",
                    "owner_email": owner,
                    "name": "ACS research",
                    "asset": "ACS",
                    "quote_currency": "USDC",
                    "status": "active",
                }
            )
        )
        self.account = self.workspace.upsert_account(
            UserExchangeAccount.from_dict(
                {
                    "id": "account-coinbase",
                    "owner_email": owner,
                    "project_id": self.project.id,
                    "label": "Coinbase public market",
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDC",
                }
            )
        )
        self.strategy = self.workspace.upsert_strategy(
            UserStrategy.from_dict(
                {
                    "id": "strategy-grid",
                    "owner_email": owner,
                    "project_id": self.project.id,
                    "name": "ACS grid research",
                    "strategy_type": "spot_grid",
                    "account_ids": [self.account.id],
                    "parameters": {
                        "lower_price": 0.8,
                        "upper_price": 1.2,
                        "grid_count": 8,
                        "quote_per_grid": 2.0,
                    },
                }
            )
        )
        self.fetch_count = 0

        async def fake_fetcher(account, *, timeframe, limit):
            self.fetch_count += 1
            self.assertEqual(account.id, self.account.id)
            self.assertEqual(timeframe, "1h")
            await asyncio.sleep(0)
            start = 1_700_000_000_000
            return [
                {
                    "timestamp_ms": start + index * 3_600_000,
                    "open": 1.0,
                    "high": 1.02,
                    "low": 0.98,
                    "close": 1.0 + ((index % 5) - 2) * 0.02,
                    "volume": 100.0 + index,
                }
                for index in range(limit)
            ]

        self.service = UserBacktestService(
            self.workspace,
            self.store,
            fetcher=fake_fetcher,
            cache_seconds=60.0,
        )

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.tmpdir.cleanup()

    async def _wait_terminal(self, run_id: str) -> dict:
        for _ in range(100):
            run = self.store.get(run_id)
            assert run is not None
            if run["status"] not in {"queued", "fetching", "running"}:
                return run
            await asyncio.sleep(0.01)
        self.fail("backtest did not finish")

    async def test_public_historical_run_completes_without_credentials(self) -> None:
        run = await self.service.create_run(
            owner_email="trader@example.com",
            project_id=self.project.id,
            strategy_id=self.strategy.id,
            account_id=self.account.id,
            timeframe="1h",
            history_bars=40,
            initial_cash=100.0,
            fee_bps=20.0,
            slippage_bps=5.0,
        )
        completed = await self._wait_terminal(run["id"])

        self.assertEqual(completed["status"], "complete")
        self.assertFalse(completed["live_submit_allowed"])
        self.assertEqual(completed["result"]["bar_count"], 40)
        self.assertEqual(
            completed["result"]["market_data"]["symbol"],
            "ACS/USDC",
        )
        self.assertEqual(self.fetch_count, 1)
        self.assertNotIn("credentials", completed["account"])

    async def test_history_cache_is_shared_but_results_remain_owner_scoped(self) -> None:
        first = await self.service.create_run(
            owner_email="trader@example.com",
            project_id=self.project.id,
            strategy_id=self.strategy.id,
            account_id=self.account.id,
            timeframe="1h",
            history_bars=40,
        )
        await self._wait_terminal(first["id"])
        second = await self.service.create_run(
            owner_email="trader@example.com",
            project_id=self.project.id,
            strategy_id=self.strategy.id,
            account_id=self.account.id,
            timeframe="1h",
            history_bars=40,
        )
        completed = await self._wait_terminal(second["id"])

        self.assertTrue(completed["result"]["market_data"]["cached"])
        self.assertEqual(self.fetch_count, 1)
        self.assertEqual(
            self.service.public_payload(
                owner_email="other@example.com",
                is_admin=False,
            )["runs"],
            [],
        )
        with self.assertRaisesRegex(PermissionError, "another user"):
            self.service.delete_run(
                first["id"],
                owner_email="other@example.com",
                is_admin=False,
            )

    async def test_rejects_strategy_outside_supported_scope(self) -> None:
        unsupported = self.workspace.upsert_strategy(
            UserStrategy.from_dict(
                {
                    "id": "strategy-mm",
                    "owner_email": "trader@example.com",
                    "project_id": self.project.id,
                    "name": "MM",
                    "strategy_type": "market_maker",
                    "account_ids": [self.account.id],
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "Spot Grid and DCA"):
            await self.service.create_run(
                owner_email="trader@example.com",
                project_id=self.project.id,
                strategy_id=unsupported.id,
                account_id=self.account.id,
                timeframe="1h",
            )

    async def test_limits_concurrent_runs_per_owner(self) -> None:
        now = time.time()
        for index in range(3):
            self.store.create(
                {
                    "id": f"active-{index}",
                    "owner_email": "trader@example.com",
                    "project_id": self.project.id,
                    "status": "running",
                    "created_at": now + index,
                    "updated_at": now + index,
                }
            )

        with self.assertRaisesRegex(ValueError, "at most 3"):
            await self.service.create_run(
                owner_email="trader@example.com",
                project_id=self.project.id,
                strategy_id=self.strategy.id,
                account_id=self.account.id,
                timeframe="1h",
            )


if __name__ == "__main__":
    unittest.main()
