from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable


PERFORMANCE_SCHEMA_SQL = """
create table if not exists cash_flows (
    flow_key text primary key,
    account_key text not null,
    transaction_id text not null default '',
    txid text not null default '',
    flow_type text not null,
    currency text not null,
    amount real not null,
    fee_cost real,
    fee_currency text not null default '',
    timestamp_ms real,
    status text not null default '',
    source text not null,
    first_observed_at real not null,
    last_observed_at real not null,
    payload_json text not null
);
create index if not exists idx_cash_flows_account_time
    on cash_flows(account_key, timestamp_ms desc);
create index if not exists idx_cash_flows_observed
    on cash_flows(first_observed_at desc);

create table if not exists cash_flow_sync_state (
    account_key text primary key,
    supported integer not null default 0,
    supported_types_json text not null default '[]',
    status text not null,
    cursor_ms real,
    last_started_at real,
    last_success_at real,
    last_error text not null default '',
    updated_at real not null
);

create table if not exists cash_flow_account_aliases (
    account_key text primary key,
    canonical_account_key text not null,
    updated_at real not null
);

create table if not exists portfolio_performance_state (
    scope_key text primary key,
    currency text not null,
    inception_at real not null,
    opening_equity real not null,
    cumulative_external_flow real not null,
    daily_day text not null,
    daily_started_at real not null,
    daily_opening_equity real not null,
    daily_external_flow real not null,
    latest_equity real not null,
    latest_at real not null,
    account_keys_json text not null,
    account_values_json text not null,
    payload_json text not null
);

create table if not exists portfolio_performance_flows (
    scope_key text not null,
    flow_key text not null references cash_flows(flow_key) on delete cascade,
    value_common real not null,
    applied_at real not null,
    primary key(scope_key, flow_key)
);

create table if not exists portfolio_performance_observations (
    observation_key text primary key,
    scope_key text not null,
    observed_at real not null,
    currency text not null,
    equity real not null,
    external_flow real not null,
    since_inception_pnl real,
    daily_pnl real,
    payload_json text not null
);
create index if not exists idx_performance_observations_scope_time
    on portfolio_performance_observations(scope_key, observed_at desc);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _local_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().date().isoformat()


def init_performance_schema(conn: Any) -> None:
    conn.executescript(PERFORMANCE_SCHEMA_SQL)


def cash_flow_sync_cursor(
    conn: Any,
    *,
    account_key: str,
    supported_types: Iterable[str],
    observed_at: float,
    overlap_ms: float = 86_400_000.0,
) -> float | None:
    supported = sorted({str(item) for item in supported_types if str(item)})
    row = conn.execute(
        "select cursor_ms from cash_flow_sync_state where account_key = ?",
        (account_key,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            insert into cash_flow_sync_state(
                account_key, supported, supported_types_json, status, cursor_ms,
                last_started_at, last_success_at, last_error, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, '', ?)
            """,
            (
                account_key,
                int(bool(supported)),
                _json(supported),
                "initialized" if supported else "unsupported",
                observed_at * 1000.0,
                observed_at,
                observed_at if not supported else None,
                observed_at,
            ),
        )
        return None
    cursor = _number(row["cursor_ms"])
    conn.execute(
        """
        update cash_flow_sync_state
        set supported = ?, supported_types_json = ?, status = ?,
            last_started_at = ?, updated_at = ?
        where account_key = ?
        """,
        (
            int(bool(supported)),
            _json(supported),
            "running" if supported else "unsupported",
            observed_at,
            observed_at,
            account_key,
        ),
    )
    if not supported:
        return None
    return max(0.0, float(cursor or observed_at * 1000.0) - overlap_ms)


def record_cash_flow_alias(
    conn: Any,
    *,
    account_key: str,
    canonical_account_key: str,
    observed_at: float,
) -> None:
    conn.execute(
        """
        insert into cash_flow_account_aliases(
            account_key, canonical_account_key, updated_at
        ) values (?, ?, ?)
        on conflict(account_key) do update set
            canonical_account_key = excluded.canonical_account_key,
            updated_at = excluded.updated_at
        """,
        (account_key, canonical_account_key, observed_at),
    )


