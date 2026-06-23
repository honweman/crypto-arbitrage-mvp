from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from .config import RiskConfig
from .execution_protection import build_multileg_execution_protection
from .models import OrderBookSnapshot
from .strategy_center import FundingArbitrageSettings, StrategyInstance


FUNDING_PERIODS_PER_DAY = 3.0
FUNDING_DAYS_PER_YEAR = 365.0


def _mid_price(book: OrderBookSnapshot | None) -> float | None:
    if book is None or not book.bids or not book.asks:
        return None
    bid = float(book.bids[0].price)
    ask = float(book.asks[0].price)
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def _basis_bps(spot_mid: float | None, derivative_mid: float | None) -> float | None:
    if spot_mid is None or derivative_mid is None or spot_mid <= 0:
        return None
    return (derivative_mid - spot_mid) / spot_mid * 10_000.0


def _rate_bps(rate: float | None) -> float | None:
    if rate is None:
        return None
    return float(rate) * 10_000.0


def funding_settings_from_strategy_center(
    payload: dict[str, Any] | None,
) -> list[FundingArbitrageSettings]:
    if not isinstance(payload, dict):
        return []

    settings: list[FundingArbitrageSettings] = []
    global_settings = FundingArbitrageSettings.from_dict(
        payload.get("funding_arbitrage", {})
    )
    if (
        global_settings.enabled
        or global_settings.spot_symbol
        or global_settings.derivative_symbol
    ):
        settings.append(global_settings)

    for raw in payload.get("strategy_instances", []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            instance = StrategyInstance.from_dict(raw)
        except ValueError:
            continue
        if instance.strategy_type != "funding_arbitrage":
            continue
        params = dict(instance.parameters)
        params.setdefault("enabled", instance.enabled)
        params.setdefault("pair_id", instance.name or instance.id)
        params.setdefault("spot_exchange", instance.exchange)
        params.setdefault("spot_symbol", instance.symbol)
        settings.append(FundingArbitrageSettings.from_dict(params))

    deduped: list[FundingArbitrageSettings] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in settings:
        key = (
            item.spot_exchange,
            item.spot_symbol,
            item.derivative_exchange,
            item.derivative_symbol,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def funding_basis_row(
    settings: FundingArbitrageSettings,
    *,
    spot_book: OrderBookSnapshot | None,
    derivative_book: OrderBookSnapshot | None,
    funding_rate: float | None,
    notional_quote: float,
    risk: RiskConfig | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    observed_at = time.time() if now is None else now
    spot_mid = _mid_price(spot_book)
    derivative_mid = _mid_price(derivative_book)
    basis = _basis_bps(spot_mid, derivative_mid)
    funding_bps = _rate_bps(funding_rate)
    abs_min_funding = abs(float(settings.min_funding_bps or 0.0))
    min_entry_basis = abs(float(settings.min_entry_basis_bps or 0.0))
    max_entry_basis = abs(float(settings.max_entry_basis_bps or 0.0))
    reasons: list[str] = []
    warnings: list[str] = []
    direction = ""
    candidate = False

    if not settings.enabled:
        reasons.append("funding arbitrage disabled")
    if not settings.spot_exchange or not settings.spot_symbol:
        reasons.append("spot leg not configured")
    if not settings.derivative_exchange or not settings.derivative_symbol:
        reasons.append("perp leg not configured")
    if spot_mid is None:
        reasons.append("spot order book unavailable")
    if derivative_mid is None:
        reasons.append("perp order book unavailable")
    if funding_bps is None:
        reasons.append("funding rate unavailable")

    if basis is not None and funding_bps is not None and not reasons:
        if funding_bps >= abs_min_funding:
            direction = "long_spot_short_perp"
            if basis < min_entry_basis:
                reasons.append(
                    f"basis {basis:.4g} bps < min entry {min_entry_basis:.4g} bps"
                )
            elif max_entry_basis > 0 and basis > max_entry_basis:
                reasons.append(
                    f"basis {basis:.4g} bps > max entry {max_entry_basis:.4g} bps"
                )
            else:
                candidate = True
        elif funding_bps <= -abs_min_funding:
            direction = "short_spot_long_perp"
            warnings.append("reverse funding needs spot inventory or borrow controls")
            if basis > -min_entry_basis:
                reasons.append(
                    f"basis {basis:.4g} bps > negative entry -{min_entry_basis:.4g} bps"
                )
            elif max_entry_basis > 0 and abs(basis) > max_entry_basis:
                reasons.append(
                    f"basis {basis:.4g} bps exceeds max entry {max_entry_basis:.4g} bps"
                )
            else:
                candidate = True
        else:
            reasons.append(
                f"funding {funding_bps:.4g} bps below threshold {abs_min_funding:.4g} bps"
            )

    if candidate:
        status = "candidate"
        paper_state = "would_open"
        reason = "entry conditions met"
    elif reasons:
        status = "disabled" if reasons == ["funding arbitrage disabled"] else "watching"
        paper_state = "blocked"
        reason = reasons[0]
    else:
        status = "watching"
        paper_state = "waiting"
        reason = "waiting for entry conditions"

    suggested_legs: list[dict[str, Any]] = []
    base_asset = str(settings.spot_symbol or settings.derivative_symbol).split("/", 1)[0]
    spot_quantity_raw = notional_quote / spot_mid if spot_mid and spot_mid > 0 else 0.0
    derivative_quantity_raw = (
        notional_quote / derivative_mid if derivative_mid and derivative_mid > 0 else 0.0
    )
    hedged_quantity = min(spot_quantity_raw, derivative_quantity_raw)
    spot_notional = hedged_quantity * spot_mid if spot_mid else notional_quote
    derivative_notional = (
        hedged_quantity * derivative_mid if derivative_mid else notional_quote
    )
    if candidate and direction == "long_spot_short_perp":
        suggested_legs = [
            {
                "exchange": settings.spot_exchange,
                "symbol": settings.spot_symbol,
                "side": "buy",
                "type": "spot",
                "quantity_base": hedged_quantity,
                "average_price": spot_mid,
                "notional_quote": spot_notional,
                "hedge_asset": base_asset,
                "hedge_base_equivalent": hedged_quantity,
            },
            {
                "exchange": settings.derivative_exchange,
                "symbol": settings.derivative_symbol,
                "side": "sell",
                "type": "perp",
                "quantity_base": hedged_quantity,
                "average_price": derivative_mid,
                "notional_quote": derivative_notional,
                "hedge_asset": base_asset,
                "hedge_base_equivalent": hedged_quantity,
            },
        ]
    elif candidate and direction == "short_spot_long_perp":
        suggested_legs = [
            {
                "exchange": settings.spot_exchange,
                "symbol": settings.spot_symbol,
                "side": "sell",
                "type": "spot",
                "quantity_base": hedged_quantity,
                "average_price": spot_mid,
                "notional_quote": spot_notional,
                "hedge_asset": base_asset,
                "hedge_base_equivalent": hedged_quantity,
            },
            {
                "exchange": settings.derivative_exchange,
                "symbol": settings.derivative_symbol,
                "side": "buy",
                "type": "perp",
                "quantity_base": hedged_quantity,
                "average_price": derivative_mid,
                "notional_quote": derivative_notional,
                "hedge_asset": base_asset,
                "hedge_base_equivalent": hedged_quantity,
            },
        ]
    protection = (
        build_multileg_execution_protection(
            strategy="funding_arbitrage",
            legs=suggested_legs,
            risk=risk or RiskConfig(),
            observed_at=observed_at,
            now=observed_at,
        )
        if suggested_legs
        else None
    )

    return {
        "pair_id": settings.pair_id
        or f"{settings.spot_exchange}:{settings.spot_symbol}/{settings.derivative_exchange}:{settings.derivative_symbol}",
        "enabled": settings.enabled,
        "spot_exchange": settings.spot_exchange,
        "spot_symbol": settings.spot_symbol,
        "derivative_exchange": settings.derivative_exchange,
        "derivative_symbol": settings.derivative_symbol,
        "spot_mid": spot_mid,
        "derivative_mid": derivative_mid,
        "basis_bps": basis,
        "funding_rate": funding_rate,
        "funding_rate_bps": funding_bps,
        "estimated_daily_funding_bps": (
            funding_bps * FUNDING_PERIODS_PER_DAY
            if funding_bps is not None
            else None
        ),
        "estimated_apr_pct": (
            float(funding_rate)
            * FUNDING_PERIODS_PER_DAY
            * FUNDING_DAYS_PER_YEAR
            * 100.0
            if funding_rate is not None
            else None
        ),
        "direction": direction,
        "status": status,
        "reason": reason,
        "reasons": reasons,
        "warnings": warnings,
        "thresholds": {
            "min_funding_bps": settings.min_funding_bps,
            "min_entry_basis_bps": settings.min_entry_basis_bps,
            "max_entry_basis_bps": settings.max_entry_basis_bps,
            "take_profit_bps": settings.take_profit_bps,
            "stop_loss_bps": settings.stop_loss_bps,
            "max_margin_usage_pct": settings.max_margin_usage_pct,
            "min_liquidation_buffer_pct": settings.min_liquidation_buffer_pct,
        },
        "paper_execution": {
            "mode": "paper",
            "state": paper_state,
            "live_enabled": False,
            "notional_quote": notional_quote,
            "suggested_legs": suggested_legs,
            "protection": protection,
            "reason": reason,
        },
        "observed_at": observed_at,
    }


def funding_basis_payload(
    settings_rows: Iterable[FundingArbitrageSettings],
    *,
    spot_books: dict[tuple[str, str], OrderBookSnapshot],
    derivative_books: dict[tuple[str, str], OrderBookSnapshot],
    funding_rates: dict[tuple[str, str], float],
    notional_quote: float,
    risk: RiskConfig | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    rows = [
        funding_basis_row(
            settings,
            spot_book=spot_books.get((settings.spot_exchange, settings.spot_symbol)),
            derivative_book=derivative_books.get(
                (settings.derivative_exchange, settings.derivative_symbol)
            ),
            funding_rate=funding_rates.get(
                (settings.derivative_exchange, settings.derivative_symbol)
            ),
            notional_quote=notional_quote,
            risk=risk,
            now=now,
        )
        for settings in settings_rows
    ]
    errors = [
        f"{row['pair_id']}: {row['reason']}"
        for row in rows
        if row["status"] not in {"candidate", "watching", "disabled"}
    ]
    warnings = [
        f"{row['pair_id']}: {warning}"
        for row in rows
        for warning in row.get("warnings", [])
    ]
    if not rows:
        status = "disabled"
    elif any(row["status"] == "candidate" for row in rows):
        status = "candidate"
    elif any(row["status"] == "watching" for row in rows):
        status = "watching"
    else:
        status = "disabled"
    return {
        "status": status,
        "mode": "paper",
        "rows": rows,
        "candidate_count": sum(1 for row in rows if row["status"] == "candidate"),
        "configured_count": len(rows),
        "checked_count": sum(
            1
            for row in rows
            if row.get("spot_mid") is not None
            and row.get("derivative_mid") is not None
            and row.get("funding_rate_bps") is not None
        ),
        "errors": errors,
        "warnings": warnings,
        "last_finished": time.time() if now is None else now,
    }
