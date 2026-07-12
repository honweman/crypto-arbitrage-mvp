from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


CONFIG_VERSION_SCHEMA = 1
DEFAULT_HISTORY_LIMIT = 100


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def configuration_payload(runtime_payload: dict[str, Any]) -> dict[str, Any]:
    """Return only durable configuration fields, excluding operational state."""
    excluded = {"updated_at", "program"}
    return {
        str(key): value for key, value in runtime_payload.items() if key not in excluded
    }


def configuration_hash(payload: dict[str, Any]) -> str:
    encoded = _canonical_payload(configuration_payload(payload)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _flatten(value: Any, *, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(value[key], prefix=child))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            child = f"{prefix}[{index}]"
            result.update(_flatten(item, prefix=child))
        if not value:
            result[prefix] = []
        return result
    return {prefix: value}


def configuration_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    limit: int = 250,
) -> list[dict[str, Any]]:
    before_rows = _flatten(configuration_payload(before or {}))
    after_rows = _flatten(configuration_payload(after or {}))
    rows: list[dict[str, Any]] = []
    missing = object()
    for path in sorted(set(before_rows) | set(after_rows)):
        old = before_rows.get(path, missing)
        new = after_rows.get(path, missing)
        if old == new:
            continue
        rows.append(
            {
                "path": path,
                "before": None if old is missing else old,
                "after": None if new is missing else new,
                "change": (
                    "added"
                    if old is missing
                    else "removed"
                    if new is missing
                    else "changed"
                ),
            }
        )
        if len(rows) >= max(1, limit):
            break
    return rows


class ConfigVersionStore:
    def __init__(self, path: str | Path, *, history_limit: int = DEFAULT_HISTORY_LIMIT):
        self.path = Path(path)
        self.history_limit = max(10, int(history_limit))
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS config_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    actor_email TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    parent_id INTEGER,
                    known_good INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    diff_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_config_versions_created
                    ON config_versions(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_config_versions_hash
                    ON config_versions(payload_hash);
                """
            )
            connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._ready = True

    @staticmethod
    def _public_row(
        row: sqlite3.Row, *, include_payload: bool = False
    ) -> dict[str, Any]:
        diff = json.loads(row["diff_json"])
        payload = {
            "id": int(row["id"]),
            "schema_version": int(row["schema_version"]),
            "created_at": float(row["created_at"]),
            "actor_email": str(row["actor_email"] or ""),
            "action": str(row["action"] or ""),
            "hash": str(row["payload_hash"]),
            "parent_id": int(row["parent_id"])
            if row["parent_id"] is not None
            else None,
            "known_good": bool(row["known_good"]),
            "change_count": len(diff),
            "diff": diff,
        }
        if include_payload:
            payload["payload"] = json.loads(row["payload_json"])
        return payload

    def latest(self, *, include_payload: bool = False) -> dict[str, Any] | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM config_versions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._public_row(row, include_payload=include_payload) if row else None

    def get(
        self, version_id: int, *, include_payload: bool = False
    ) -> dict[str, Any] | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM config_versions WHERE id = ?",
                (int(version_id),),
            ).fetchone()
        return self._public_row(row, include_payload=include_payload) if row else None

    def list(self, *, limit: int = 30) -> list[dict[str, Any]]:
        self._ensure()
        target = max(1, min(int(limit), self.history_limit))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM config_versions ORDER BY id DESC LIMIT ?",
                (target,),
            ).fetchall()
        return [self._public_row(row) for row in rows]

    def record(
        self,
        runtime_payload: dict[str, Any],
        *,
        actor_email: str = "system",
        action: str = "runtime_update",
        known_good: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        self._ensure()
        payload = configuration_payload(runtime_payload)
        payload_text = _canonical_payload(payload)
        payload_hash = configuration_hash(payload)
        now = time.time()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT * FROM config_versions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if (
                previous is not None
                and previous["payload_hash"] == payload_hash
                and not force
            ):
                if known_good and not bool(previous["known_good"]):
                    connection.execute(
                        "UPDATE config_versions SET known_good = 1 WHERE id = ?",
                        (int(previous["id"]),),
                    )
                    connection.commit()
                    previous = connection.execute(
                        "SELECT * FROM config_versions WHERE id = ?",
                        (int(previous["id"]),),
                    ).fetchone()
                return self._public_row(previous)
            previous_payload = (
                json.loads(previous["payload_json"]) if previous is not None else {}
            )
            diff = configuration_diff(previous_payload, payload)
            cursor = connection.execute(
                """
                INSERT INTO config_versions(
                    schema_version,
                    created_at,
                    actor_email,
                    action,
                    payload_hash,
                    parent_id,
                    known_good,
                    payload_json,
                    diff_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    CONFIG_VERSION_SCHEMA,
                    now,
                    str(actor_email or "system")[:160],
                    str(action or "runtime_update")[:160],
                    payload_hash,
                    int(previous["id"]) if previous is not None else None,
                    1 if known_good else 0,
                    payload_text,
                    _canonical_payload(diff),
                ),
            )
            version_id = int(cursor.lastrowid)
            connection.execute(
                """
                DELETE FROM config_versions
                WHERE id NOT IN (
                    SELECT id FROM config_versions ORDER BY id DESC LIMIT ?
                )
                AND id != COALESCE(
                    (
                        SELECT id FROM config_versions
                        WHERE known_good = 1
                        ORDER BY id DESC LIMIT 1
                    ),
                    -1
                )
                """,
                (self.history_limit,),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM config_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
        return self._public_row(row)

    def mark_known_good(self, version_id: int) -> dict[str, Any]:
        self._ensure()
        with self._connect() as connection:
            connection.execute(
                "UPDATE config_versions SET known_good = 1 WHERE id = ?",
                (int(version_id),),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM config_versions WHERE id = ?",
                (int(version_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"configuration version not found: {version_id}")
        return self._public_row(row)

    def latest_known_good(
        self, *, before_id: int | None = None
    ) -> dict[str, Any] | None:
        self._ensure()
        query = "SELECT * FROM config_versions WHERE known_good = 1"
        params: tuple[Any, ...] = ()
        if before_id is not None:
            query += " AND id < ?"
            params = (int(before_id),)
        query += " ORDER BY id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._public_row(row, include_payload=True) if row else None
