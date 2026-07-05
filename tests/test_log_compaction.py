from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
