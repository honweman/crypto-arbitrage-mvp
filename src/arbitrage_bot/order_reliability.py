from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


RECOVERY_WAIT_SECONDS = 5.0
DEFAULT_TERMINAL_RETENTION_SECONDS = 14 * 24 * 60 * 60
DEFAULT_MAX_TERMINAL_ROWS = 200_000
COMPACT_EVERY_RESERVATIONS = 1_000


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def order_intent_hash(intent: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(intent).encode("utf-8")).hexdigest()


class OrderIntentStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._ready = False
        self._reservation_count = 0

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    client_order_id TEXT PRIMARY KEY,
                    intent_hash TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount REAL NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    retry_after REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_order_intents_status
                    ON order_intents(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_order_intents_order
                    ON order_intents(exchange, symbol, order_id);
                """
            )
            connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._ready = True

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "client_order_id": str(row["client_order_id"]),
            "intent_hash": str(row["intent_hash"]),
            "exchange": str(row["exchange"]),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "amount": float(row["amount"]),
            "price": float(row["price"]),
            "status": str(row["status"]),
            "order_id": str(row["order_id"] or ""),
            "attempt_count": int(row["attempt_count"]),
            "last_error": str(row["last_error"] or ""),
            "response": json.loads(row["response_json"] or "{}"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "retry_after": float(row["retry_after"] or 0.0),
        }

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._row(row) if row else None

    def reserve(
        self,
        client_order_id: str,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure()
        intent_hash = order_intent_hash(intent)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if row is not None:
                existing = self._row(row)
                if existing["intent_hash"] != intent_hash:
                    raise ValueError(
                        f"client order id collision with different order: {client_order_id}"
                    )
                if existing["status"] in {"submitted", "recovered", "canceled"}:
                    connection.commit()
                    return {"action": "return_existing", **existing}
                if existing["status"] == "failed":
                    connection.commit()
                    return {"action": "failed", **existing}
                if existing["retry_after"] > now:
                    connection.commit()
                    return {"action": "recover_only", **existing}
                connection.execute(
                    """
                    UPDATE order_intents
                    SET status = 'reserved',
                        attempt_count = attempt_count + 1,
                        updated_at = ?,
                        retry_after = ?
                    WHERE client_order_id = ?
                    """,
                    (now, now + RECOVERY_WAIT_SECONDS, client_order_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO order_intents(
                        client_order_id,
                        intent_hash,
                        exchange,
                        symbol,
                        side,
                        amount,
                        price,
                        status,
                        attempt_count,
                        created_at,
                        updated_at,
                        retry_after
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?, ?)
                    """,
                    (
                        client_order_id,
                        intent_hash,
                        str(intent.get("exchange") or ""),
                        str(intent.get("symbol") or ""),
                        str(intent.get("side") or ""),
                        float(intent.get("amount") or 0.0),
                        float(intent.get("price") or 0.0),
                        now,
                        now,
                        now + RECOVERY_WAIT_SECONDS,
                    ),
                )
            connection.commit()
        current = self.get(client_order_id)
        self._reservation_count += 1
        if self._reservation_count % COMPACT_EVERY_RESERVATIONS == 0:
            self.compact()
        return {"action": "submit", **(current or {})}

    def mark_submitted(
        self,
        client_order_id: str,
        response: dict[str, Any],
        *,
        recovered: bool = False,
    ) -> dict[str, Any]:
        self._ensure()
        now = time.time()
        order_id = str(response.get("id") or response.get("order") or "")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE order_intents
                SET status = ?, order_id = ?, response_json = ?,
                    last_error = '', updated_at = ?, retry_after = 0
                WHERE client_order_id = ?
                """,
                (
                    "recovered" if recovered else "submitted",
                    order_id,
                    _canonical(response),
                    now,
                    client_order_id,
                ),
            )
            connection.commit()
        return self.get(client_order_id) or {}

    def mark_unknown(self, client_order_id: str, error: str) -> dict[str, Any]:
        self._ensure()
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE order_intents
                SET status = 'unknown', last_error = ?, updated_at = ?, retry_after = ?
                WHERE client_order_id = ?
                """,
                (str(error)[:500], now, now + RECOVERY_WAIT_SECONDS, client_order_id),
            )
            connection.commit()
        return self.get(client_order_id) or {}

    def mark_failed(self, client_order_id: str, error: str) -> dict[str, Any]:
        self._ensure()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE order_intents
                SET status = 'failed', last_error = ?, updated_at = ?, retry_after = 0
                WHERE client_order_id = ?
                """,
                (str(error)[:500], time.time(), client_order_id),
            )
            connection.commit()
        return self.get(client_order_id) or {}

    def mark_canceled_by_order_id(
        self,
        *,
        exchange: str,
        symbol: str,
        order_id: str,
        response: dict[str, Any] | None = None,
    ) -> None:
        self._ensure()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE order_intents
                SET status = 'canceled', response_json = ?, updated_at = ?, retry_after = 0
                WHERE exchange = ? AND symbol = ? AND order_id = ?
                """,
                (
                    _canonical(response or {"id": order_id, "status": "canceled"}),
                    time.time(),
                    exchange,
                    symbol,
                    order_id,
                ),
            )
            connection.commit()

    def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM order_intents
                WHERE status IN ('reserved', 'unknown')
                ORDER BY updated_at ASC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        self._ensure()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM order_intents GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "path": str(self.path),
            "counts": counts,
            "pending_count": counts.get("reserved", 0) + counts.get("unknown", 0),
            "total_count": sum(counts.values()),
        }

    def compact(
        self,
        *,
        terminal_retention_seconds: float = DEFAULT_TERMINAL_RETENTION_SECONDS,
        max_terminal_rows: int = DEFAULT_MAX_TERMINAL_ROWS,
    ) -> dict[str, int]:
        self._ensure()
        cutoff = time.time() - max(0.0, float(terminal_retention_seconds))
        row_limit = max(1_000, int(max_terminal_rows))
        with self._connect() as connection:
            expired = connection.execute(
                """
                DELETE FROM order_intents
                WHERE status NOT IN ('reserved', 'unknown')
                  AND updated_at < ?
                """,
                (cutoff,),
            ).rowcount
            overflow = connection.execute(
                """
                DELETE FROM order_intents
                WHERE rowid IN (
                    SELECT rowid FROM order_intents
                    WHERE status NOT IN ('reserved', 'unknown')
                    ORDER BY updated_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (row_limit,),
            ).rowcount
            connection.commit()
        return {
            "expired_removed": max(0, int(expired or 0)),
            "overflow_removed": max(0, int(overflow or 0)),
        }
