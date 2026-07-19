from __future__ import annotations

from typing import Any

from .models import OrderBookSnapshot
from .orderbook import estimate_fill, max_base_for_quote
from .user_strategies import UserStrategy
from .user_workspace import (
    DEX_VENUES_BY_ID,
    UserExchangeAccount,
    UserProject,
)


def _quote_currency(symbol: str) -> str:
    if "/" not in str(symbol or ""):
        return ""
    return str(symbol).split("/", 1)[1].split(":", 1)[0].strip().upper()


def _slippage_bps(
    book: OrderBookSnapshot,
    side: str,
    average_price: float,
) -> float:
    top = book.asks[0].price if side == "buy" else book.bids[0].price
    if top <= 0:
        return float("inf")
    if side == "buy":
        return max(0.0, (average_price - top) / top * 10_000)
    return max(0.0, (top - average_price) / top * 10_000)


def _candidate(
    strategy: UserStrategy,
    spot: UserExchangeAccount,
    derivative: UserExchangeAccount,
    spot_book: OrderBookSnapshot,
    derivative_book: OrderBookSnapshot,
    quote_rates: dict[str, float],
    *,
    common_notional: float,
    direction: str,
    funding_rate: float | None,
) -> dict[str, Any] | None:
    spot_rate = quote_rates.get(_quote_currency(spot.symbol))
    derivative_rate = quote_rates.get(_quote_currency(derivative.symbol))
    if spot_rate is None or derivative_rate is None:
        return None
    spot_budget = common_notional / spot_rate
    derivative_budget = common_notional / derivative_rate
    if direction == "positive_basis":
        spot_side = "buy"
        derivative_side = "sell"
        spot_levels = spot_book.asks
        derivative_levels = derivative_book.bids
    else:
        spot_side = "sell"
        derivative_side = "buy"
        spot_levels = spot_book.bids
        derivative_levels = derivative_book.asks
    quantity = min(
        max_base_for_quote(spot_levels, spot_budget),
        max_base_for_quote(derivative_levels, derivative_budget),
    )
    if quantity <= 0:
        return None
    fee_bps = float(strategy.risk["paper_fee_bps"])
    spot_fill = estimate_fill(
        spot_levels,
        side=spot_side,
        quantity_base=quantity,
        fee_bps=fee_bps,
    )
    derivative_fill = estimate_fill(
        derivative_levels,
        side=derivative_side,
        quantity_base=quantity,
        fee_bps=fee_bps,
    )
    if spot_fill is None or derivative_fill is None:
        return None
    spot_price_common = spot_fill.average_price * spot_rate
    derivative_price_common = derivative_fill.average_price * derivative_rate
    basis_bps = (
        (derivative_price_common - spot_price_common) / spot_price_common * 10_000
    )
    direction_matches = (
        basis_bps >= 0 if direction == "positive_basis" else basis_bps < 0
    )
    if not direction_matches:
        return None
    if direction == "positive_basis":
        entry_edge_common = (
            derivative_fill.net_quote * derivative_rate
            - spot_fill.net_quote * spot_rate
        )
    else:
        entry_edge_common = (
            spot_fill.net_quote * spot_rate
            - derivative_fill.net_quote * derivative_rate
        )
    reference_common = max(
        spot_fill.gross_quote * spot_rate,
        derivative_fill.gross_quote * derivative_rate,
    )
    funding_bps = funding_rate * 10_000 if funding_rate is not None else None
    supportive_funding_bps = (
        funding_bps
        if direction == "positive_basis" and funding_bps is not None
        else -funding_bps
        if direction == "negative_basis" and funding_bps is not None
        else None
    )
    max_slippage = max(
        _slippage_bps(spot_book, spot_side, spot_fill.average_price),
        _slippage_bps(
            derivative_book,
            derivative_side,
            derivative_fill.average_price,
        ),
    )
    min_funding = float(strategy.parameters["min_funding_bps"])
    funding_ok = min_funding <= 0 or (
        supportive_funding_bps is not None and supportive_funding_bps >= min_funding
    )
    return {
        "direction": direction,
        "spot_account_id": spot.id,
        "spot_exchange": spot.exchange,
        "spot_symbol": spot.symbol,
        "derivative_account_id": derivative.id,
        "derivative_exchange": derivative.exchange,
        "derivative_symbol": derivative.symbol,
        "derivative_venue_type": (
            "dex" if derivative.exchange in DEX_VENUES_BY_ID else "cex"
        ),
        "spot_side": spot_side,
        "derivative_side": derivative_side,
        "quantity_base": quantity,
        "spot_average_price": spot_fill.average_price,
        "derivative_average_price": derivative_fill.average_price,
        "basis_bps": basis_bps,
        "funding_rate_bps": funding_bps,
        "supportive_funding_bps": supportive_funding_bps,
        "entry_edge_common": entry_edge_common,
        "entry_edge_bps": (
            entry_edge_common / reference_common * 10_000
            if reference_common > 0
            else 0.0
        ),
        "max_slippage_bps": max_slippage,
        "basis_ok": abs(basis_bps) >= float(strategy.parameters["min_basis_bps"]),
        "funding_ok": funding_ok,
        "slippage_ok": max_slippage <= float(strategy.risk["max_slippage_bps"]),
        "leverage": float(strategy.parameters["max_leverage"]),
        "mode": "paper_scan",
        "live_submit_allowed": False,
    }


