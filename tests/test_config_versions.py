from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.web.config_versions import ConfigVersionStore, configuration_diff


class ConfigVersionStoreTest(unittest.TestCase):
    def test_versions_record_diff_and_restore_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigVersionStore(Path(tmp) / "versions.sqlite3")
            first = store.record(
                {
                    "risk_overrides": {"max_order_quote": 5.0},
                    "program": {"running": True},
                },
                actor_email="one@example.com",
                action="initial",
                known_good=True,
            )
            second = store.record(
                {
                    "risk_overrides": {"max_order_quote": 10.0},
                    "program": {"running": False},
                },
                actor_email="two@example.com",
                action="risk_update",
            )

            self.assertNotEqual(first["hash"], second["hash"])
            self.assertEqual(second["change_count"], 1)
            self.assertEqual(
                second["diff"][0]["path"], "risk_overrides.max_order_quote"
            )
            self.assertEqual(store.latest_known_good()["id"], first["id"])
            restored = store.get(first["id"], include_payload=True)
            self.assertEqual(
                restored["payload"]["risk_overrides"]["max_order_quote"], 5.0
            )
            self.assertNotIn("program", restored["payload"])

    def test_configuration_diff_reports_added_removed_and_changed_fields(self) -> None:
        diff = configuration_diff(
            {"a": 1, "removed": True},
            {"a": 2, "added": True},
        )
        self.assertEqual(
            {row["path"]: row["change"] for row in diff},
            {"a": "changed", "added": "added", "removed": "removed"},
        )

    def test_history_compaction_preserves_latest_known_good_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigVersionStore(
                Path(tmp) / "versions.sqlite3",
                history_limit=10,
            )
            baseline = store.record(
                {"risk_overrides": {"max_order_quote": 1.0}},
                known_good=True,
            )
            for value in range(2, 15):
                store.record({"risk_overrides": {"max_order_quote": float(value)}})

            known_good = store.latest_known_good()

            self.assertIsNotNone(known_good)
            self.assertEqual(known_good["id"], baseline["id"])
