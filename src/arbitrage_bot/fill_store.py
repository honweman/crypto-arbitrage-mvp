from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import PnlStoreConfig


PNL_SOURCES = (
    "market_maker",
    "arbitrage",
    "auto_buy_sell",
    "manual",
    "unattributed",
)


def _connect(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_fill_store(path: str) -> None:
    with closing(_connect(path)) as conn:
        conn.executescript(
            """
            create table if not exists fills (
                fill_key text primary key,
                exchange text not null,
                symbol text not null,
                trade_id text,
                order_id text,
                side text,
                source text not null,
                base_currency text,
                quote_currency text,
                price real,
                amount real,
                cost real,
                notional_common real,
                fee_cost real,
                fee_currency text,
                fee_common real,
                realized_pnl_common real,
                timestamp_ms real,
                day text not null,
                observed_at real not null,
                raw_json text not null
            );
            create index if not exists idx_fills_day on fills(day);
            create index if not exists idx_fills_order on fills(exchange, symbol, order_id);
            create table if not exists daily_pnl_snapshots (
                day text primary key,
                currency text not null,
                total_realized_pnl real not null,
                market_maker_pnl real not null,
                arbitrage_pnl real not null,
                auto_buy_sell_pnl real not null,
                manual_pnl real not null,
                unattributed_pnl real not null,
                total_fees real not null,
                total_notional real not null,
                trade_count integer not null,
                updated_at real not null
            );
            """
        )
        conn.commit()


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day_for_trade(trade: dict[str, Any], observed_at: float) -> str:
    timestamp_ms = _number_or_none(trade.get("timestamp"))
    timestamp = (
        timestamp_ms / 1000
        if timestamp_ms and timestamp_ms > 10_000_000_000
        else timestamp_ms
    )
    if timestamp is None or timestamp <= 0:
        timestamp = observed_at
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def _fill_key(trade: dict[str, Any]) -> str:
    identity = {
        "exchange": trade.get("exchange"),
        "symbol": trade.get("symbol"),
        "id": trade.get("id"),
        "order_id": trade.get("order_id"),
        "side": trade.get("side"),
        "price": trade.get("price"),
        "amount": trade.get("amount"),
        "timestamp": trade.get("timestamp"),
    }
    payload = json.dumps(identity, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _source_row(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "trade_count": 0,
        "notional_common": 0.0,
        "fees_common": 0.0,
        "realized_pnl": 0.0,
    }


def _empty_daily_summary(day: str, currency: str) -> dict[str, Any]:
    sources = {source: _source_row(source) for source in PNL_SOURCES}
    return {
        "day": day,
        "currency": currency,
        "trade_count": 0,
        "total_realized_pnl": 0.0,
        "total_fees": 0.0,
        "total_notional": 0.0,
        "sources": sources,
        "updated_at": None,
    }


def _daily_summary_from_rows(
    rows: list[sqlite3.Row],
    *,
    day: str,
    currency: str,
    updated_at: float | None = None,
) -> dict[str, Any]:
    summary = _empty_daily_summary(day, currency)
    for row in rows:
        source = str(row["source"] or "unattributed")
        source_row = summary["sources"].setdefault(source, _source_row(source))
        trade_count = int(row["trade_count"] or 0)
        realized = float(row["realized_pnl"] or 0.0)
        fees = float(row["fees"] or 0.0)
        notional = float(row["notional"] or 0.0)
        source_row["trade_count"] += trade_count
        source_row["realized_pnl"] += realized
        source_row["fees_common"] += fees
        source_row["notional_common"] += notional
        summary["trade_count"] += trade_count
        summary["total_realized_pnl"] += realized
        summary["total_fees"] += fees
        summary["total_notional"] += notional
    summary["sources"] = {
        source: row
        for source, row in summary["sources"].items()
        if row["trade_count"] > 0 or abs(row["realized_pnl"]) >= 1e-12
    }
    summary["updated_at"] = updated_at
    return summary


def load_daily_pnl_summary(
    cfg: PnlStoreConfig,
    *,
    currency: str = "USD",
    day: str | None = None,
) -> dict[str, Any]:
    day = day or time.strftime("%Y-%m-%d", time.localtime())
    if not cfg.enabled:
        return {
            "enabled": False,
            **_empty_daily_summary(day, currency),
        }
    init_fill_store(cfg.path)
    with closing(_connect(cfg.path)) as conn:
        rows = conn.execute(
            """
            select
                source,
                count(*) as trade_count,
                coalesce(sum(realized_pnl_common), 0) as realized_pnl,
                coalesce(sum(fee_common), 0) as fees,
                coalesce(sum(notional_common), 0) as notional
            from fills
            where day = ?
            group by source
            """,
            (day,),
        ).fetchall()
        snapshot = conn.execute(
            "select updated_at from daily_pnl_snapshots where day = ?",
            (day,),
        ).fetchone()
    summary = _daily_summary_from_rows(
        rows,
        day=day,
        currency=currency,
        updated_at=float(snapshot["updated_at"]) if snapshot else None,
    )
    summary["enabled"] = True
    summary["path"] = cfg.path
    return summary


def _write_daily_snapshot(
    conn: sqlite3.Connection,
    summary: dict[str, Any],
) -> None:
    sources = summary.get("sources") or {}
    conn.execute(
        """
        insert into daily_pnl_snapshots (
            day,
            currency,
            total_realized_pnl,
            market_maker_pnl,
            arbitrage_pnl,
            auto_buy_sell_pnl,
            manual_pnl,
            unattributed_pnl,
            total_fees,
            total_notional,
            trade_count,
            updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(day) do update set
            currency = excluded.currency,
            total_realized_pnl = excluded.total_realized_pnl,
            market_maker_pnl = excluded.market_maker_pnl,
            arbitrage_pnl = excluded.arbitrage_pnl,
            auto_buy_sell_pnl = excluded.auto_buy_sell_pnl,
            manual_pnl = excluded.manual_pnl,
            unattributed_pnl = excluded.unattributed_pnl,
            total_fees = excluded.total_fees,
            total_notional = excluded.total_notional,
            trade_count = excluded.trade_count,
            updated_at = excluded.updated_at
        """,
        (
            summary["day"],
            summary["currency"],
            summary["total_realized_pnl"],
            sources.get("market_maker", {}).get("realized_pnl", 0.0),
            sources.get("arbitrage", {}).get("realized_pnl", 0.0),
            sources.get("auto_buy_sell", {}).get("realized_pnl", 0.0),
            sources.get("manual", {}).get("realized_pnl", 0.0),
            sources.get("unattributed", {}).get("realized_pnl", 0.0),
            summary["total_fees"],
            summary["total_notional"],
            summary["trade_count"],
            summary["updated_at"],
        ),
    )


def persist_fill_pnl(
    cfg: PnlStoreConfig,
    trades: list[dict[str, Any]],
    *,
    currency: str = "USD",
) -> dict[str, Any]:
    day = time.strftime("%Y-%m-%d", time.localtime())
    if not cfg.enabled:
        return {
            "enabled": False,
            "path": cfg.path,
            "stored_fill_count": 0,
            "daily": _empty_daily_summary(day, currency),
        }

    init_fill_store(cfg.path)
    observed_at = time.time()
    with closing(_connect(cfg.path)) as conn:
        for trade in trades:
            fee = trade.get("fee") if isinstance(trade.get("fee"), dict) else {}
            conn.execute(
                """
                insert into fills (
                    fill_key,
                    exchange,
                    symbol,
                    trade_id,
                    order_id,
                    side,
                    source,
                    base_currency,
                    quote_currency,
                    price,
                    amount,
                    cost,
                    notional_common,
                    fee_cost,
                    fee_currency,
                    fee_common,
                    realized_pnl_common,
                    timestamp_ms,
                    day,
                    observed_at,
                    raw_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(fill_key) do update set
                    source = excluded.source,
                    notional_common = excluded.notional_common,
                    fee_common = excluded.fee_common,
                    realized_pnl_common = excluded.realized_pnl_common,
                    observed_at = excluded.observed_at,
                    raw_json = excluded.raw_json
                """,
                (
                    _fill_key(trade),
                    str(trade.get("exchange") or ""),
                    str(trade.get("symbol") or ""),
                    str(trade.get("id") or ""),
                    str(trade.get("order_id") or ""),
                    str(trade.get("side") or ""),
                    str(trade.get("source") or "unattributed"),
                    str(trade.get("base_currency") or ""),
                    str(trade.get("quote_currency") or ""),
                    _number_or_none(trade.get("price")),
                    _number_or_none(trade.get("amount")),
                    _number_or_none(trade.get("cost")),
                    _number_or_none(trade.get("notional_common")),
                    _number_or_none(fee.get("cost")),
                    str(fee.get("currency") or ""),
                    _number_or_none(trade.get("fee_common")),
                    _number_or_none(trade.get("realized_pnl_common")),
                    _number_or_none(trade.get("timestamp")),
                    _day_for_trade(trade, observed_at),
                    observed_at,
                    json.dumps(trade, ensure_ascii=True, sort_keys=True),
                ),
            )
        conn.commit()
        rows = conn.execute(
            """
            select
                source,
                count(*) as trade_count,
                coalesce(sum(realized_pnl_common), 0) as realized_pnl,
                coalesce(sum(fee_common), 0) as fees,
                coalesce(sum(notional_common), 0) as notional
            from fills
            where day = ?
            group by source
            """,
            (day,),
        ).fetchall()
        stored_fill_count = int(
            conn.execute("select count(*) as count from fills").fetchone()["count"]
        )
        summary = _daily_summary_from_rows(
            rows,
            day=day,
            currency=currency,
            updated_at=observed_at,
        )
        _write_daily_snapshot(conn, summary)
        conn.commit()

    summary["enabled"] = True
    summary["path"] = cfg.path
    return {
        "enabled": True,
        "path": cfg.path,
        "stored_fill_count": stored_fill_count,
        "daily": summary,
    }


def load_fill_rows(
    cfg: PnlStoreConfig,
    *,
    day: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    if not cfg.enabled:
        return []
    target_day = day or time.strftime("%Y-%m-%d", time.localtime())
    init_fill_store(cfg.path)
    with closing(_connect(cfg.path)) as conn:
        rows = conn.execute(
            """
            select * from fills
            where day = ?
            order by timestamp_ms desc, observed_at desc
            limit ?
            """,
            (target_day, max(1, min(int(limit), 50_000))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.update(
            {
                "exchange": str(row["exchange"] or ""),
                "symbol": str(row["symbol"] or ""),
                "id": str(row["trade_id"] or ""),
                "order_id": str(row["order_id"] or ""),
                "side": str(row["side"] or ""),
                "source": str(row["source"] or "unattributed"),
                "base_currency": str(row["base_currency"] or ""),
                "quote_currency": str(row["quote_currency"] or ""),
                "price": _number_or_none(row["price"]),
                "amount": _number_or_none(row["amount"]),
                "cost": _number_or_none(row["cost"]),
                "notional_common": _number_or_none(row["notional_common"]),
                "fee_common": _number_or_none(row["fee_common"]),
                "realized_pnl_common": _number_or_none(row["realized_pnl_common"]),
                "timestamp": _number_or_none(row["timestamp_ms"]),
                "observed_at": _number_or_none(row["observed_at"]),
            }
        )
        result.append(payload)
    return result