def _cash_flow_account_map(conn: Any, account_keys: set[str]) -> dict[str, str]:
    aliases = {key: key for key in account_keys}
    if not account_keys:
        return aliases
    placeholders = ",".join("?" for _ in account_keys)
    rows = conn.execute(
        f"""
        select account_key, canonical_account_key
        from cash_flow_account_aliases
        where account_key in ({placeholders})
        """,  # noqa: S608
        tuple(sorted(account_keys)),
    ).fetchall()
    for row in rows:
        aliases[str(row["account_key"])] = str(row["canonical_account_key"])
    return aliases


def record_cash_flow_batch(
    conn: Any,
    *,
    account_key: str,
    transactions: Iterable[dict[str, Any]],
    supported_types: Iterable[str],
    observed_at: float,
    errors: Iterable[str] = (),
) -> int:
    supported = sorted({str(item) for item in supported_types if str(item)})
    error_rows = [str(item) for item in errors if str(item)]
    inserted = 0
    latest_timestamp_ms = observed_at * 1000.0
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        flow_type = str(transaction.get("type") or "").lower()
        if flow_type not in {"deposit", "withdrawal"}:
            continue
        currency = str(transaction.get("currency") or "").upper()
        amount = _number(transaction.get("amount"))
        if not currency or amount is None or amount < 0:
            continue
        transaction_id = str(transaction.get("id") or "")
        txid = str(transaction.get("txid") or "")
        timestamp_ms = _number(transaction.get("timestamp"))
        latest_timestamp_ms = max(latest_timestamp_ms, timestamp_ms or 0.0)
        fee = transaction.get("fee") if isinstance(transaction.get("fee"), dict) else {}
        identity = transaction_id or txid or _json(
            [flow_type, currency, amount, timestamp_ms, transaction.get("address")]
        )
        import hashlib

        flow_key = hashlib.sha256(
            _json([account_key, flow_type, identity]).encode("utf-8")
        ).hexdigest()[:40]
        existed = conn.execute(
            "select 1 from cash_flows where flow_key = ?",
            (flow_key,),
        ).fetchone()
        conn.execute(
            """
            insert into cash_flows(
                flow_key, account_key, transaction_id, txid, flow_type,
                currency, amount, fee_cost, fee_currency, timestamp_ms, status,
                source, first_observed_at, last_observed_at, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(flow_key) do update set
                status = excluded.status,
                last_observed_at = max(cash_flows.last_observed_at, excluded.last_observed_at),
                payload_json = excluded.payload_json
            """,
            (
                flow_key,
                account_key,
                transaction_id,
                txid,
                flow_type,
                currency,
                amount,
                _number(fee.get("cost")),
                str(fee.get("currency") or "").upper(),
                timestamp_ms,
                str(transaction.get("status") or ""),
                str(transaction.get("source") or "account-worker"),
                observed_at,
                observed_at,
                _json(transaction),
            ),
        )
        inserted += int(existed is None)
    conn.execute(
        """
        insert into cash_flow_sync_state(
            account_key, supported, supported_types_json, status, cursor_ms,
            last_started_at, last_success_at, last_error, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(account_key) do update set
            supported = excluded.supported,
            supported_types_json = excluded.supported_types_json,
            status = excluded.status,
            cursor_ms = max(
                coalesce(cash_flow_sync_state.cursor_ms, 0),
                excluded.cursor_ms
            ),
            last_success_at = excluded.last_success_at,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (
            account_key,
            int(bool(supported)),
            _json(supported),
            "error" if error_rows and not supported else "partial" if error_rows else "ok",
            latest_timestamp_ms,
            observed_at,
            observed_at if supported and not error_rows else None,
            "; ".join(error_rows),
            observed_at,
        ),
    )
    return inserted


def record_cash_flow_sync_error(
    conn: Any,
    *,
    account_key: str,
    supported_types: Iterable[str],
    observed_at: float,
    error: str,
) -> None:
    supported = sorted({str(item) for item in supported_types if str(item)})
    conn.execute(
        """
        insert into cash_flow_sync_state(
            account_key, supported, supported_types_json, status, cursor_ms,
            last_started_at, last_success_at, last_error, updated_at
        ) values (?, ?, ?, 'error', ?, ?, null, ?, ?)
        on conflict(account_key) do update set
            supported = excluded.supported,
            supported_types_json = excluded.supported_types_json,
            status = 'error',
            last_started_at = excluded.last_started_at,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (
            account_key,
            int(bool(supported)),
            _json(supported),
            observed_at * 1000.0,
            observed_at,
            str(error),
            observed_at,
        ),
    )


