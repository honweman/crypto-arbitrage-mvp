from __future__ import annotations

import time
from typing import Any

from .config import ExchangeConfig, RiskConfig
from .derivatives import _number_or_none, normalize_derivative_position


def contract_snapshot(
    exchange: ExchangeConfig,
    raw_positions: list[dict[str, Any]],
    *,
    markets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    positions = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            continue
        market = (markets or {}).get(raw.get("symbol")) or {}
        if market.get("option") or raw.get("option"):
            continue
        if (
            market.get("type") in {"swap", "future"}
            and market["type"] != exchange.market_type
        ):
            continue
        enriched = {
            key: market.get(key) for key in ("inverse", "settle", "contractSize")
        }
        enriched.update(raw)
        row = normalize_derivative_position(exchange, enriched, risk=RiskConfig())
        if row is not None:
            positions.append(row)
    return {"status": "ok", "checked_at": time.time(), "positions": positions}


def balance_equity_adjustments(
    exchange: ExchangeConfig, balance: dict[str, Any]
) -> dict[str, float]:
    # CCXT Bybit totals are wallet balances, while equity includes unrealized P/L.
    # Derive the difference from the same response, never from a later position poll.
    if exchange.id != "bybit":
        return {}
    result = (balance.get("info") or {}).get("result") or {}
    adjustments = {}
    for account in result.get("list", []) or []:
        for coin in account.get("coin", []) or []:
            currency = str(coin.get("coin") or "").upper()
            equity = _number_or_none(coin.get("equity"))
            total = _number_or_none((balance.get("total") or {}).get(currency))
            if total is None:
                total = _number_or_none((balance.get(currency) or {}).get("total"))
            if currency and equity is not None and total is not None:
                adjustments[currency] = equity - total
    return adjustments


def apply_contract_portfolio(
    portfolio: dict[str, Any],
    accounts: list[dict[str, Any]],
    quote_rates: dict[str, float],
) -> dict[str, Any]:
    """Account rows must already be owner filtered and connection deduplicated."""
    rates = dict(quote_rates)
    rates.setdefault(str(portfolio.get("quote_currency") or "USD"), 1.0)
    for row in portfolio.get("positions", []) or []:
        if row.get("mark_price"):
            rates.setdefault(row["asset"], row["mark_price"])
    for account in accounts:
        for row in (account.get("contract_snapshot") or {}).get("positions", []):
            if row.get("inverse") and row.get("mark_price"):
                rate = rates.get(row.get("quote_currency"))
                if rate is not None:
                    rates.setdefault(
                        row.get("settle_currency"), row["mark_price"] * rate
                    )
    rows = []
    missing: set[str] = set()
    unavailable = []
    adjustments: dict[str, float] = {}
    observed = []
    for account in accounts:
        snapshot = account.get("contract_snapshot") or {}
        key = str(account.get("exchange") or "")
        if not snapshot:
            if account.get("market_type") in {"swap", "future"} or set(
                account.get("market_types") or []
            ) & {"swap", "future"}:
                unavailable.append(key)
            continue
        if (
            snapshot.get("status") != "ok"
            or account.get("status") == "error"
            or (
                snapshot.get("checked_at")
                and time.time() - snapshot["checked_at"] > 600
            )
        ):
            unavailable.append(key)
        if snapshot.get("checked_at"):
            observed.append(snapshot["checked_at"])
        for raw in snapshot.get("positions", []):
            row = dict(raw)
            row.update(
                account=account.get("label") or key,
                exchange=key,
                exchange_id=account.get("id"),
            )
            for field, currency in (
                ("notional_quote", row.get("quote_currency")),
                ("unrealized_pnl", row.get("settle_currency")),
                ("initial_margin", row.get("settle_currency")),
            ):
                rate = _number_or_none(rates.get(currency))
                value = _number_or_none(row.get(field))
                row[field + "_common"] = (
                    value * rate if value is not None and rate is not None else None
                )
                if value not in {None, 0.0} and rate is None:
                    missing.add(str(currency or "unknown"))
            rows.append(row)
        for currency, amount in (snapshot.get("equity_adjustments") or {}).items():
            rate = _number_or_none(rates.get(currency))
            if rate is None and amount:
                missing.add(currency)
            elif rate is not None:
                adjustments[key] = adjustments.get(key, 0.0) + amount * rate

    def total(field: str) -> float | None:
        values = [row.get(field) for row in rows]
        return (
            sum(values)
            if not unavailable and all(v is not None for v in values)
            else None
        )

    pnl = total("unrealized_pnl_common")
    portfolio["contracts"] = {
        "status": "partial" if unavailable or missing or pnl is None else "ok",
        "positions": rows,
        "position_count": len(rows),
        "gross_notional": total("notional_quote_common"),
        "unrealized_pnl": pnl,
        "initial_margin": total("initial_margin_common"),
        "currency": portfolio.get("quote_currency") or "USD",
        "missing_rates": sorted(missing),
        "unavailable_accounts": unavailable,
        "observed_at": min(observed) if observed else None,
    }
    portfolio["contract_equity_adjustments"] = adjustments
    portfolio["total_asset_missing_rates"] = sorted(
        set(portfolio.get("total_asset_missing_rates", [])) | missing
    )
    # The caller supplies freshly rebuilt wallet assets; notional is never equity.
    if portfolio.get("total_asset_value") is not None:
        portfolio["total_asset_value"] += sum(adjustments.values())
    if unavailable or missing:
        portfolio["total_asset_value"] = None
    sources = dict(portfolio.get("sources") or {})
    sources["contracts_unrealized"] = pnl
    portfolio["sources"] = sources
    portfolio["total_pnl"] = (
        sum(value or 0.0 for value in sources.values()) if pnl is not None else None
    )
    return portfolio
