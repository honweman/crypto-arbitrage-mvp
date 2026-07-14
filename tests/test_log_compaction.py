from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from arbitrage_bot.maintenance import optimize_sqlite, prune_historical_files
from arbitrage_bot.order_reliability import OrderIntentStore


class LogCompactionScriptTest(unittest.TestCase):
    def test_compact_logs_rotates_configured_jsonl_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trade_path = root / "trade_events.jsonl"
            timeline_path = root / "strategy_timeline.jsonl"
            audit_path = root / "web_audit_events.jsonl"
            for path in (trade_path, timeline_path, audit_path):
                path.write_text('{"event": "one"}\n', encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "trade_log": {
                            "path": str(trade_path),
                            "rotate_max_bytes": 1,
                            "rotate_keep_files": 1,
                            "rotate_compress": False,
                        },
                        "strategy_timeline": {
                            "path": str(timeline_path),
                            "rotate_max_bytes": 1,
                            "rotate_keep_files": 1,
                            "rotate_compress": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/compact_logs.py",
                    "--config",
                    str(config_path),
                ],
                check=True,
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONPATH": "src"},
                text=True,
                capture_output=True,
            )
            payload = json.loads(result.stdout)

            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["results"]), 3)
            self.assertTrue(any(root.glob("trade_events.jsonl.*")))
            self.assertTrue(any(root.glob("strategy_timeline.jsonl.*")))
            self.assertTrue(any(root.glob("web_audit_events.jsonl.*")))

    def test_sqlite_maintenance_removes_terminal_intents_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order_intents.sqlite3"
            store = OrderIntentStore(path)
            intent = {
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "side": "buy",
                "amount": 1.0,
                "price": 1.0,
            }
            store.reserve("terminal", intent)
            store.mark_submitted("terminal", {"id": "exchange-order"})
            store.reserve("uncertain", intent)
            store.mark_unknown("uncertain", "gateway timeout")

            result = optimize_sqlite(
                path,
                order_intent_retention_days=0,
                order_intent_max_terminal_rows=1_000,
            )

            self.assertTrue(result["optimized"])
            self.assertEqual(
                result["order_intent_compaction"]["expired_removed"],
                1,
            )
            self.assertIsNone(store.get("terminal"))
            self.assertEqual(store.get("uncertain")["status"], "unknown")

    def test_backup_retention_supports_dry_run_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = time.time()
            for index in range(4):
                path = root / f"deploy_backup_2026010{index}_000000.tgz"
                path.write_bytes(b"backup")
                os.utime(path, (now - 30 * 24 * 60 * 60 - index, ) * 2)

            dry_run = prune_historical_files(
                root,
                patterns=("deploy_backup_*.tgz",),
                keep_files=1,
                min_age_days=14,
                apply=False,
                now=now,
            )
            applied = prune_historical_files(
                root,
                patterns=("deploy_backup_*.tgz",),
                keep_files=1,
                min_age_days=14,
                apply=True,
                now=now,
            )

            self.assertEqual(dry_run["candidate_count"], 3)
            self.assertEqual(dry_run["removed_count"], 0)
            self.assertEqual(applied["removed_count"], 3)
            self.assertEqual(len(list(root.glob("deploy_backup_*.tgz"))), 1)


if __name__ == "__main__":
    unittest.main()