def portfolio_currency_rates(portfolio: dict[str, Any]) -> dict[str, float]:
    rates: dict[str, float] = {}
    currency = str(
        portfolio.get("total_asset_currency")
        or portfolio.get("quote_currency")
        or "USD"
    ).upper()
    rates[currency] = 1.0
    for item in portfolio.get("positions", []) or []:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset") or "").upper()
        mark = _number(item.get("mark_price"))
        if asset and mark is not None and mark > 0:
            rates[asset] = mark
    cash = portfolio.get("cash_balances") or {}
    cash_common = portfolio.get("cash_balances_common") or {}
    for asset, amount_raw in cash.items():
        amount = _number(amount_raw)
        value = _number(cash_common.get(asset))
        if amount not in {None, 0.0} and value is not None:
            rates[str(asset).upper()] = value / amount
    return rates


def account_values_from_balances(
    account_balances: dict[str, Any],
    portfolio: dict[str, Any],
) -> dict[str, float]:
    rates = portfolio_currency_rates(portfolio)
    result: dict[str, float] = {}
    for account in account_balances.get("accounts", []) or []:
        if not isinstance(account, dict):
            continue
        account_key = str(account.get("exchange") or account.get("account_key") or "")
        balance = account.get("balance") if isinstance(account.get("balance"), dict) else {}
        if not account_key or not balance.get("checked") or account.get("errors"):
            continue
        value = 0.0
        complete = True
        has_value = False
        for row in balance.get("currencies", []) or []:
            if not isinstance(row, dict):
                continue
            amount = _number(row.get("total"))
            currency = str(row.get("currency") or "").upper()
            if amount in {None, 0.0} or not currency:
                continue
            has_value = True
            rate = rates.get(currency)
            if rate is None:
                complete = False
                break
            value += float(amount) * rate
        if complete and has_value:
            result[account_key] = value
    return result


def _cash_flow_coverage(conn: Any, account_keys: set[str]) -> dict[str, Any]:
    if not account_keys:
        return {
            "status": "not_applicable",
            "account_count": 0,
            "covered_account_count": 0,
            "unsupported_accounts": [],
            "error_accounts": [],
        }
    aliases = _cash_flow_account_map(conn, account_keys)
    effective_keys = set(aliases.values())
    placeholders = ",".join("?" for _ in effective_keys)
    rows = conn.execute(
        f"select * from cash_flow_sync_state where account_key in ({placeholders})",  # noqa: S608
        tuple(sorted(effective_keys)),
    ).fetchall()
    by_account = {str(row["account_key"]): row for row in rows}
    unsupported = sorted(
        key
        for key in account_keys
        if aliases[key] not in by_account
        or not bool(by_account[aliases[key]]["supported"])
    )
    errors = sorted(
        key for key in account_keys
        if aliases[key] in by_account
        and str(by_account[aliases[key]]["status"]) in {"error", "partial"}
    )
    pending = sorted(
        key for key in account_keys
        if aliases[key] in by_account
        and bool(by_account[aliases[key]]["supported"])
        and str(by_account[aliases[key]]["status"])
        not in {"ok", "error", "partial"}
    )
    covered = len(account_keys) - len(unsupported) - len(errors) - len(pending)
    return {
        "status": "complete" if covered == len(account_keys) else "partial",
        "account_count": len(account_keys),
        "covered_account_count": max(0, covered),
        "unsupported_accounts": unsupported,
        "error_accounts": errors,
        "pending_accounts": pending,
    }


