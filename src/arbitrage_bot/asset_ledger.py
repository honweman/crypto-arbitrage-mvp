from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .config import AssetLedgerConfig


SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stable_key(*parts: Any) -> str:
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()[:40]


def _connect(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    conn.execute("pragma busy_timeout = 10000")
    conn.execute("pragma foreign_keys = on")
    return conn


def init_asset_ledger(path: str) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        with closing(sqlite3.connect(str(db_path), timeout=10.0)) as bootstrap:
            bootstrap.execute("pragma auto_vacuum = incremental")
            bootstrap.commit()
    with closing(_connect(path)) as conn:
        conn.executescript(
            """
            create table if not exists ledger_meta (
                key text primary key,
                value text not null,
                updated_at real not null
            );

            create table if not exists ledger_events (
                event_key text primary key,
                event_type text not null,
                account_key text not null,
                symbol text not null default '',
                external_id text not null default '',
                effective_at real,
                observed_at real not null,
                source text not null,
                payload_json text not null
            );
            create index if not exists idx_ledger_events_account_time
                on ledger_events(account_key, observed_at desc);
            create index if not exists idx_ledger_events_type_time
                on ledger_events(event_type, observed_at desc);

            create table if not exists balance_snapshots (
                snapshot_id text primary key,
                account_key text not null,
                observed_at real not null,
                status text not null,
                source text not null,
                payload_json text not null
            );
            create index if not exists idx_balance_snapshots_account_time
                on balance_snapshots(account_key, observed_at desc);
            create index if not exists idx_balance_snapshots_account_source_time
                on balance_snapshots(account_key, source, observed_at desc);

            create table if not exists balance_rows (
                snapshot_id text not null references balance_snapshots(snapshot_id)
                    on delete cascade,
                account_key text not null,
                currency text not null,
                free real,
                used real,
                total real,
                exchange_free real,
                exchange_used real,
                exchange_total real,
                open_order_reserved real,
                primary key(snapshot_id, currency)
            );
            create index if not exists idx_balance_rows_account_currency
                on balance_rows(account_key, currency);

            create table if not exists order_snapshots (
                snapshot_id text primary key,
                account_key text not null,
                observed_at real not null,
                status text not null,
                source text not null,
                payload_json text not null
            );
            create index if not exists idx_order_snapshots_account_time
                on order_snapshots(account_key, observed_at desc);
            create index if not exists idx_order_snapshots_account_source_time
                on order_snapshots(account_key, source, observed_at desc);

            create table if not exists order_rows (
                snapshot_id text not null references order_snapshots(snapshot_id)
                    on delete cascade,
                account_key text not null,
                bucket text not null,
                order_id text not null,
                client_order_id text not null default '',
                symbol text not null default '',
                side text not null default '',
                status text not null default '',
                price real,
                amount real,
                filled real,
                remaining real,
                cost real,
                timestamp_ms real,
                payload_json text not null,
                primary key(snapshot_id, bucket, order_id, symbol)
            );
            create index if not exists idx_order_rows_account_status
                on order_rows(account_key, bucket, status);

            create table if not exists ledger_fills (
                fill_key text primary key,
                account_key text not null,
                trade_id text not null default '',
                order_id text not null default '',
                symbol text not null default '',
                side text not null default '',
                source text not null default 'unattributed',
                price real,
                amount real,
                cost real,
                fee_cost real,
                fee_currency text not null default '',
                notional_common real,
                realized_pnl_common real,
                timestamp_ms real,
                first_observed_at real not null,
                last_observed_at real not null,
                payload_json text not null
            );
            create index if not exists idx_ledger_fills_account_time
                on ledger_fills(account_key, timestamp_ms desc);
            create index if not exists idx_ledger_fills_order
                on ledger_fills(account_key, symbol, order_id);

            create table if not exists fill_source_observations (
                fill_key text not null references ledger_fills(fill_key)
                    on delete cascade,
                source text not null,
                first_observed_at real not null,
                last_observed_at real not null,
                primary key(fill_key, source)
            );
            create index if not exists idx_fill_source_observations_source_time
                on fill_source_observations(source, first_observed_at);

            create table if not exists position_snapshots (
                snapshot_id text primary key,
                observed_at real not null,
                status text not null,
                source text not null,
                payload_json text not null
            );
            create table if not exists position_rows (
                snapshot_id text not null references position_snapshots(snapshot_id)
                    on delete cascade,
                asset text not null,
                position_base real,
                average_entry_price real,
                mark_price real,
                position_value real,
                price_move_pnl real,
                payload_json text not null,
                primary key(snapshot_id, asset)
            );
            create index if not exists idx_position_rows_asset
                on position_rows(asset);

            create table if not exists pnl_snapshots (
                snapshot_id text primary key,
                observed_at real not null,
                currency text not null,
                total_pnl real,
                realized_pnl real,
                total_fees real,
                source_json text not null,
                payload_json text not null
            );
            create index if not exists idx_pnl_snapshots_time
                on pnl_snapshots(observed_at desc);

            create table if not exists reconciliation_runs (
                run_id text primary key,
                account_key text not null,
                observed_at real not null,
                status text not null,
                source text not null,
                balance_snapshot_id text,
                order_snapshot_id text,
                diff_count integer not null,
                error_json text not null
            );
            create index if not exists idx_reconciliation_account_time
                on reconciliation_runs(account_key, observed_at desc);

            create table if not exists reconciliation_diffs (
                run_id text not null references reconciliation_runs(run_id)
                    on delete cascade,
                category text not null,
                item_key text not null,
                expected real,
                observed real,
                delta real,
                severity text not null,
                message text not null,
                primary key(run_id, category, item_key)
            );

            create table if not exists monitor_checkpoints (
                checkpoint_id text primary key,
                observed_at real not null,
                status text not null,
                source text not null,
                account_balances_json text not null,
                order_activity_json text not null,
                portfolio_json text not null
            );
            create index if not exists idx_monitor_checkpoints_time
                on monitor_checkpoints(observed_at desc);

            create table if not exists worker_heartbeats (
                worker_id text primary key,
                account_key text not null,
                pid integer,
                status text not null,
                last_started_at real,
                last_success_at real,
                last_error_at real,
                next_due_at real,
                cycle_count integer not null default 0,
                error_count integer not null default 0,
                last_error text not null default '',
                metadata_json text not null default '{}',
                updated_at real not null
            );
            create index if not exists idx_worker_heartbeats_account
                on worker_heartbeats(account_key, updated_at desc);
            """
        )
        conn.execute(
            """
            insert into ledger_meta(key, value, updated_at) values('schema_version', ?, ?)
            on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(SCHEMA_VERSION), time.time()),
        )
        conn.commit()


def _account_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for account in (payload or {}).get("accounts", []) or []:
        if not isinstance(account, dict):
            continue
        key = str(account.get("exchange") or account.get("account_key") or "")
        if key:
            result[key] = account
    return result


def _fill_key(account_key: str, trade: dict[str, Any]) -> str:
    return _stable_key(
        account_key,
        trade.get("id"),
        trade.get("order_id"),
        trade.get("symbol"),
        trade.get("side"),
        trade.get("price"),
        trade.get("amount"),
        trade.get("timestamp"),
    )


def _row_discrepancy(row: dict[str, Any]) -> float | None:
    free = _number(row.get("free"))
    used = _number(row.get("used"))
    total = _number(row.get("total"))
    if free is None or used is None or total is None:
        return None
    return total - free - used


class AssetLedgerStore:
    def __init__(self, cfg: AssetLedgerConfig) -> None:
        self.cfg = cfg
        if cfg.enabled and (
            not Path(cfg.path).is_file() or Path(cfg.path).stat().st_size == 0
        ):
            init_asset_ledger(cfg.path)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        account_key: str,
        payload: dict[str, Any],
        observed_at: float,
        source: str,
        symbol: str = "",
        external_id: str = "",
        effective_at: float | None = None,
    ) -> None:
        event_key = _stable_key(
            event_type,
            account_key,
            symbol,
            external_id,
            effective_at,
            observed_at,
            payload,
        )
        conn.execute(
            """
            insert or ignore into ledger_events(
                event_key, event_type, account_key, symbol, external_id,
                effective_at, observed_at, source, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                event_type,
                account_key,
                symbol,
                external_id,
                effective_at,
                observed_at,
                source,
                _json(payload),
            ),
        )

    def _record_balance(
        self,
        conn: sqlite3.Connection,
        account_key: str,
        account: dict[str, Any],
        observed_at: float,
        source: str,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        balance = account.get("balance") if isinstance(account.get("balance"), dict) else {}
        checked = bool(balance.get("checked"))
        rows = [row for row in balance.get("currencies", []) or [] if isinstance(row, dict)]
        if not checked or account.get("errors"):
            return None, []
        payload_json = _json(account)
        previous_header = conn.execute(
            """
            select snapshot_id, observed_at, payload_json from balance_snapshots
            where account_key = ? and source = ?
            order by observed_at desc limit 1
            """,
            (account_key, source),
        ).fetchone()
        if (
            previous_header is not None
            and previous_header["payload_json"] == payload_json
        ):
            return str(previous_header["snapshot_id"]), []
        previous_totals: dict[str, float] = {}
        has_source_fill_baseline = False
        if previous_header is not None:
            has_source_fill_baseline = bool(
                conn.execute(
                    """
                    select 1 from fill_source_observations
                    where source = ? and first_observed_at <= ? limit 1
                    """,
                    (source, float(previous_header["observed_at"])),
                ).fetchone()
            )
            previous_totals = {
                str(row["currency"]): float(row["total"])
                for row in conn.execute(
                    "select currency, total from balance_rows where snapshot_id = ?",
                    (previous_header["snapshot_id"],),
                ).fetchall()
                if row["total"] is not None
            }
        snapshot_id = _stable_key("balance", account_key, observed_at, account)
        conn.execute(
            """
            insert or ignore into balance_snapshots(
                snapshot_id, account_key, observed_at, status, source, payload_json
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                account_key,
                observed_at,
                str(account.get("status") or "ok"),
                source,
                payload_json,
            ),
        )
        diffs: list[dict[str, Any]] = []
        current_totals: dict[str, float] = {}
        for row in rows:
            currency = str(row.get("currency") or "").upper()
            if not currency:
                continue
            conn.execute(
                """
                insert or replace into balance_rows(
                    snapshot_id, account_key, currency, free, used, total,
                    exchange_free, exchange_used, exchange_total, open_order_reserved
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    account_key,
                    currency,
                    _number(row.get("free")),
                    _number(row.get("used")),
                    _number(row.get("total")),
                    _number(row.get("exchange_free")),
                    _number(row.get("exchange_used")),
                    _number(row.get("exchange_total")),
                    _number(row.get("open_order_reserved")),
                ),
            )
            if _number(row.get("total")) is not None:
                current_totals[currency] = float(row["total"])
            discrepancy = _row_discrepancy(row)
            tolerance = max(self.cfg.balance_abs_tolerance, abs(_number(row.get("total")) or 0.0) * self.cfg.balance_rel_tolerance)
            if discrepancy is not None and abs(discrepancy) > tolerance:
                diffs.append(
                    {
                        "category": "balance_identity",
                        "item_key": currency,
                        "expected": (_number(row.get("free")) or 0.0) + (_number(row.get("used")) or 0.0),
                        "observed": _number(row.get("total")),
                        "delta": discrepancy,
                        "severity": "warning",
                        "message": f"{currency} total differs from free + used",
                    }
                )
        if previous_header is not None and has_source_fill_baseline:
            fill_deltas: dict[str, float] = {}
            fills = conn.execute(
                """
                select f.* from ledger_fills f
                join fill_source_observations o on o.fill_key = f.fill_key
                where f.account_key = ? and o.source = ?
                    and o.first_observed_at > ? and o.first_observed_at <= ?
                """,
                (
                    account_key,
                    source,
                    float(previous_header["observed_at"]),
                    observed_at,
                ),
            ).fetchall()
            for fill in fills:
                symbol = str(fill["symbol"] or "")
                if "/" not in symbol:
                    continue
                base, quote = symbol.split("/", 1)
                base = base.upper()
                quote = quote.split(":", 1)[0].upper()
                amount = float(fill["amount"] or 0.0)
                cost = float(fill["cost"] or 0.0)
                side = str(fill["side"] or "").lower()
                if side == "buy":
                    fill_deltas[base] = fill_deltas.get(base, 0.0) + amount
                    fill_deltas[quote] = fill_deltas.get(quote, 0.0) - cost
                elif side == "sell":
                    fill_deltas[base] = fill_deltas.get(base, 0.0) - amount
                    fill_deltas[quote] = fill_deltas.get(quote, 0.0) + cost
                fee_currency = str(fill["fee_currency"] or "").upper()
                if fee_currency:
                    fill_deltas[fee_currency] = fill_deltas.get(
                        fee_currency, 0.0
                    ) - float(fill["fee_cost"] or 0.0)
            for currency in sorted(set(previous_totals) | set(current_totals)):
                if currency not in previous_totals or currency not in current_totals:
                    continue
                expected = previous_totals[currency] + fill_deltas.get(currency, 0.0)
                observed = current_totals[currency]
                delta = observed - expected
                tolerance = max(
                    self.cfg.balance_abs_tolerance,
                    abs(expected) * self.cfg.balance_rel_tolerance,
                )
                if abs(delta) > tolerance:
                    diffs.append(
                        {
                            "category": "unexplained_balance_change",
                            "item_key": currency,
                            "expected": expected,
                            "observed": observed,
                            "delta": delta,
                            "severity": "warning",
                            "message": (
                                f"{currency} exchange balance differs from prior "
                                "ledger balance plus newly observed fills"
                            ),
                        }
                    )
        self._insert_event(
            conn,
            event_type="balance_snapshot",
            account_key=account_key,
            payload=account,
            observed_at=observed_at,
            source=source,
            external_id=snapshot_id,
        )
        return snapshot_id, diffs

    def _record_orders(
        self,
        conn: sqlite3.Connection,
        account_key: str,
        account: dict[str, Any],
        observed_at: float,
        source: str,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if account.get("errors"):
            return None, []
        payload_json = _json(account)
        previous_header = conn.execute(
            """
            select snapshot_id, payload_json from order_snapshots
            where account_key = ? and source = ?
            order by observed_at desc limit 1
            """,
            (account_key, source),
        ).fetchone()
        unchanged_snapshot = bool(
            previous_header is not None
            and previous_header["payload_json"] == payload_json
        )
        snapshot_id = _stable_key("orders", account_key, observed_at, account)
        if unchanged_snapshot:
            snapshot_id = str(previous_header["snapshot_id"])
        else:
            conn.execute(
                """
                insert or ignore into order_snapshots(
                    snapshot_id, account_key, observed_at, status, source, payload_json
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    account_key,
                    observed_at,
                    str(account.get("status") or "ok"),
                    source,
                    payload_json,
                ),
            )
        diffs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for bucket in (() if unchanged_snapshot else ("open_orders", "closed_orders")):
            for index, order in enumerate(account.get(bucket, []) or []):
                if not isinstance(order, dict):
                    continue
                order_id = str(order.get("id") or order.get("order_id") or f"missing-{index}")
                symbol = str(order.get("symbol") or "")
                identity = (bucket, order_id, symbol)
                if identity in seen:
                    diffs.append(
                        {
                            "category": "duplicate_order",
                            "item_key": ":".join(identity),
                            "expected": 1.0,
                            "observed": 2.0,
                            "delta": 1.0,
                            "severity": "warning",
                            "message": "duplicate order id in exchange snapshot",
                        }
                    )
                    continue
                seen.add(identity)
                conn.execute(
                    """
                    insert or replace into order_rows(
                        snapshot_id, account_key, bucket, order_id, client_order_id,
                        symbol, side, status, price, amount, filled, remaining, cost,
                        timestamp_ms, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        account_key,
                        bucket,
                        order_id,
                        str(order.get("client_order_id") or order.get("clientOrderId") or ""),
                        symbol,
                        str(order.get("side") or ""),
                        str(order.get("status") or ""),
                        _number(order.get("price")),
                        _number(order.get("amount")),
                        _number(order.get("filled")),
                        _number(order.get("remaining")),
                        _number(order.get("cost")),
                        _number(order.get("timestamp")),
                        _json(order),
                    ),
                )
        for trade in account.get("recent_trades", []) or []:
            if not isinstance(trade, dict):
                continue
            fill_key = _fill_key(account_key, trade)
            fee = trade.get("fee") if isinstance(trade.get("fee"), dict) else {}
            source_observed = conn.execute(
                """
                select 1 from fill_source_observations
                where fill_key = ? and source = ? limit 1
                """,
                (fill_key, source),
            ).fetchone()
            conn.execute(
                """
                insert into ledger_fills(
                    fill_key, account_key, trade_id, order_id, symbol, side, source,
                    price, amount, cost, fee_cost, fee_currency, notional_common,
                    realized_pnl_common, timestamp_ms, first_observed_at,
                    last_observed_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(fill_key) do update set
                    source=case
                        when excluded.source = 'unattributed' then ledger_fills.source
                        else excluded.source
                    end,
                    notional_common=coalesce(
                        excluded.notional_common, ledger_fills.notional_common
                    ),
                    realized_pnl_common=coalesce(
                        excluded.realized_pnl_common,
                        ledger_fills.realized_pnl_common
                    ),
                    last_observed_at=max(
                        excluded.last_observed_at,
                        ledger_fills.last_observed_at
                    ),
                    payload_json=excluded.payload_json
                """,
                (
                    fill_key,
                    account_key,
                    str(trade.get("id") or ""),
                    str(trade.get("order_id") or ""),
                    str(trade.get("symbol") or ""),
                    str(trade.get("side") or ""),
                    str(trade.get("source") or "unattributed"),
                    _number(trade.get("price")),
                    _number(trade.get("amount")),
                    _number(trade.get("cost")),
                    _number(fee.get("cost")),
                    str(fee.get("currency") or ""),
                    _number(trade.get("notional_common")),
                    _number(trade.get("realized_pnl_common")),
                    _number(trade.get("timestamp")),
                    observed_at,
                    observed_at,
                    _json(trade),
                ),
            )
            conn.execute(
                """
                insert into fill_source_observations(
                    fill_key, source, first_observed_at, last_observed_at
                ) values (?, ?, ?, ?)
                on conflict(fill_key, source) do update set
                    last_observed_at=excluded.last_observed_at
                """,
                (fill_key, source, observed_at, observed_at),
            )
            if source_observed is None:
                self._insert_event(
                    conn,
                    event_type="fill_observed",
                    account_key=account_key,
                    payload=trade,
                    observed_at=observed_at,
                    source=source,
                    symbol=str(trade.get("symbol") or ""),
                    external_id=fill_key,
                    effective_at=_number(trade.get("timestamp")),
                )
        if not unchanged_snapshot:
            self._insert_event(
                conn,
                event_type="order_snapshot",
                account_key=account_key,
                payload=account,
                observed_at=observed_at,
                source=source,
                external_id=snapshot_id,
            )
        return snapshot_id, diffs

    def record_monitor_checkpoint(
        self,
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
        *,
        portfolio: dict[str, Any] | None = None,
        source: str = "web-monitor",
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        observed_at = float(observed_at or time.time())
        balance_accounts = _account_map(account_balances)
        order_accounts = _account_map(order_activity)
        account_keys = sorted(set(balance_accounts) | set(order_accounts))
        checkpoint_id = _stable_key(
            "checkpoint", observed_at, account_balances, order_activity, portfolio or {}
        )
        run_summaries: list[dict[str, Any]] = []
        with closing(_connect(self.cfg.path)) as conn:
            conn.execute("begin immediate")
            for account_key in account_keys:
                balance_account = balance_accounts.get(account_key, {})
                order_account = order_accounts.get(account_key, {})
                order_id, order_diffs = self._record_orders(
                    conn, account_key, order_account, observed_at, source
                )
                balance_id, balance_diffs = self._record_balance(
                    conn, account_key, balance_account, observed_at, source
                )
                errors = [
                    *[str(item) for item in balance_account.get("errors", []) or []],
                    *[str(item) for item in order_account.get("errors", []) or []],
                ]
                diffs = [*balance_diffs, *order_diffs]
                status = "error" if errors else ("warning" if diffs else "ok")
                run_id = _stable_key(
                    "reconciliation", account_key, observed_at, balance_id, order_id, errors
                )
                conn.execute(
                    """
                    insert or replace into reconciliation_runs(
                        run_id, account_key, observed_at, status, source,
                        balance_snapshot_id, order_snapshot_id, diff_count, error_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        account_key,
                        observed_at,
                        status,
                        source,
                        balance_id,
                        order_id,
                        len(diffs),
                        _json(errors),
                    ),
                )
                for diff in diffs:
                    conn.execute(
                        """
                        insert or replace into reconciliation_diffs(
                            run_id, category, item_key, expected, observed, delta,
                            severity, message
                        ) values (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            diff["category"],
                            diff["item_key"],
                            diff.get("expected"),
                            diff.get("observed"),
                            diff.get("delta"),
                            diff["severity"],
                            diff["message"],
                        ),
                    )
                run_summaries.append(
                    {
                        "account_key": account_key,
                        "status": status,
                        "diff_count": len(diffs),
                        "error_count": len(errors),
                        "run_id": run_id,
                    }
                )
            checkpoint_status = (
                "error"
                if any(row["status"] == "error" for row in run_summaries)
                else "warning"
                if any(row["status"] == "warning" for row in run_summaries)
                else "ok"
            )
            portfolio_payload = portfolio or {}
            position_snapshot_id = _stable_key(
                "positions", observed_at, portfolio_payload
            )
            conn.execute(
                """
                insert or ignore into position_snapshots(
                    snapshot_id, observed_at, status, source, payload_json
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    position_snapshot_id,
                    observed_at,
                    str(portfolio_payload.get("status") or "unknown"),
                    source,
                    _json(portfolio_payload),
                ),
            )
            for position in portfolio_payload.get("positions", []) or []:
                if not isinstance(position, dict):
                    continue
                asset = str(position.get("asset") or "").upper()
                if not asset:
                    continue
                conn.execute(
                    """
                    insert or replace into position_rows(
                        snapshot_id, asset, position_base, average_entry_price,
                        mark_price, position_value, price_move_pnl, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position_snapshot_id,
                        asset,
                        _number(position.get("position_base")),
                        _number(position.get("average_entry_price")),
                        _number(position.get("mark_price")),
                        _number(position.get("position_value")),
                        _number(position.get("price_move_pnl")),
                        _json(position),
                    ),
                )
            daily_pnl = (
                order_activity.get("daily_pnl")
                if isinstance(order_activity.get("daily_pnl"), dict)
                else {}
            )
            pnl_snapshot_id = _stable_key(
                "pnl", observed_at, daily_pnl, portfolio_payload.get("total_pnl")
            )
            conn.execute(
                """
                insert or ignore into pnl_snapshots(
                    snapshot_id, observed_at, currency, total_pnl, realized_pnl,
                    total_fees, source_json, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pnl_snapshot_id,
                    observed_at,
                    str(
                        daily_pnl.get("currency")
                        or portfolio_payload.get("quote_currency")
                        or "USD"
                    ),
                    _number(portfolio_payload.get("total_pnl")),
                    _number(daily_pnl.get("total_realized_pnl")),
                    _number(daily_pnl.get("total_fees")),
                    _json(daily_pnl.get("sources") or {}),
                    _json(
                        {
                            "daily_pnl": daily_pnl,
                            "portfolio": portfolio_payload,
                        }
                    ),
                ),
            )
            self._insert_event(
                conn,
                event_type="position_snapshot",
                account_key="portfolio",
                payload=portfolio_payload,
                observed_at=observed_at,
                source=source,
                external_id=position_snapshot_id,
            )
            self._insert_event(
                conn,
                event_type="pnl_snapshot",
                account_key="portfolio",
                payload={"daily_pnl": daily_pnl, "portfolio": portfolio_payload},
                observed_at=observed_at,
                source=source,
                external_id=pnl_snapshot_id,
            )
            conn.execute(
                """
                insert or replace into monitor_checkpoints(
                    checkpoint_id, observed_at, status, source,
                    account_balances_json, order_activity_json, portfolio_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    observed_at,
                    checkpoint_status,
                    source,
                    _json(account_balances),
                    _json(order_activity),
                    _json(portfolio or {}),
                ),
            )
            conn.commit()
        return {
            "enabled": True,
            "checkpoint_id": checkpoint_id,
            "status": checkpoint_status,
            "observed_at": observed_at,
            "accounts": run_summaries,
            "path": self.cfg.path,
        }

    def record_account_snapshot(
        self,
        *,
        account_key: str,
        balance_account: dict[str, Any],
        order_account: dict[str, Any],
        source: str,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        observed_at = float(observed_at or time.time())
        with closing(_connect(self.cfg.path)) as conn:
            conn.execute("begin immediate")
            order_id, order_diffs = self._record_orders(
                conn, account_key, order_account, observed_at, source
            )
            balance_id, balance_diffs = self._record_balance(
                conn, account_key, balance_account, observed_at, source
            )
            errors = [
                *[str(item) for item in balance_account.get("errors", []) or []],
                *[str(item) for item in order_account.get("errors", []) or []],
            ]
            diffs = [*balance_diffs, *order_diffs]
            status = "error" if errors else ("warning" if diffs else "ok")
            run_id = _stable_key(
                "reconciliation", account_key, observed_at, balance_id, order_id, errors
            )
            conn.execute(
                """
                insert or replace into reconciliation_runs(
                    run_id, account_key, observed_at, status, source,
                    balance_snapshot_id, order_snapshot_id, diff_count, error_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    account_key,
                    observed_at,
                    status,
                    source,
                    balance_id,
                    order_id,
                    len(diffs),
                    _json(errors),
                ),
            )
            for diff in diffs:
                conn.execute(
                    """
                    insert or replace into reconciliation_diffs(
                        run_id, category, item_key, expected, observed, delta,
                        severity, message
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        diff["category"],
                        diff["item_key"],
                        diff.get("expected"),
                        diff.get("observed"),
                        diff.get("delta"),
                        diff["severity"],
                        diff["message"],
                    ),
                )
            conn.commit()
        return {
            "enabled": True,
            "account_key": account_key,
            "run_id": run_id,
            "status": status,
            "diff_count": len(diffs),
            "error_count": len(errors),
            "observed_at": observed_at,
        }

    def latest_checkpoint(self, *, healthy_only: bool = False) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        if not Path(self.cfg.path).is_file() or Path(self.cfg.path).stat().st_size == 0:
            init_asset_ledger(self.cfg.path)
        where = "where status != 'error'" if healthy_only else ""
        with closing(_connect(self.cfg.path)) as conn:
            row = conn.execute(
                f"""
                select * from monitor_checkpoints {where}
                order by observed_at desc limit 1
                """  # noqa: S608 - where is a fixed internal fragment
            ).fetchone()
        if row is None:
            return None
        return {
            "checkpoint_id": row["checkpoint_id"],
            "observed_at": float(row["observed_at"]),
            "status": row["status"],
            "source": row["source"],
            "account_balances": json.loads(row["account_balances_json"]),
            "order_activity": json.loads(row["order_activity_json"]),
            "portfolio": json.loads(row["portfolio_json"]),
        }

    def update_worker_heartbeat(
        self,
        *,
        worker_id: str,
        account_key: str,
        status: str,
        pid: int | None = None,
        last_started_at: float | None = None,
        last_success_at: float | None = None,
        last_error_at: float | None = None,
        next_due_at: float | None = None,
        increment_cycle: bool = False,
        increment_error: bool = False,
        last_error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        now = time.time()
        with closing(_connect(self.cfg.path)) as conn:
            conn.execute(
                """
                insert into worker_heartbeats(
                    worker_id, account_key, pid, status, last_started_at,
                    last_success_at, last_error_at, next_due_at, cycle_count,
                    error_count, last_error, metadata_json, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(worker_id) do update set
                    account_key=excluded.account_key,
                    pid=coalesce(excluded.pid, worker_heartbeats.pid),
                    status=excluded.status,
                    last_started_at=coalesce(excluded.last_started_at, worker_heartbeats.last_started_at),
                    last_success_at=coalesce(excluded.last_success_at, worker_heartbeats.last_success_at),
                    last_error_at=coalesce(excluded.last_error_at, worker_heartbeats.last_error_at),
                    next_due_at=excluded.next_due_at,
                    cycle_count=worker_heartbeats.cycle_count + ?,
                    error_count=worker_heartbeats.error_count + ?,
                    last_error=excluded.last_error,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    worker_id,
                    account_key,
                    pid,
                    status,
                    last_started_at,
                    last_success_at,
                    last_error_at,
                    next_due_at,
                    1 if increment_cycle else 0,
                    1 if increment_error else 0,
                    last_error[:2000],
                    _json(metadata or {}),
                    now,
                    1 if increment_cycle else 0,
                    1 if increment_error else 0,
                ),
            )
            conn.commit()

    def summary(
        self,
        *,
        now: float | None = None,
        include_counts: bool = True,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "status": "disabled"}
        now = float(now or time.time())
        if not Path(self.cfg.path).is_file() or Path(self.cfg.path).stat().st_size == 0:
            init_asset_ledger(self.cfg.path)
        with closing(_connect(self.cfg.path)) as conn:
            counts = (
                {
                    table: int(
                        conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
                    )
                    for table in (
                        "ledger_events",
                        "balance_snapshots",
                        "order_snapshots",
                        "ledger_fills",
                        "position_snapshots",
                        "pnl_snapshots",
                        "reconciliation_runs",
                    )
                }
                if include_counts
                else {}
            )
            latest = conn.execute(
                "select observed_at, status, checkpoint_id from monitor_checkpoints order by observed_at desc limit 1"
            ).fetchone()
            workers = conn.execute(
                "select * from worker_heartbeats order by account_key, worker_id"
            ).fetchall()
            recent_runs = conn.execute(
                """
                select r.* from reconciliation_runs r
                join (
                    select account_key, max(observed_at) observed_at
                    from reconciliation_runs group by account_key
                ) x on x.account_key=r.account_key and x.observed_at=r.observed_at
                order by r.account_key
                """
            ).fetchall()
        latest_age = now - float(latest["observed_at"]) if latest else None
        worker_rows = []
        for row in workers:
            age = now - float(row["updated_at"])
            worker_rows.append(
                {
                    "worker_id": row["worker_id"],
                    "account_key": row["account_key"],
                    "status": row["status"],
                    "pid": row["pid"],
                    "cycle_count": int(row["cycle_count"]),
                    "error_count": int(row["error_count"]),
                    "last_success_at": row["last_success_at"],
                    "next_due_at": row["next_due_at"],
                    "last_error": row["last_error"],
                    "heartbeat_age_seconds": age,
                    "stale": age > self.cfg.worker_stale_seconds,
                    "metadata": json.loads(row["metadata_json"] or "{}"),
                }
            )
        reconciliation = [
            {
                "account_key": row["account_key"],
                "status": row["status"],
                "diff_count": int(row["diff_count"]),
                "observed_at": float(row["observed_at"]),
                "errors": json.loads(row["error_json"] or "[]"),
            }
            for row in recent_runs
        ]
        status = "ok"
        if latest is None or (latest_age is not None and latest_age > self.cfg.stale_seconds):
            status = "warning"
        if any(row["status"] == "error" for row in reconciliation):
            status = "error"
        elif any(row["status"] == "warning" for row in reconciliation) and status == "ok":
            status = "warning"
        return {
            "enabled": True,
            "status": status,
            "path": self.cfg.path,
            "schema_version": SCHEMA_VERSION,
            "counts": counts,
            "latest_checkpoint_id": latest["checkpoint_id"] if latest else None,
            "latest_checkpoint_at": float(latest["observed_at"]) if latest else None,
            "latest_checkpoint_age_seconds": latest_age,
            "workers": worker_rows,
            "reconciliation": reconciliation,
            "checked_at": now,
        }


def attach_ledger_checkpoint(
    cfg: AssetLedgerConfig,
    account_balances: dict[str, Any],
    order_activity: dict[str, Any],
    *,
    portfolio: dict[str, Any] | None = None,
    source: str = "web-monitor",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    store = AssetLedgerStore(cfg)
    if not store.enabled:
        return account_balances, order_activity, {"enabled": False, "status": "disabled"}
    previous = store.latest_checkpoint()
    private_revision = (
        account_balances.get("last_finished"),
        account_balances.get("status"),
        account_balances.get("errors"),
        order_activity.get("last_finished"),
        order_activity.get("status"),
        order_activity.get("errors"),
    )
    previous_revision = (
        (previous or {}).get("account_balances", {}).get("last_finished"),
        (previous or {}).get("account_balances", {}).get("status"),
        (previous or {}).get("account_balances", {}).get("errors"),
        (previous or {}).get("order_activity", {}).get("last_finished"),
        (previous or {}).get("order_activity", {}).get("status"),
        (previous or {}).get("order_activity", {}).get("errors"),
    )
    if previous is not None and private_revision == previous_revision:
        checkpoint = {
            "enabled": True,
            "checkpoint_id": previous.get("checkpoint_id"),
            "status": previous.get("status"),
            "observed_at": previous.get("observed_at"),
            "path": cfg.path,
            "unchanged": True,
        }
        canonical = previous
    else:
        checkpoint = store.record_monitor_checkpoint(
            account_balances,
            order_activity,
            portfolio=portfolio,
            source=source,
        )
        canonical = store.latest_checkpoint()
    fallback_used = False
    if checkpoint.get("status") == "error":
        healthy = store.latest_checkpoint(healthy_only=True)
        if healthy is not None and healthy.get("checkpoint_id") != checkpoint.get(
            "checkpoint_id"
        ):
            canonical = healthy
            fallback_used = True
    if canonical is None:
        return account_balances, order_activity, store.summary()
    ledger = store.summary()
    ledger["checkpoint"] = checkpoint
    ledger["fallback_used"] = fallback_used
    if fallback_used:
        ledger["status"] = "warning"
        ledger["fallback_checkpoint_id"] = canonical.get("checkpoint_id")
    canonical_balances = canonical["account_balances"]
    canonical_activity = canonical["order_activity"]
    if fallback_used:
        canonical_balances.update(
            {
                key: account_balances.get(key)
                for key in ("status", "errors", "warnings", "last_finished")
                if key in account_balances
            }
        )
        canonical_activity.update(
            {
                key: order_activity.get(key)
                for key in (
                    "status",
                    "errors",
                    "warnings",
                    "last_finished",
                    "reconciliation",
                )
                if key in order_activity
            }
        )
        canonical_balances["stale_snapshot"] = True
        canonical_activity["stale_snapshot"] = True
    canonical_balances["ledger"] = ledger
    canonical_activity["ledger"] = ledger
    return canonical_balances, canonical_activity, ledger


def account_keys_from_payloads(*payloads: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for payload in payloads:
        keys.update(_account_map(payload))
    return keys


def prune_asset_ledger(
    cfg: AssetLedgerConfig,
    *,
    before: float,
    tables: Iterable[str] = (
        "ledger_events",
        "monitor_checkpoints",
        "reconciliation_runs",
        "balance_snapshots",
        "order_snapshots",
        "position_snapshots",
        "pnl_snapshots",
        "ledger_fills",
    ),
) -> dict[str, int]:
    if not cfg.enabled:
        return {}
    allowed = {
        "ledger_events",
        "monitor_checkpoints",
        "reconciliation_runs",
        "balance_snapshots",
        "order_snapshots",
        "position_snapshots",
        "pnl_snapshots",
        "ledger_fills",
    }
    selected = set(tables) & allowed
    deleted: dict[str, int] = {}
    statements = {
        "ledger_events": "delete from ledger_events where observed_at < ?",
        "monitor_checkpoints": """
            delete from monitor_checkpoints
            where observed_at < ?
              and checkpoint_id not in (
                  select checkpoint_id from monitor_checkpoints
                  order by observed_at desc limit 1
              )
              and checkpoint_id not in (
                  select checkpoint_id from monitor_checkpoints
                  where status != 'error'
                  order by observed_at desc limit 1
              )
        """,
        "reconciliation_runs": """
            delete from reconciliation_runs
            where observed_at < ?
              and run_id not in (
                  select run_id from reconciliation_runs latest
                  where latest.account_key = reconciliation_runs.account_key
                    and latest.source = reconciliation_runs.source
                  order by latest.observed_at desc limit 1
              )
        """,
        "balance_snapshots": """
            delete from balance_snapshots
            where observed_at < ?
              and snapshot_id not in (
                  select snapshot_id from balance_snapshots latest
                  where latest.account_key = balance_snapshots.account_key
                    and latest.source = balance_snapshots.source
                  order by latest.observed_at desc limit 1
              )
        """,
        "order_snapshots": """
            delete from order_snapshots
            where observed_at < ?
              and snapshot_id not in (
                  select snapshot_id from order_snapshots latest
                  where latest.account_key = order_snapshots.account_key
                    and latest.source = order_snapshots.source
                  order by latest.observed_at desc limit 1
              )
        """,
        "position_snapshots": """
            delete from position_snapshots
            where observed_at < ?
              and snapshot_id not in (
                  select snapshot_id from position_snapshots latest
                  where latest.source = position_snapshots.source
                  order by latest.observed_at desc limit 1
              )
        """,
        "pnl_snapshots": """
            delete from pnl_snapshots
            where observed_at < ?
              and snapshot_id not in (
                  select snapshot_id from pnl_snapshots latest
                  where latest.currency = pnl_snapshots.currency
                  order by latest.observed_at desc limit 1
              )
        """,
        "ledger_fills": "delete from ledger_fills where last_observed_at < ?",
    }
    delete_order = (
        "reconciliation_runs",
        "balance_snapshots",
        "order_snapshots",
        "position_snapshots",
        "pnl_snapshots",
        "monitor_checkpoints",
        "ledger_events",
        "ledger_fills",
    )
    with closing(_connect(cfg.path)) as conn:
        for table in delete_order:
            if table not in selected:
                continue
            cursor = conn.execute(statements[table], (float(before),))
            deleted[table] = max(0, int(cursor.rowcount))
            conn.commit()
        conn.execute("pragma optimize")
        conn.execute("pragma wal_checkpoint(passive)")
    return deleted
