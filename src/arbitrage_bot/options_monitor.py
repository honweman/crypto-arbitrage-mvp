from __future__ import annotations

import time
from typing import Any

from .config import BotConfig, OptionComboConfig
from .models import Opportunity, OrderBookSnapshot
from .strategies.options_arbitrage import find_options_arbitrage_opportunities


def _best_bid_ask(book: OrderBookSnapshot | None) -> tuple[float, float] | None:
    if book is None or not book.bids or not book.asks:
        return None
    bid = float(book.bids[0].price)
    ask = float(book.asks[0].price)
    if bid <= 0 or ask <= 0:
        return None
    return bid, ask


def _mid(book: OrderBookSnapshot | None) -> float | None:
    best = _best_bid_ask(book)
    if best is None:
        return None
    return (best[0] + best[1]) / 2.0


def _combo_key(combo: OptionComboConfig) -> tuple[str, str, str, float, str]:
    return (
        combo.underlying,
        combo.call_symbol,
        combo.put_symbol,
        combo.strike,
        combo.expiry,
    )


def _opportunity_combo_key(opportunity: Opportunity) -> tuple[str, str, str, float, str]:
    metadata = opportunity.metadata
    call_symbol = ""
    put_symbol = ""
    for leg in opportunity.legs:
        side_symbol = leg.symbol
        if "-C" in side_symbol or side_symbol.endswith("C"):
            call_symbol = side_symbol
        elif "-P" in side_symbol or side_symbol.endswith("P"):
            put_symbol = side_symbol
    return (
        str(metadata.get("underlying") or ""),
        call_symbol,
        put_symbol,
        float(metadata.get("strike") or 0.0),
        str(metadata.get("expiry") or ""),
    )


def _paper_state(
    opportunity: Opportunity | None,
    reason: str,
) -> dict[str, Any]:
    if opportunity is None:
        return {
            "mode": "paper",
            "state": "waiting" if reason == "edge below thresholds" else "blocked",
            "live_enabled": False,
            "suggested_legs": [],
            "reason": reason,
        }
    return {
        "mode": "paper",
        "state": "would_open",
        "live_enabled": False,
        "suggested_legs": [
            {
                "exchange": leg.exchange,
                "symbol": leg.symbol,
                "side": leg.side,
                "quantity_base": leg.quantity_base,
                "average_price": leg.average_price,
            }
            for leg in opportunity.legs
        ],
        "reason": "entry conditions met",
    }


def options_arbitrage_payload(
    cfg: BotConfig,
    *,
    spot_books: dict[tuple[str, str], OrderBookSnapshot],
    option_books: dict[tuple[str, str], OrderBookSnapshot],
    now: float | None = None,
) -> dict[str, Any]:
    observed_at = time.time() if now is None else now
    if not cfg.option_combos:
        return {
            "status": "disabled",
            "mode": "paper",
            "rows": [],
            "opportunities": [],
            "candidate_count": 0,
            "configured_count": 0,
            "checked_count": 0,
            "last_finished": observed_at,
            "errors": [],
            "warnings": [],
        }

    opportunities = (
        find_options_arbitrage_opportunities(
            spot_books=spot_books,
            option_books=option_books,
            spot_exchanges=cfg.spot_exchanges,
            option_exchanges=cfg.derivative_exchanges,
            combos=cfg.option_combos,
            cfg=cfg.options_arbitrage,
        )
        if cfg.options_arbitrage.enabled
        else []
    )
    best_by_combo: dict[tuple[str, str, str, float, str], Opportunity] = {}
    for opportunity in opportunities:
        key = _opportunity_combo_key(opportunity)
        current = best_by_combo.get(key)
        if current is None or opportunity.profit_bps > current.profit_bps:
            best_by_combo[key] = opportunity

    rows: list[dict[str, Any]] = []
    for combo in cfg.option_combos:
        spot_book = spot_books.get((combo.spot_exchange, combo.spot_symbol))
        call_book = option_books.get((combo.option_exchange, combo.call_symbol))
        put_book = option_books.get((combo.option_exchange, combo.put_symbol))
        spot_mid = _mid(spot_book)
        call_mid = _mid(call_book)
        put_mid = _mid(put_book)
        synthetic_forward_mid = (
            call_mid - put_mid + combo.strike
            if call_mid is not None and put_mid is not None
            else None
        )
        parity_gap_bps = (
            (synthetic_forward_mid - spot_mid) / spot_mid * 10_000.0
            if synthetic_forward_mid is not None and spot_mid
            else None
        )
        reasons: list[str] = []
        if not cfg.options_arbitrage.enabled:
            reasons.append("options arbitrage disabled")
        else:
            if spot_mid is None:
                reasons.append("spot order book unavailable")
            if call_mid is None:
                reasons.append("call order book unavailable")
            if put_mid is None:
                reasons.append("put order book unavailable")
        opportunity = best_by_combo.get(_combo_key(combo))
        if opportunity is None and not reasons and cfg.options_arbitrage.enabled:
            reasons.append("edge below thresholds")
        if opportunity is not None:
            status = "candidate"
            reason = "entry conditions met"
        elif reasons == ["options arbitrage disabled"]:
            status = "disabled"
            reason = reasons[0]
        else:
            status = "watching"
            reason = reasons[0] if reasons else "waiting for entry conditions"
        rows.append(
            {
                "underlying": combo.underlying,
                "spot_exchange": combo.spot_exchange,
                "spot_symbol": combo.spot_symbol,
                "option_exchange": combo.option_exchange,
                "call_symbol": combo.call_symbol,
                "put_symbol": combo.put_symbol,
                "strike": combo.strike,
                "expiry": combo.expiry,
                "contract_size": combo.contract_size,
                "quote_currency": combo.quote_currency,
                "spot_mid": spot_mid,
                "call_mid": call_mid,
                "put_mid": put_mid,
                "synthetic_forward_mid": synthetic_forward_mid,
                "parity_gap_bps": parity_gap_bps,
                "status": status,
                "reason": reason,
                "reasons": reasons,
                "opportunity": opportunity.to_dict() if opportunity else None,
                "paper_execution": _paper_state(opportunity, reason),
                "observed_at": observed_at,
            }
        )

    status = "disabled"
    if cfg.options_arbitrage.enabled:
        status = "candidate" if opportunities else "watching"
    return {
        "status": status,
        "mode": "paper",
        "rows": rows,
        "opportunities": [item.to_dict() for item in opportunities[:10]],
        "candidate_count": len(opportunities),
        "configured_count": len(cfg.option_combos),
        "checked_count": sum(
            1
            for row in rows
            if row.get("spot_mid") is not None
            and row.get("call_mid") is not None
            and row.get("put_mid") is not None
        ),
        "thresholds": {
            "notional_quote": cfg.options_arbitrage.notional_quote,
            "min_edge_quote": cfg.options_arbitrage.min_edge_quote,
            "min_edge_bps": cfg.options_arbitrage.min_edge_bps,
            "max_contracts": cfg.options_arbitrage.max_contracts,
            "max_days_to_expiry": cfg.options_arbitrage.max_days_to_expiry,
        },
        "last_finished": observed_at,
        "errors": [],
        "warnings": [
            "paper mode only; live option exercise, assignment, and margin controls are not enabled"
        ]
        if cfg.options_arbitrage.enabled
        else [],
    }