def _completed_cash_flow(status: Any) -> bool:
    normalized = str(status or "").strip().upper()
    return normalized in {
        "OK",
        "DONE",
        "ACCEPTED",
        "SUCCESS",
        "SUCCEEDED",
        "COMPLETE",
        "COMPLETED",
        "DEPOSIT_ACCEPTED",
    }


def _empty_performance(
    portfolio: dict[str, Any],
    *,
    reason: str,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    currency = str(
        portfolio.get("total_asset_currency")
        or portfolio.get("quote_currency")
        or "USD"
    ).upper()
    return {
        "status": "unavailable",
        "reason": reason,
        "currency": currency,
        "current_equity": _number(portfolio.get("total_asset_value")),
        "since_inception": {"pnl": None},
        "rolling_24h": {"pnl": None},
        "daily": {"pnl": None, "day": _local_day(datetime.now().timestamp())},
        "cash_flow_coverage": coverage,
    }


def _rolling_24h_performance(
    conn: Any,
    *,
    scope_key: str,
    observed_at: float,
    current_equity: float,
    inception_at: float,
    opening_equity: float,
    cumulative_flow: float,
    current_external_flow: float,
) -> dict[str, Any]:
    target_at = observed_at - 24.0 * 60.0 * 60.0
    if inception_at > target_at:
        return {
            "pnl": current_equity - opening_equity - cumulative_flow,
            "started_at": inception_at,
            "opening_equity": opening_equity,
            "net_external_flow": cumulative_flow,
            "window_seconds": max(0.0, observed_at - inception_at),
            "complete_window": False,
        }

    reference = conn.execute(
        """
        select observed_at, equity
        from portfolio_performance_observations
        where scope_key = ? and observed_at <= ?
        order by observed_at desc
        limit 1
        """,
        (scope_key, target_at),
    ).fetchone()
    if reference is None:
        reference = conn.execute(
            """
            select observed_at, equity
            from portfolio_performance_observations
            where scope_key = ?
            order by observed_at asc
            limit 1
            """,
            (scope_key,),
        ).fetchone()
    if reference is None:
        return {
            "pnl": current_equity - opening_equity - cumulative_flow,
            "started_at": inception_at,
            "opening_equity": opening_equity,
            "net_external_flow": cumulative_flow,
            "window_seconds": max(0.0, observed_at - inception_at),
            "complete_window": False,
        }

    started_at = float(reference["observed_at"])
    window_flow = float(
        conn.execute(
            """
            select coalesce(sum(external_flow), 0)
            from portfolio_performance_observations
            where scope_key = ? and observed_at > ? and observed_at <= ?
            """,
            (scope_key, started_at, observed_at),
        ).fetchone()[0]
        or 0.0
    )
    window_flow += current_external_flow
    reference_equity = float(reference["equity"])
    return {
        "pnl": current_equity - reference_equity - window_flow,
        "started_at": started_at,
        "opening_equity": reference_equity,
        "net_external_flow": window_flow,
        "window_seconds": max(0.0, observed_at - started_at),
        "complete_window": started_at <= target_at,
    }


def update_portfolio_performance(
    conn: Any,
    *,
    portfolio: dict[str, Any],
    scope_key: str,
    account_keys: Iterable[str],
    account_values: dict[str, float] | None,
    observed_at: float,
    observation_key: str,
) -> dict[str, Any]:
    keys = {str(item) for item in account_keys if str(item)}
    coverage = _cash_flow_coverage(conn, keys)
    state = conn.execute(
        "select * from portfolio_performance_state where scope_key = ?",
        (scope_key,),
    ).fetchone()

    def unavailable(reason: str, **extra: Any) -> dict[str, Any]:
        if state is not None:
            try:
                previous = json.loads(state["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                previous = {}
            if isinstance(previous, dict) and previous.get("since_inception"):
                previous.update(
                    {
                        "status": "stale",
                        "reason": reason,
                        "stale_since": observed_at,
                        "last_reliable_at": float(state["latest_at"]),
                        "cash_flow_coverage": coverage,
                    }
                )
                previous.update(extra)
                return previous
        payload = _empty_performance(portfolio, reason=reason, coverage=coverage)
        payload.update(extra)
        return payload

    missing = sorted(
        {
            str(item).upper()
            for item in [
                *(portfolio.get("position_missing_marks") or []),
                *(portfolio.get("cash_missing_rates") or []),
                *(portfolio.get("total_asset_missing_rates") or []),
            ]
            if item
        }
    )
    equity = _number(portfolio.get("total_asset_value"))
    if equity is None or missing:
        reason = "missing asset prices" if missing else "total asset value unavailable"
        return unavailable(reason, missing_rates=missing)

    currency = str(
        portfolio.get("total_asset_currency")
        or portfolio.get("quote_currency")
        or "USD"
    ).upper()
    current_day = _local_day(observed_at)
    rates = portfolio_currency_rates(portfolio)
    normalized_values = {
        str(key): float(value)
        for key, value in (account_values or {}).items()
        if _number(value) is not None
    }
    missing_account_values = sorted(keys - set(normalized_values))
    if missing_account_values:
        return unavailable(
            reason="account valuations are incomplete",
            missing_account_values=missing_account_values,
        )
    if state is None:
        payload = {
            "status": "ok" if coverage["status"] == "complete" else "warning",
            "reason": "" if coverage["status"] == "complete" else "cash-flow coverage is partial",
            "currency": currency,
            "current_equity": equity,
            "since_inception": {
                "pnl": 0.0,
                "started_at": observed_at,
                "opening_equity": equity,
                "net_external_flow": 0.0,
            },
            "rolling_24h": {
                "pnl": 0.0,
                "started_at": observed_at,
                "opening_equity": equity,
                "net_external_flow": 0.0,
                "window_seconds": 0.0,
                "complete_window": False,
            },
            "daily": {
                "pnl": 0.0,
                "day": current_day,
                "started_at": observed_at,
                "opening_equity": equity,
                "net_external_flow": 0.0,
            },
            "cash_flow_coverage": coverage,
        }
        conn.execute(
            """
            insert into portfolio_performance_state(
                scope_key, currency, inception_at, opening_equity,
                cumulative_external_flow, daily_day, daily_started_at,
                daily_opening_equity, daily_external_flow, latest_equity,
                latest_at, account_keys_json, account_values_json, payload_json
            ) values (?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                scope_key,
                currency,
                observed_at,
                equity,
                current_day,
                observed_at,
                equity,
                equity,
                observed_at,
                _json(sorted(keys)),
                _json(normalized_values),
                _json(payload),
            ),
        )
        return payload

    inception_at = float(state["inception_at"])
    opening_equity = float(state["opening_equity"])
    cumulative_flow = float(state["cumulative_external_flow"])
    daily_day = str(state["daily_day"])
    daily_started_at = float(state["daily_started_at"])
    daily_opening = float(state["daily_opening_equity"])
    daily_flow = float(state["daily_external_flow"])
    latest_equity = float(state["latest_equity"])
    previous_keys = set(json.loads(state["account_keys_json"] or "[]"))
    previous_values = {
        str(key): float(value)
        for key, value in json.loads(state["account_values_json"] or "{}").items()
    }

    day_rolled = daily_day != current_day
    if day_rolled:
        daily_day = current_day
        daily_started_at = observed_at
        daily_opening = latest_equity
        daily_flow = 0.0

    membership_flow = sum(normalized_values.get(key, 0.0) for key in keys - previous_keys)
    membership_flow -= sum(previous_values.get(key, 0.0) for key in previous_keys - keys)

    pending_rates: set[str] = set()
    applied_flow = 0.0
    flow_count = 0
    candidate_keys = sorted(
        set(_cash_flow_account_map(conn, keys | previous_keys).values())
    )
    if candidate_keys:
        placeholders = ",".join("?" for _ in candidate_keys)
        flows = conn.execute(
            f"""
            select f.* from cash_flows f
            where f.account_key in ({placeholders})
              and coalesce(f.timestamp_ms, f.first_observed_at * 1000) >= ?
              and not exists (
                  select 1 from portfolio_performance_flows pf
                  where pf.scope_key = ? and pf.flow_key = f.flow_key
              )
            order by coalesce(f.timestamp_ms, f.first_observed_at * 1000), f.flow_key
            """,  # noqa: S608
            (*candidate_keys, inception_at * 1000.0, scope_key),
        ).fetchall()
        for flow in flows:
            if not _completed_cash_flow(flow["status"]):
                continue
            flow_currency = str(flow["currency"] or "").upper()
            rate = rates.get(flow_currency)
            if rate is None:
                pending_rates.add(flow_currency)
                continue
            direction = 1.0 if str(flow["flow_type"]) == "deposit" else -1.0
            value_common = direction * float(flow["amount"] or 0.0) * rate
            conn.execute(
                """
                insert or ignore into portfolio_performance_flows(
                    scope_key, flow_key, value_common, applied_at
                ) values (?, ?, ?, ?)
                """,
                (scope_key, flow["flow_key"], value_common, observed_at),
            )
            applied_flow += value_common
            flow_count += 1

    cumulative_flow += membership_flow + applied_flow
    daily_flow += membership_flow + applied_flow
    since_pnl = equity - opening_equity - cumulative_flow
    today_pnl = equity - daily_opening - daily_flow
    rolling_24h = _rolling_24h_performance(
        conn,
        scope_key=scope_key,
        observed_at=observed_at,
        current_equity=equity,
        inception_at=inception_at,
        opening_equity=opening_equity,
        cumulative_flow=cumulative_flow,
        current_external_flow=membership_flow + applied_flow,
    )
    status = "ok"
    reasons: list[str] = []
    if coverage["status"] != "complete":
        status = "warning"
        reasons.append("cash-flow coverage is partial")
    if pending_rates:
        status = "warning"
        reasons.append("cash flows are waiting for prices")
        since_pnl = None
        today_pnl = None
        rolling_24h["pnl"] = None
    payload = {
        "status": status,
        "reason": "; ".join(reasons),
        "currency": currency,
        "current_equity": equity,
        "since_inception": {
            "pnl": since_pnl,
            "started_at": inception_at,
            "opening_equity": opening_equity,
            "net_external_flow": cumulative_flow,
        },
        "rolling_24h": rolling_24h,
        "daily": {
            "pnl": today_pnl,
            "day": daily_day,
            "started_at": daily_started_at,
            "opening_equity": daily_opening,
            "net_external_flow": daily_flow,
        },
        "cash_flow_coverage": coverage,
        "applied_cash_flow_count": flow_count,
        "applied_cash_flow_value": applied_flow,
        "account_membership_flow": membership_flow,
        "pending_cash_flow_currencies": sorted(pending_rates),
    }
    should_persist = bool(
        day_rolled
        or flow_count
        or abs(membership_flow) > 1e-12
        or observed_at - float(state["latest_at"]) >= 30.0
    )
    if should_persist:
        conn.execute(
            """
            update portfolio_performance_state
            set currency = ?, cumulative_external_flow = ?, daily_day = ?,
                daily_started_at = ?, daily_opening_equity = ?,
                daily_external_flow = ?, latest_equity = ?, latest_at = ?,
                account_keys_json = ?, account_values_json = ?, payload_json = ?
            where scope_key = ?
            """,
            (
                currency,
                cumulative_flow,
                daily_day,
                daily_started_at,
                daily_opening,
                daily_flow,
                equity,
                observed_at,
                _json(sorted(keys)),
                _json(normalized_values),
                _json(payload),
                scope_key,
            ),
        )
        conn.execute(
            """
            insert or replace into portfolio_performance_observations(
                observation_key, scope_key, observed_at, currency, equity,
                external_flow, since_inception_pnl, daily_pnl, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_key,
                scope_key,
                observed_at,
                currency,
                equity,
                membership_flow + applied_flow,
                since_pnl,
                today_pnl,
                _json(payload),
            ),
        )
    return payload
