from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .order_reliability import IN_FLIGHT_GRACE_SECONDS


def inspect_order_intents(
    path: str | Path,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Inspect the order journal without treating active submissions as uncertain."""
    journal_path = Path(path).expanduser().resolve()
    observed_at = time.time() if now is None else float(now)
    result: dict[str, Any] = {
        "path": str(journal_path),
        "blocking_count": 0,
        "unknown_count": 0,
        "stale_reserved_count": 0,
        "in_flight_count": 0,
        "grace_seconds": IN_FLIGHT_GRACE_SECONDS,
        "observed_at": observed_at,
    }
    if not journal_path.exists():
        return result

    uri = f"file:{quote(str(journal_path), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'order_intents'"
        ).fetchone()
        if table is None:
            return result
        stale_before = observed_at - IN_FLIGHT_GRACE_SECONDS
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END),
                SUM(
                    CASE
                        WHEN status = 'reserved' AND updated_at <= ? THEN 1
                        ELSE 0
                    END
                ),
                SUM(
                    CASE
                        WHEN status = 'reserved' AND updated_at > ? THEN 1
                        ELSE 0
                    END
                )
            FROM order_intents
            """,
            (stale_before, stale_before),
        ).fetchone()

    unknown_count = int((row or (0, 0, 0))[0] or 0)
    stale_reserved_count = int((row or (0, 0, 0))[1] or 0)
    in_flight_count = int((row or (0, 0, 0))[2] or 0)
    result.update(
        {
            "blocking_count": unknown_count + stale_reserved_count,
            "unknown_count": unknown_count,
            "stale_reserved_count": stale_reserved_count,
            "in_flight_count": in_flight_count,
        }
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail when an order journal contains deployment-blocking intents"
    )
    parser.add_argument("journal", help="Path to order_intents.sqlite3")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = inspect_order_intents(args.journal)
    if result["in_flight_count"]:
        print(
            "deployment observed "
            f"{result['in_flight_count']} active order submission(s) within the "
            f"{result['grace_seconds']:.0f}s grace period"
        )
    if result["blocking_count"]:
        print(
            "deployment blocked by "
            f"{result['blocking_count']} uncertain order intent(s) "
            f"(unknown={result['unknown_count']}, "
            f"stale_reserved={result['stale_reserved_count']})",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
