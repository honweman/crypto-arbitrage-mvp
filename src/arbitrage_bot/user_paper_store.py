from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .user_strategies import UserStrategy


MAX_EVENTS_PER_STRATEGY = 500
MAX_FILLS_PER_STRATEGY = 5_000
PUBLIC_EVENT_LIMIT = 50
PUBLIC_FILL_LIMIT = 30
_NO_STATE_EXPECTATION = object()


class UserPaperStateConflict(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _clean_text(value: Any, *, max_length: int = 240) -> str:
    return str(value or "").strip()[:max_length]


class UserPaperTradingStore:
    """Persistent, user-scoped paper state with no exchange credentials."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
                CREATE TABLE IF NOT EXISTS user_paper_states (
                    strategy_id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_paper_states_owner
                    ON user_paper_states(owner_email);
                CREATE TABLE IF NOT EXISTS user_paper_fills (
                    fill_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    owner_email TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    gross_quote REAL NOT NULL,
                    fee_quote REAL NOT NULL,
                    quote_currency TEXT NOT NULL,
                    realized_pnl_common REAL NOT NULL,
                    filled_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_paper_fills_strategy
                    ON user_paper_fills(strategy_id, filled_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_paper_fills_owner
                    ON user_paper_fills(owner_email, filled_at DESC);
                CREATE TABLE IF NOT EXISTS user_paper_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    owner_email TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_paper_events_strategy
                    ON user_paper_events(strategy_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_paper_events_owner
                    ON user_paper_events(owner_email, created_at DESC);
                """
            )
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._ready = True

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _scope(owner_email: str, is_admin: bool) -> tuple[str, tuple[Any, ...]]:
        if is_admin:
            return "", ()
        owner = str(owner_email or "").strip().lower()
        if not owner or "@" not in owner:
            raise ValueError("owner email is required")
        return " WHERE owner_email = ?", (owner,)

    def get_state(self, strategy_id: str) -> dict[str, Any] | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_paper_states WHERE strategy_id = ?",
                (str(strategy_id),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def persist_cycle(
        self,
        strategy: UserStrategy,
        state: dict[str, Any],
        *,
        fills: list[dict[str, Any]] | None = None,
        event: dict[str, Any] | None = None,
        expected_state_updated_at: float | None | object = _NO_STATE_EXPECTATION,
    ) -> dict[str, int]:
        self._ensure()
        now = _now()
        owner = strategy.owner_email
        state_payload = dict(state)
        state_payload.update(
            {
                "strategy_id": strategy.id,
                "owner_email": owner,
                "project_id": strategy.project_id,
                "strategy_type": strategy.strategy_type,
                "mode": "paper",
                "live_submit_allowed": False,
                "updated_at": float(state_payload.get("updated_at") or now),
            }
        )
        run_id = _clean_text(state_payload.get("run_id"), max_length=80)
        if not run_id:
            raise ValueError("paper state run_id is required")
        status = _clean_text(state_payload.get("status"), max_length=80) or "unknown"
        stored_fills = 0
        stored_events = 0

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT owner_email, updated_at FROM user_paper_states WHERE strategy_id = ?",
                (strategy.id,),
            ).fetchone()
            if existing is not None and existing["owner_email"] != owner:
                raise ValueError("paper strategy owner cannot be changed")
            if expected_state_updated_at is not _NO_STATE_EXPECTATION:
                if expected_state_updated_at is None and existing is not None:
                    raise UserPaperStateConflict("paper state was created concurrently")
                if expected_state_updated_at is not None and (
                    existing is None
                    or not math.isclose(
                        float(existing["updated_at"]),
                        float(expected_state_updated_at),
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                ):
                    raise UserPaperStateConflict("paper state changed concurrently")
            connection.execute(
                """
                INSERT INTO user_paper_states(
                    strategy_id, owner_email, project_id, strategy_type,
                    status, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    owner_email = excluded.owner_email,
                    project_id = excluded.project_id,
                    strategy_type = excluded.strategy_type,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    strategy.id,
                    owner,
                    strategy.project_id,
                    strategy.strategy_type,
                    status,
                    state_payload["updated_at"],
                    self._dump(state_payload),
                ),
            )

            for raw_fill in fills or []:
                fill = dict(raw_fill)
                fill_id = _clean_text(fill.get("fill_id"), max_length=100)
                if not fill_id:
                    raise ValueError("paper fill_id is required")
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO user_paper_fills(
                        fill_id, strategy_id, run_id, owner_email, account_id,
                        exchange, symbol, side, price, amount, gross_quote,
                        fee_quote, quote_currency, realized_pnl_common,
                        filled_at, payload
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill_id,
                        strategy.id,
                        run_id,
                        owner,
                        _clean_text(fill.get("account_id"), max_length=80),
                        _clean_text(fill.get("exchange"), max_length=80),
                        _clean_text(fill.get("symbol"), max_length=80),
                        _clean_text(fill.get("side"), max_length=8),
                        float(fill.get("price") or 0.0),
                        float(fill.get("amount") or 0.0),
                        float(fill.get("gross_quote") or 0.0),
                        float(fill.get("fee_quote") or 0.0),
                        _clean_text(fill.get("quote_currency"), max_length=20),
                        float(fill.get("realized_pnl_common") or 0.0),
                        float(fill.get("filled_at") or now),
                        self._dump(fill),
                    ),
                )
                stored_fills += max(0, cursor.rowcount)

            if event is not None:
                event_payload = dict(event)
                event_key = _clean_text(event_payload.get("event_key"), max_length=100)
                if not event_key:
                    raise ValueError("paper event_key is required")
                event_payload.update(
                    {
                        "strategy_id": strategy.id,
                        "run_id": run_id,
                        "owner_email": owner,
                        "project_id": strategy.project_id,
                        "mode": "paper",
                        "live_submit_allowed": False,
                    }
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO user_paper_events(
                        event_key, strategy_id, run_id, owner_email, project_id,
                        event_type, status, reason, created_at, payload
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        strategy.id,
                        run_id,
                        owner,
                        strategy.project_id,
                        _clean_text(event_payload.get("event_type"), max_length=80),
                        _clean_text(event_payload.get("status"), max_length=80),
                        _clean_text(event_payload.get("reason")),
                        float(event_payload.get("created_at") or now),
                        self._dump(event_payload),
                    ),
                )
                stored_events += max(0, cursor.rowcount)

            if stored_fills or stored_events:
                self._prune_strategy(connection, strategy.id)
            connection.commit()
        return {"stored_fills": stored_fills, "stored_events": stored_events}

    @staticmethod
    def _prune_strategy(connection: sqlite3.Connection, strategy_id: str) -> None:
        connection.execute(
            """
            DELETE FROM user_paper_events
            WHERE strategy_id = ? AND id NOT IN (
                SELECT id FROM user_paper_events
                WHERE strategy_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            """,
            (strategy_id, strategy_id, MAX_EVENTS_PER_STRATEGY),
        )
        connection.execute(
            """
            DELETE FROM user_paper_fills
            WHERE strategy_id = ? AND fill_id NOT IN (
                SELECT fill_id FROM user_paper_fills
                WHERE strategy_id = ?
                ORDER BY filled_at DESC, fill_id DESC
                LIMIT ?
            )
            """,
            (strategy_id, strategy_id, MAX_FILLS_PER_STRATEGY),
        )

    def counts(self, strategy_id: str) -> dict[str, int]:
        self._ensure()
        with self._connect() as connection:
            return self._counts_in_connection(connection, strategy_id)

    @staticmethod
    def _counts_in_connection(
        connection: sqlite3.Connection,
        strategy_id: str,
    ) -> dict[str, int]:
        fill_count = connection.execute(
            "SELECT COUNT(*) FROM user_paper_fills WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM user_paper_events WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()[0]
        state_count = connection.execute(
            "SELECT COUNT(*) FROM user_paper_states WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()[0]
        return {
            "state_count": int(state_count),
            "fill_count": int(fill_count),
            "event_count": int(event_count),
        }

    def reset_strategy(
        self,
        strategy: UserStrategy,
        *,
        reason: str = "paper simulation reset by user",
    ) -> dict[str, int]:
        self._ensure()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            counts = self._counts_in_connection(connection, strategy.id)
            connection.execute(
                "DELETE FROM user_paper_states WHERE strategy_id = ?",
                (strategy.id,),
            )
            connection.execute(
                "DELETE FROM user_paper_fills WHERE strategy_id = ?",
                (strategy.id,),
            )
            connection.execute(
                "DELETE FROM user_paper_events WHERE strategy_id = ?",
                (strategy.id,),
            )
            created_at = _now()
            event = {
                "event_key": f"reset:{strategy.id}:{created_at:.6f}",
                "strategy_id": strategy.id,
                "run_id": "reset",
                "owner_email": strategy.owner_email,
                "project_id": strategy.project_id,
                "event_type": "reset",
                "status": "reset",
                "reason": _clean_text(reason),
                "created_at": created_at,
                "mode": "paper",
                "live_submit_allowed": False,
            }
            connection.execute(
                """
                INSERT INTO user_paper_events(
                    event_key, strategy_id, run_id, owner_email, project_id,
                    event_type, status, reason, created_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_key"],
                    strategy.id,
                    "reset",
                    strategy.owner_email,
                    strategy.project_id,
                    "reset",
                    "reset",
                    event["reason"],
                    created_at,
                    self._dump(event),
                ),
            )
            connection.commit()
        return counts

    def delete_strategy(self, strategy_id: str) -> None:
        self._ensure()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in (
                "user_paper_states",
                "user_paper_fills",
                "user_paper_events",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE strategy_id = ?",  # noqa: S608
                    (strategy_id,),
                )
            connection.commit()

    def public_payload(self, *, owner_email: str, is_admin: bool) -> dict[str, Any]:
        self._ensure()
        where, params = self._scope(owner_email, is_admin)
        with self._connect() as connection:
            state_rows = connection.execute(
                "SELECT payload FROM user_paper_states"
                + where
                + " ORDER BY updated_at DESC, strategy_id ASC",
                params,
            ).fetchall()
            event_rows = connection.execute(
                "SELECT payload FROM user_paper_events"
                + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, PUBLIC_EVENT_LIMIT),
            ).fetchall()
            fill_rows = connection.execute(
                "SELECT payload FROM user_paper_fills"
                + where
                + " ORDER BY filled_at DESC, fill_id DESC LIMIT ?",
                (*params, PUBLIC_FILL_LIMIT),
            ).fetchall()
            fill_count_rows = connection.execute(
                "SELECT strategy_id, COUNT(*) AS count FROM user_paper_fills"
                + where
                + " GROUP BY strategy_id",
                params,
            ).fetchall()
            event_count_rows = connection.execute(
                "SELECT strategy_id, COUNT(*) AS count FROM user_paper_events"
                + where
                + " GROUP BY strategy_id",
                params,
            ).fetchall()
        states = [json.loads(row["payload"]) for row in state_rows]
        events = [json.loads(row["payload"]) for row in event_rows]
        fills = [json.loads(row["payload"]) for row in fill_rows]
        counts: dict[str, dict[str, int]] = {
            str(state["strategy_id"]): {
                "state_count": 1,
                "fill_count": 0,
                "event_count": 0,
            }
            for state in states
        }
        for row in fill_count_rows:
            counts.setdefault(
                str(row["strategy_id"]),
                {"state_count": 0, "fill_count": 0, "event_count": 0},
            )["fill_count"] = int(row["count"])
        for row in event_count_rows:
            counts.setdefault(
                str(row["strategy_id"]),
                {"state_count": 0, "fill_count": 0, "event_count": 0},
            )["event_count"] = int(row["count"])
        currencies = {
            str(state.get("common_quote_currency") or "")
            for state in states
            if state.get("common_quote_currency")
        }
        total_pnl = sum(float(state.get("total_pnl_common") or 0.0) for state in states)
        daily_pnl = sum(float(state.get("daily_pnl_common") or 0.0) for state in states)
        return {
            "status": "ok",
            "mode": "paper",
            "live_submit_allowed": False,
            "states": states,
            "events": events,
            "recent_fills": fills,
            "counts": counts,
            "summary": {
                "state_count": len(states),
                "running_count": sum(
                    1
                    for state in states
                    if state.get("status")
                    in {"running", "orders_active", "waiting"}
                ),
                "complete_count": sum(
                    1 for state in states if state.get("status") == "complete"
                ),
                "blocked_count": sum(
                    1
                    for state in states
                    if str(state.get("status") or "").startswith("blocked")
                    or state.get("status") == "error"
                ),
                "fill_count": sum(int(state.get("fill_count") or 0) for state in states),
                "open_order_count": sum(
                    int(state.get("open_order_count") or 0) for state in states
                ),
                "total_pnl_common": total_pnl,
                "daily_pnl_common": daily_pnl,
                "common_quote_currency": (
                    next(iter(currencies)) if len(currencies) == 1 else "MULTI"
                ),
            },
        }
