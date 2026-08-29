from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from arbitrage_bot.asset_ledger import (
    AssetLedgerStore,
    attach_ledger_checkpoint,
    prune_asset_ledger,
)
from arbitrage_bot.config import AssetLedgerConfig


def _balances(*, status: str = "ok", errors: list[str] | None = None):
    return {
        "status": status,
        "accounts": [
            {
                "exchange": "coinbase-spot",
                "status": status,
                "errors": errors or [],
                "balance": {
                    "checked": not errors,
                    "currencies": [
                        {"currency": "ACS", "free": 90, "used": 10, "total": 100},
                        {"currency": "USDC", "free": 80, "used": 20, "total": 100},
                    ],
                },
            }
        ],
        "checked_account_count": 0 if errors else 1,
        "total_account_count": 1,
        "totals": [],
        "errors": errors or [],
        "last_finished": 1000,
    }


def _activity(*, status: str = "ok", errors: list[str] | None = None):
    return {
        "status": status,
        "accounts": [
            {
                "exchange": "coinbase-spot",
                "status": status,
                "errors": errors or [],
                "open_orders": [
                    {
                        "id": "order-1",
                        "symbol": "ACS/USDC",
                        "side": "buy",
                        "status": "open",
                        "price": 0.2,
                        "amount": 100,
                        "filled": 0,
                        "remaining": 100,
                    }
                ],
                "closed_orders": [],
                "recent_trades": [
                    {
                        "id": "trade-1",
                        "order_id": "order-0",
                        "symbol": "ACS/USDC",
                        "side": "buy",
                        "price": 0.2,
                        "amount": 10,
                        "cost": 2,
                        "timestamp": 999000,
                        "source": "market_maker",
                        "notional_common": 2,
                        "realized_pnl_common": 0.1,
                    }
                ],
            }
        ],
        "open_orders": [],
        "recent_trades": [],
        "daily_pnl": {
            "currency": "USD",
            "total_realized_pnl": 0.1,
            "total_fees": 0.01,
            "sources": {"market_maker": {"realized_pnl": 0.1}},
        },
        "errors": errors or [],
        "last_finished": 1000,
    }


class AssetLedgerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp_dir.name) / "ledger.sqlite3")
        self.cfg = AssetLedgerConfig(enabled=True, path=self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_records_auditable_asset_snapshot(self) -> None:
        store = AssetLedgerStore(self.cfg)
        result = store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio={
                "status": "ok",
                "quote_currency": "USD",
                "total_pnl": 1.25,
                "positions": [
                    {
                        "asset": "ACS",
                        "position_base": 100,
                        "average_entry_price": 0.19,
                        "mark_price": 0.2,
                        "position_value": 20,
                        "price_move_pnl": 1,
                    }
                ],
            },
            observed_at=1000,
        )
        self.assertEqual(result["status"], "ok")

        summary = store.summary(now=1010)
        self.assertEqual(summary["counts"]["balance_snapshots"], 1)
        self.assertEqual(summary["counts"]["order_snapshots"], 1)
        self.assertEqual(summary["counts"]["ledger_fills"], 1)
        self.assertEqual(summary["counts"]["position_snapshots"], 1)
        self.assertEqual(summary["counts"]["pnl_snapshots"], 1)
        self.assertEqual(summary["reconciliation"][0]["status"], "ok")

        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("pragma auto_vacuum").fetchone()[0], 2)
            self.assertEqual(conn.execute("select count(*) from balance_rows").fetchone()[0], 2)
            self.assertEqual(conn.execute("select count(*) from order_rows").fetchone()[0], 1)
            self.assertGreaterEqual(conn.execute("select count(*) from ledger_events").fetchone()[0], 4)

    def test_fill_ingestion_is_idempotent(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_monitor_checkpoint(_balances(), _activity(), observed_at=1000)
        store.record_monitor_checkpoint(_balances(), _activity(), observed_at=1010)
        self.assertEqual(store.summary(now=1011)["counts"]["ledger_fills"], 1)

    def test_performance_excludes_deposits_and_withdrawals(self) -> None:
        store = AssetLedgerStore(self.cfg)

        def portfolio(equity: float) -> dict:
            return {
                "status": "ok",
                "quote_currency": "USD",
                "total_asset_currency": "USD",
                "total_asset_value": equity,
                "cash_balances": {"USDC": equity - 20.0},
                "cash_balances_common": {"USDC": equity - 20.0},
                "cash_value": equity - 20.0,
                "position_value": 20.0,
                "positions": [
                    {
                        "asset": "ACS",
                        "position_base": 100.0,
                        "mark_price": 0.2,
                        "position_value": 20.0,
                    }
                ],
                "position_missing_marks": [],
                "cash_missing_rates": [],
                "total_asset_missing_rates": [],
                "total_pnl": 99.0,
            }

        first = store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio=portfolio(120.0),
            observed_at=1000.0,
        )
        self.assertEqual(first["performance"]["since_inception"]["pnl"], 0.0)

        deposit = {
            "id": "deposit-1",
            "type": "deposit",
            "currency": "USDC",
            "amount": 50.0,
            "timestamp": 1010_000,
            "status": "ok",
        }
        inserted = store.record_cash_flows(
            account_key="coinbase-spot",
            transactions=[deposit],
            supported_types=["deposit", "withdrawal"],
            observed_at=1010.0,
        )
        duplicate = store.record_cash_flows(
            account_key="coinbase-spot",
            transactions=[deposit],
            supported_types=["deposit", "withdrawal"],
            observed_at=1011.0,
        )
        self.assertEqual(inserted["inserted_count"], 1)
        self.assertEqual(duplicate["inserted_count"], 0)

        after_deposit = store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio=portfolio(170.0),
            observed_at=1020.0,
        )["performance"]
        self.assertEqual(after_deposit["since_inception"]["pnl"], 0.0)
        self.assertEqual(
            after_deposit["since_inception"]["net_external_flow"],
            50.0,
        )

        market_gain = store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio=portfolio(180.0),
            observed_at=1060.0,
        )["performance"]
        self.assertEqual(market_gain["since_inception"]["pnl"], 10.0)

        store.record_cash_flows(
            account_key="coinbase-spot",
            transactions=[
                {
                    "id": "withdrawal-1",
                    "type": "withdrawal",
                    "currency": "USDC",
                    "amount": 20.0,
                    "timestamp": 1070_000,
                    "status": "ok",
                }
            ],
            supported_types=["deposit", "withdrawal"],
            observed_at=1070.0,
        )
        after_withdrawal = store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio=portfolio(160.0),
            observed_at=1080.0,
        )["performance"]
        self.assertEqual(after_withdrawal["since_inception"]["pnl"], 10.0)
        self.assertEqual(
            after_withdrawal["since_inception"]["net_external_flow"],
            30.0,
        )

    def test_daily_performance_rolls_over_without_resetting_inception(self) -> None:
        store = AssetLedgerStore(self.cfg)
        first_at = datetime(2026, 8, 27, 23, 59, 0).astimezone().timestamp()
        second_at = datetime(2026, 8, 28, 0, 1, 0).astimezone().timestamp()

        def portfolio(equity: float) -> dict:
            return {
                "status": "ok",
                "quote_currency": "USD",
                "total_asset_currency": "USD",
                "total_asset_value": equity,
                "cash_balances": {"USDC": equity},
                "cash_balances_common": {"USDC": equity},
                "cash_value": equity,
                "position_value": 0.0,
                "positions": [],
                "position_missing_marks": [],
                "cash_missing_rates": [],
                "total_asset_missing_rates": [],
                "total_pnl": 0.0,
            }

        balances = _balances()
        balances["accounts"][0]["balance"]["currencies"] = [
            {"currency": "USDC", "free": 100, "used": 0, "total": 100}
        ]

        store.record_monitor_checkpoint(
            balances,
            _activity(),
            portfolio=portfolio(100.0),
            observed_at=first_at,
        )
        result = store.record_monitor_checkpoint(
            balances,
            _activity(),
            portfolio=portfolio(103.0),
            observed_at=second_at,
        )["performance"]
        self.assertEqual(result["since_inception"]["pnl"], 3.0)
        self.assertEqual(result["daily"]["pnl"], 3.0)
        self.assertNotEqual(
            result["since_inception"]["started_at"],
            result["daily"]["started_at"],
        )

    def test_rolling_24h_performance_uses_trailing_equity_and_excludes_flows(
        self,
    ) -> None:
        store = AssetLedgerStore(self.cfg)
        first_at = 1_800_000_000.0

        def portfolio(equity: float) -> dict:
            return {
                "status": "ok",
                "quote_currency": "USD",
                "total_asset_currency": "USD",
                "total_asset_value": equity,
                "cash_balances": {"USDC": equity},
                "cash_balances_common": {"USDC": equity},
                "cash_value": equity,
                "position_value": 0.0,
                "positions": [],
                "position_missing_marks": [],
                "cash_missing_rates": [],
                "total_asset_missing_rates": [],
            }

        balances = _balances()
        balances["accounts"][0]["balance"]["currencies"] = [
            {"currency": "USDC", "free": 100, "used": 0, "total": 100}
        ]
        first = store.record_monitor_checkpoint(
            balances,
            _activity(),
            portfolio=portfolio(100.0),
            observed_at=first_at,
        )["performance"]
        self.assertEqual(first["rolling_24h"]["pnl"], 0.0)
        self.assertFalse(first["rolling_24h"]["complete_window"])

        one_hour_at = first_at + 3600.0
        store.record_monitor_checkpoint(
            balances,
            _activity(),
            portfolio=portfolio(101.0),
            observed_at=one_hour_at,
        )
        deposit_at = first_at + 13.0 * 3600.0
        store.record_cash_flows(
            account_key="coinbase-spot",
            transactions=[
                {
                    "id": "rolling-deposit",
                    "type": "deposit",
                    "currency": "USDC",
                    "amount": 50.0,
                    "timestamp": int(deposit_at * 1000),
                    "status": "ok",
                }
            ],
            supported_types=["deposit", "withdrawal"],
            observed_at=deposit_at,
        )
        store.record_monitor_checkpoint(
            balances,
            _activity(),
            portfolio=portfolio(163.0),
            observed_at=deposit_at + 60.0,
        )

        result = store.record_monitor_checkpoint(
            balances,
            _activity(),
            portfolio=portfolio(166.0),
            observed_at=first_at + 25.0 * 3600.0,
        )["performance"]["rolling_24h"]
        self.assertTrue(result["complete_window"])
        self.assertEqual(result["started_at"], one_hour_at)
        self.assertEqual(result["net_external_flow"], 50.0)
        self.assertEqual(result["pnl"], 15.0)

    def test_performance_keeps_last_reliable_value_when_valuation_is_missing(self) -> None:
        store = AssetLedgerStore(self.cfg)
        complete = {
            "status": "ok",
            "quote_currency": "USD",
            "total_asset_currency": "USD",
            "total_asset_value": 120.0,
            "cash_balances": {"USDC": 100.0},
            "cash_balances_common": {"USDC": 100.0},
            "cash_value": 100.0,
            "position_value": 20.0,
            "positions": [
                {
                    "asset": "ACS",
                    "position_base": 100.0,
                    "mark_price": 0.2,
                    "position_value": 20.0,
                }
            ],
            "position_missing_marks": [],
            "cash_missing_rates": [],
            "total_asset_missing_rates": [],
            "total_pnl": 0.0,
        }
        store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio=complete,
            observed_at=1000.0,
        )
        incomplete = {**complete, "total_asset_value": None}
        result = store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            portfolio=incomplete,
            observed_at=1060.0,
        )["performance"]
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["since_inception"]["pnl"], 0.0)
        self.assertEqual(result["last_reliable_at"], 1000.0)

    def test_shared_spot_and_swap_account_applies_cash_flow_once(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_cash_flows(
            account_key="workspace:account-1:spot",
            transactions=[],
            supported_types=["deposit", "withdrawal"],
            observed_at=990.0,
        )
        store.set_cash_flow_account_alias(
            account_key="workspace:account-1:swap",
            canonical_account_key="workspace:account-1:spot",
            observed_at=990.0,
        )

        def balances(spot_total: float) -> dict:
            return {
                "status": "ok",
                "accounts": [
                    {
                        "exchange": "workspace:account-1:spot",
                        "status": "ok",
                        "errors": [],
                        "balance": {
                            "checked": True,
                            "currencies": [
                                {
                                    "currency": "USDC",
                                    "free": spot_total,
                                    "used": 0.0,
                                    "total": spot_total,
                                }
                            ],
                        },
                    },
                    {
                        "exchange": "workspace:account-1:swap",
                        "status": "ok",
                        "errors": [],
                        "balance": {
                            "checked": True,
                            "currencies": [
                                {
                                    "currency": "USDC",
                                    "free": 100.0,
                                    "used": 0.0,
                                    "total": 100.0,
                                }
                            ],
                        },
                    },
                ],
                "totals": [],
                "checked_account_count": 2,
                "total_account_count": 2,
                "errors": [],
            }

        def portfolio(equity: float) -> dict:
            return {
                "status": "ok",
                "quote_currency": "USD",
                "total_asset_currency": "USD",
                "total_asset_value": equity,
                "cash_balances": {"USDC": equity},
                "cash_balances_common": {"USDC": equity},
                "cash_value": equity,
                "position_value": 0.0,
                "positions": [],
                "position_missing_marks": [],
                "cash_missing_rates": [],
                "total_asset_missing_rates": [],
                "total_pnl": 0.0,
            }

        store.record_monitor_checkpoint(
            balances(100.0),
            {"accounts": [], "daily_pnl": {}},
            portfolio=portfolio(200.0),
            observed_at=1000.0,
        )
        store.record_cash_flows(
            account_key="workspace:account-1:spot",
            transactions=[
                {
                    "id": "shared-deposit",
                    "type": "deposit",
                    "currency": "USDC",
                    "amount": 50.0,
                    "timestamp": 1010_000,
                    "status": "ok",
                }
            ],
            supported_types=["deposit", "withdrawal"],
            observed_at=1010.0,
        )
        result = store.record_monitor_checkpoint(
            balances(150.0),
            {"accounts": [], "daily_pnl": {}},
            portfolio=portfolio(250.0),
            observed_at=1020.0,
        )["performance"]
        self.assertEqual(result["since_inception"]["net_external_flow"], 50.0)
        self.assertEqual(result["since_inception"]["pnl"], 0.0)
        self.assertEqual(
            result["cash_flow_coverage"]["covered_account_count"],
            2,
        )

    def test_strategy_fill_source_is_idempotent_and_queryable(self) -> None:
        store = AssetLedgerStore(self.cfg)
        trade = {
            **_activity()["accounts"][0]["recent_trades"][0],
            "strategy_instance_id": "user-mm-123",
            "source": "market_maker",
        }
        first = store.record_fills(
            account_key="workspace:connection-1:spot",
            trades=[trade],
            source="market-maker:user-mm-123",
            observed_at=1000,
        )
        second = store.record_fills(
            account_key="workspace:connection-1:spot",
            trades=[trade],
            source="market-maker:user-mm-123",
            observed_at=1010,
        )

        self.assertEqual(first["new_fill_count"], 1)
        self.assertEqual(second["new_fill_count"], 0)
        rows = store.recent_fills(
            observation_sources=["market-maker:user-mm-123"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "trade-1")
        self.assertEqual(rows[0]["source"], "market_maker")
        self.assertEqual(
            rows[0]["observation_source"],
            "market-maker:user-mm-123",
        )
        self.assertEqual(
            store.recent_fills(
                observation_sources=["market-maker:user-mm-other"],
            ),
            [],
        )

    def test_balance_identity_difference_is_audited(self) -> None:
        payload = _balances()
        payload["accounts"][0]["balance"]["currencies"][0]["total"] = 120
        store = AssetLedgerStore(self.cfg)
        result = store.record_monitor_checkpoint(payload, _activity(), observed_at=1000)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["accounts"][0]["diff_count"], 1)

    def test_unexplained_exchange_balance_change_is_reconciled(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_monitor_checkpoint(_balances(), _activity(), observed_at=1000)
        changed = _balances()
        changed["accounts"][0]["balance"]["currencies"][1] = {
            "currency": "USDC",
            "free": 130,
            "used": 20,
            "total": 150,
        }
        result = store.record_monitor_checkpoint(
            changed,
            _activity(),
            observed_at=1010,
        )
        self.assertEqual(result["status"], "warning")
        with sqlite3.connect(self.path) as conn:
            categories = {
                row[0]
                for row in conn.execute(
                    "select category from reconciliation_diffs"
                ).fetchall()
            }
        self.assertIn("unexplained_balance_change", categories)

    def test_balance_projection_does_not_mix_collection_sources(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            source="web-monitor",
            observed_at=1000,
        )
        changed = _balances()
        changed["accounts"][0]["balance"]["currencies"][1] = {
            "currency": "USDC",
            "free": 130,
            "used": 20,
            "total": 150,
        }
        result = store.record_account_snapshot(
            account_key="coinbase-spot",
            balance_account=changed["accounts"][0],
            order_account=_activity()["accounts"][0],
            source="account-reader:coinbase-spot",
            observed_at=1010,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["diff_count"], 0)

    def test_fill_observation_cursor_is_independent_per_source(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_monitor_checkpoint(
            _balances(),
            _activity(),
            source="web-monitor",
            observed_at=1000,
        )
        worker_result = store.record_account_snapshot(
            account_key="coinbase-spot",
            balance_account=_balances()["accounts"][0],
            order_account=_activity()["accounts"][0],
            source="account-reader:coinbase-spot",
            observed_at=1010,
        )
        self.assertEqual(worker_result["status"], "ok")
        with sqlite3.connect(self.path) as conn:
            sources = {
                row[0]
                for row in conn.execute(
                    "select source from fill_source_observations"
                ).fetchall()
            }
        self.assertEqual(
            sources,
            {"web-monitor", "account-reader:coinbase-spot"},
        )

    def test_worker_reuses_unchanged_snapshots_and_fill_events(self) -> None:
        store = AssetLedgerStore(self.cfg)
        source = "account-reader:coinbase-spot"
        for observed_at in (1000, 1010):
            store.record_account_snapshot(
                account_key="coinbase-spot",
                balance_account=_balances()["accounts"][0],
                order_account=_activity()["accounts"][0],
                source=source,
                observed_at=observed_at,
            )

        summary = store.summary(now=1011)
        self.assertEqual(summary["counts"]["balance_snapshots"], 1)
        self.assertEqual(summary["counts"]["order_snapshots"], 1)
        self.assertEqual(summary["counts"]["reconciliation_runs"], 2)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "select count(*) from ledger_events where event_type = ?",
                    ("fill_observed",),
                ).fetchone()[0],
                1,
            )

    def test_lightweight_summary_omits_expensive_counts(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_monitor_checkpoint(_balances(), _activity(), observed_at=1000)
        summary = store.summary(now=1010, include_counts=False)
        self.assertEqual(summary["counts"], {})
        self.assertEqual(summary["status"], "ok")

    def test_failed_refresh_uses_last_healthy_checkpoint(self) -> None:
        balances, activity, _ = attach_ledger_checkpoint(
            self.cfg, _balances(), _activity()
        )
        self.assertEqual(balances["status"], "ok")
        self.assertEqual(activity["status"], "ok")

        failed_balances, failed_activity, ledger = attach_ledger_checkpoint(
            self.cfg,
            _balances(status="error", errors=["timeout"]),
            _activity(status="error", errors=["timeout"]),
        )
        self.assertEqual(failed_balances["status"], "error")
        self.assertEqual(failed_activity["status"], "error")
        self.assertTrue(failed_balances["stale_snapshot"])
        self.assertEqual(
            failed_balances["accounts"][0]["balance"]["currencies"][0]["currency"],
            "ACS",
        )
        self.assertTrue(ledger["fallback_used"])
        self.assertEqual(ledger["checkpoint"]["status"], "error")

    def test_worker_heartbeat_tracks_errors_and_staleness(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.update_worker_heartbeat(
            worker_id="account-reader:coinbase-spot",
            account_key="coinbase-spot",
            status="error",
            increment_cycle=True,
            increment_error=True,
            last_error="timeout",
        )
        worker = store.summary(now=time.time() + 1000)["workers"][0]
        self.assertEqual(worker["cycle_count"], 1)
        self.assertEqual(worker["error_count"], 1)
        self.assertEqual(worker["last_error"], "timeout")
        self.assertTrue(worker["stale"])

    def test_prune_removes_expired_history_and_preserves_latest_per_source(self) -> None:
        store = AssetLedgerStore(self.cfg)
        store.record_monitor_checkpoint(_balances(), _activity(), observed_at=1000)

        current_activity = _activity()
        current_activity["accounts"][0]["recent_trades"][0] = {
            **current_activity["accounts"][0]["recent_trades"][0],
            "id": "trade-2",
            "order_id": "order-2",
            "timestamp": 4999000,
        }
        store.record_monitor_checkpoint(
            _balances(),
            current_activity,
            observed_at=5000,
        )
        store.record_account_snapshot(
            account_key="coinbase-spot",
            balance_account=_balances()["accounts"][0],
            order_account=_activity()["accounts"][0],
            source="stale-reader",
            observed_at=900,
        )

        deleted = prune_asset_ledger(self.cfg, before=4000)

        self.assertGreater(deleted["ledger_events"], 0)
        self.assertEqual(deleted["monitor_checkpoints"], 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("select count(*) from monitor_checkpoints").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "select count(*) from balance_snapshots where source = ?",
                    ("stale-reader",),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "select count(*) from ledger_fills where trade_id = ?",
                    ("trade-1",),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "select count(*) from ledger_fills where trade_id = ?",
                    ("trade-2",),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