def scan_user_contract_arbitrage(
    strategy: UserStrategy,
    project: UserProject,
    accounts: list[UserExchangeAccount],
    books: dict[str, OrderBookSnapshot],
    quote_rates: dict[str, float],
    funding_rates: dict[str, float | None],
) -> tuple[str, str, str, dict[str, Any], dict[str, Any]]:
    project_rate = quote_rates.get(project.quote_currency)
    if project_rate is None:
        return (
            "blocked_quote_rate",
            f"quote rate is unavailable: {project.quote_currency}",
            "blocked",
            {},
            {},
        )
    common_notional = float(strategy.parameters["max_cycle_quote"]) * project_rate
    spot_accounts = [row for row in accounts if row.market_type == "spot"]
    derivative_accounts = [
        row for row in accounts if row.market_type in {"swap", "future"}
    ]
    observations = [
        candidate
        for spot in spot_accounts
        for derivative in derivative_accounts
        for direction in ("positive_basis", "negative_basis")
        for candidate in [
            _candidate(
                strategy,
                spot,
                derivative,
                books[spot.id],
                books[derivative.id],
                quote_rates,
                common_notional=common_notional,
                direction=direction,
                funding_rate=funding_rates.get(derivative.id),
            )
        ]
        if candidate is not None
    ]
    ranked = sorted(
        observations,
        key=lambda row: (
            bool(row["basis_ok"] and row["funding_ok"] and row["slippage_ok"]),
            abs(float(row["basis_bps"])),
            float(row.get("supportive_funding_bps") or 0.0),
        ),
        reverse=True,
    )
    candidates = [
        row
        for row in ranked
        if row["basis_ok"] and row["funding_ok"] and row["slippage_ok"]
    ]
    best = ranked[0] if ranked else None
    scan = {
        "candidate_count": len(candidates),
        "observation_count": len(observations),
        "best": best,
        "max_cycle_quote": float(strategy.parameters["max_cycle_quote"]),
        "project_quote_currency": project.quote_currency,
        "common_notional": common_notional,
        "paper_scan_only": True,
        "live_submit_allowed": False,
    }
    if not candidates:
        if best is None:
            reason = "no compatible spot/perpetual depth is available"
        elif not best["slippage_ok"]:
            reason = "contract arbitrage candidate exceeds max slippage"
        elif not best["basis_ok"]:
            reason = (
                f"basis {abs(float(best['basis_bps'])):.2f} bps is below "
                f"{float(strategy.parameters['min_basis_bps']):.2f} bps"
            )
        elif not best["funding_ok"]:
            funding = best.get("supportive_funding_bps")
            reason = (
                "funding rate is unavailable"
                if funding is None
                else f"supportive funding {float(funding):.2f} bps is below threshold"
            )
        else:
            reason = "no contract arbitrage candidate"
        return "waiting", reason, "waiting", best or {}, scan
    selected = candidates[0]
    venue = str(selected["derivative_venue_type"]).upper()
    reason = (
        f"{venue} contract candidate: {abs(float(selected['basis_bps'])):.2f} bps basis"
    )
    return "candidate", reason, "candidate", selected, scan
