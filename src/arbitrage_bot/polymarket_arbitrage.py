from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from .models import BookLevel, FillEstimate, OrderBookSnapshot
from .orderbook import available_base, estimate_fill, max_base_for_quote
from .user_strategies import UserStrategy
from .user_workspace import DEX_VENUES_BY_ID, UserExchangeAccount, UserProject


POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
MAX_BATCH_TOKEN_IDS = 500


@dataclass(frozen=True)
class PolymarketBook:
    token_id: str
    condition_id: str
    snapshot: OrderBookSnapshot
    neg_risk: bool = False
    min_order_size: float = 0.0
    tick_size: float = 0.0
    book_hash: str = ""


def prediction_token_ids(parameters: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            [
                str(item)
                for key in ("outcome_asset_ids", "neg_risk_no_asset_ids")
                for item in parameters.get(key, [])
                if str(item)
            ]
        )
    )


def _timestamp_ms(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1000)
    if number < 10_000_000_000:
        number *= 1000
    return int(number)


def _levels(raw: Any, *, reverse: bool, depth: int) -> list[BookLevel]:
    rows: list[BookLevel] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("price"))
            amount = float(item.get("size"))
        except (TypeError, ValueError):
            continue
        if 0 < price < 1 and amount > 0 and math.isfinite(price + amount):
            rows.append(BookLevel(price=price, amount=amount))
    return sorted(rows, key=lambda row: row.price, reverse=reverse)[:depth]


def parse_polymarket_books(
    payload: Any,
    *,
    depth: int,
    received_at: float | None = None,
) -> dict[str, PolymarketBook]:
    if not isinstance(payload, list):
        raise ValueError("Polymarket order book response must be a list")
    observed_at = time.time() if received_at is None else float(received_at)
    result: dict[str, PolymarketBook] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        token_id = str(raw.get("asset_id") or "").strip()
        if not token_id:
            continue
        bids = _levels(raw.get("bids"), reverse=True, depth=depth)
        asks = _levels(raw.get("asks"), reverse=False, depth=depth)
        result[token_id] = PolymarketBook(
            token_id=token_id,
            condition_id=str(raw.get("market") or "").strip(),
            snapshot=OrderBookSnapshot(
                exchange="polymarket",
                symbol=token_id,
                bids=bids,
                asks=asks,
                timestamp_ms=_timestamp_ms(raw.get("timestamp")),
                source="polymarket_clob_rest",
                received_at=observed_at,
            ),
            neg_risk=bool(raw.get("neg_risk")),
            min_order_size=max(0.0, float(raw.get("min_order_size") or 0.0)),
            tick_size=max(0.0, float(raw.get("tick_size") or 0.0)),
            book_hash=str(raw.get("hash") or "")[:160],
        )
    return result


async def fetch_polymarket_order_books(
    token_ids: list[str],
    *,
    depth: int = 20,
    timeout_seconds: float = 15.0,
) -> dict[str, PolymarketBook]:
    unique_ids = list(dict.fromkeys(str(item).strip() for item in token_ids if item))
    if not unique_ids:
        return {}
    if len(unique_ids) > MAX_BATCH_TOKEN_IDS:
        raise ValueError(f"Polymarket supports at most {MAX_BATCH_TOKEN_IDS} token IDs")
    timeout = aiohttp.ClientTimeout(total=max(2.0, float(timeout_seconds)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{POLYMARKET_CLOB_URL}/books",
                json=[{"token_id": token_id} for token_id in unique_ids],
            ) as response:
                if response.status >= 400:
                    body = (await response.text())[:240]
                    raise RuntimeError(f"Polymarket HTTP {response.status}: {body}")
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(f"Polymarket market data failed: {exc}") from exc
    books = parse_polymarket_books(payload, depth=max(1, int(depth)))
    missing = [token_id for token_id in unique_ids if token_id not in books]
    if missing:
        raise ValueError(
            "Polymarket order books are unavailable for token IDs: "
            + ", ".join(missing[:3])
        )
    return books


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
        return math.inf
    if side == "buy":
        return max(0.0, (average_price - top) / top * 10_000)
    return max(0.0, (top - average_price) / top * 10_000)


def _bundle_quantity(
    levels_by_leg: list[list[BookLevel]],
    *,
    side: str,
    quote_budget: float,
    fee_bps: float,
) -> float:
    if not levels_by_leg or quote_budget <= 0:
        return 0.0
    maximum = min(available_base(levels) for levels in levels_by_leg)
    if side == "sell":
        return min(maximum, quote_budget)
    top_sum = sum(levels[0].price for levels in levels_by_leg if levels)
    if top_sum <= 0:
        return 0.0
    maximum = min(maximum, quote_budget / top_sum)
    low = 0.0
    high = maximum
    for _ in range(48):
        quantity = (low + high) / 2
        fills = [
            estimate_fill(levels, side="buy", quantity_base=quantity, fee_bps=fee_bps)
            for levels in levels_by_leg
        ]
        cost = sum(fill.net_quote for fill in fills if fill is not None)
        if all(fill is not None for fill in fills) and cost <= quote_budget:
            low = quantity
        else:
            high = quantity
    return low


def _fill_rows(
    token_ids: list[str],
    books: dict[str, PolymarketBook],
    *,
    side: str,
    quantity: float,
    fee_bps: float,
) -> tuple[list[FillEstimate], float]:
    fills: list[FillEstimate] = []
    max_slippage = 0.0
    for token_id in token_ids:
        snapshot = books[token_id].snapshot
        levels = snapshot.asks if side == "buy" else snapshot.bids
        fill = estimate_fill(
            levels,
            side=side,
            quantity_base=quantity,
            fee_bps=fee_bps,
        )
        if fill is None:
            return [], math.inf
        fills.append(fill)
        max_slippage = max(
            max_slippage,
            _slippage_bps(snapshot, side, fill.average_price),
        )
    return fills, max_slippage


def _leg_payload(
    token_id: str,
    fill: FillEstimate,
    *,
    outcome_role: str,
) -> dict[str, Any]:
    return {
        "venue": "polymarket",
        "market_type": "prediction",
        "token_id": token_id,
        "outcome_role": outcome_role,
        "side": fill.side,
        "quantity": fill.quantity_base,
        "average_price": fill.average_price,
        "gross_quote": fill.gross_quote,
        "fee_quote": fill.fee_quote,
        "quote_currency": "USDC",
    }


def _candidate_rank(row: dict[str, Any]) -> tuple[bool, float, float]:
    return (
        bool(row.get("qualified")),
        float(row.get("profit_quote") or row.get("model_edge_quote") or 0.0),
        float(row.get("profit_bps") or row.get("model_edge_bps") or 0.0),
    )


def _complete_set_candidates(
    strategy: UserStrategy,
    books: dict[str, PolymarketBook],
    token_ids: list[str],
    *,
    usdc_budget: float,
) -> list[dict[str, Any]]:
    if len(token_ids) < 2 or any(token_id not in books for token_id in token_ids):
        return []
    condition_ids = {books[token_id].condition_id for token_id in token_ids}
    if len(token_ids) == 2 and ("" in condition_ids or len(condition_ids) != 1):
        return []
    if len(token_ids) > 2 and (
        not strategy.parameters["event_group_id"]
        or any(not books[token_id].neg_risk for token_id in token_ids)
    ):
        return []
    fee_bps = float(strategy.risk["paper_fee_bps"])
    conversion_bps = float(strategy.parameters["conversion_cost_bps"])
    min_profit_bps = float(strategy.parameters["min_profit_bps"])
    max_slippage_bps = float(strategy.risk["max_slippage_bps"])
    candidates: list[dict[str, Any]] = []
    for side, mechanism in (
        ("buy", "complete_set_buy"),
        ("sell", "complete_set_reverse"),
    ):
        levels = [
            books[token_id].snapshot.asks
            if side == "buy"
            else books[token_id].snapshot.bids
            for token_id in token_ids
        ]
        if any(not row for row in levels):
            continue
        quantity = _bundle_quantity(
            levels,
            side=side,
            quote_budget=usdc_budget,
            fee_bps=fee_bps,
        )
        minimum = max(books[token_id].min_order_size for token_id in token_ids)
        if quantity <= 1e-12 or quantity + 1e-12 < minimum:
            continue
        fills, max_slippage = _fill_rows(
            token_ids,
            books,
            side=side,
            quantity=quantity,
            fee_bps=fee_bps,
        )
        if not fills:
            continue
        conversion_cost = quantity * conversion_bps / 10_000
        if side == "buy":
            invested = sum(fill.net_quote for fill in fills) + conversion_cost
            profit = quantity - invested
        else:
            invested = quantity + conversion_cost
            profit = sum(fill.net_quote for fill in fills) - invested
        profit_bps = profit / invested * 10_000 if invested > 0 else 0.0
        candidates.append(
            {
                "mechanism": mechanism,
                "risk_class": "structural",
                "quantity": quantity,
                "payout_or_collateral_quote": quantity,
                "invested_quote": invested,
                "profit_quote": profit,
                "profit_bps": profit_bps,
                "max_slippage_bps": max_slippage,
                "inventory_required": side == "sell",
                "token_count": len(token_ids),
                "legs": [
                    _leg_payload(token_id, fill, outcome_role=f"outcome_{index + 1}")
                    for index, (token_id, fill) in enumerate(zip(token_ids, fills))
                ],
                "qualified": profit_bps >= min_profit_bps
                and max_slippage <= max_slippage_bps,
                "execution_notes": (
                    ["merge complete set after all buy legs fill"]
                    if side == "buy"
                    else [
                        "requires complete-set inventory or collateral split before selling"
                    ]
                ),
                "live_submit_allowed": False,
            }
        )
    return candidates


def _neg_risk_candidates(
    strategy: UserStrategy,
    books: dict[str, PolymarketBook],
    yes_ids: list[str],
    no_ids: list[str],
    *,
    usdc_budget: float,
) -> list[dict[str, Any]]:
    if (
        len(yes_ids) < 3
        or len(no_ids) != len(yes_ids)
        or any(token_id not in books for token_id in yes_ids + no_ids)
        or any(
            not books[yes_token_id].condition_id
            or books[yes_token_id].condition_id
            != books[no_token_id].condition_id
            for yes_token_id, no_token_id in zip(yes_ids, no_ids)
        )
    ):
        return []
    fee_bps = float(strategy.risk["paper_fee_bps"])
    conversion_bps = float(strategy.parameters["conversion_cost_bps"])
    min_profit_bps = float(strategy.parameters["min_profit_bps"])
    max_slippage_bps = float(strategy.risk["max_slippage_bps"])
    candidates: list[dict[str, Any]] = []
    for index, no_token_id in enumerate(no_ids):
        sell_yes_ids = [
            token_id for item, token_id in enumerate(yes_ids) if item != index
        ]
        no_book = books[no_token_id]
        sell_books = [books[token_id] for token_id in sell_yes_ids]
        if (
            not no_book.neg_risk
            or any(not row.neg_risk for row in sell_books)
            or not no_book.snapshot.asks
            or any(not row.snapshot.bids for row in sell_books)
        ):
            continue
        buy_capacity = max_base_for_quote(
            no_book.snapshot.asks,
            usdc_budget / (1 + fee_bps / 10_000),
        )
        quantity = min(
            buy_capacity,
            available_base(no_book.snapshot.asks),
            *(available_base(row.snapshot.bids) for row in sell_books),
        )
        minimum = max(
            [no_book.min_order_size]
            + [row.min_order_size for row in sell_books]
        )
        if quantity <= 1e-12 or quantity + 1e-12 < minimum:
            continue
        buy_fill = estimate_fill(
            no_book.snapshot.asks,
            side="buy",
            quantity_base=quantity,
            fee_bps=fee_bps,
        )
        sell_fills = [
            estimate_fill(
                row.snapshot.bids,
                side="sell",
                quantity_base=quantity,
                fee_bps=fee_bps,
            )
            for row in sell_books
        ]
        if buy_fill is None or any(fill is None for fill in sell_fills):
            continue
        valid_sells = [fill for fill in sell_fills if fill is not None]
        conversion_cost = quantity * conversion_bps / 10_000
        invested = buy_fill.net_quote + conversion_cost
        proceeds = sum(fill.net_quote for fill in valid_sells)
        profit = proceeds - invested
        profit_bps = profit / invested * 10_000 if invested > 0 else 0.0
        max_slippage = max(
            _slippage_bps(no_book.snapshot, "buy", buy_fill.average_price),
            *(
                _slippage_bps(row.snapshot, "sell", fill.average_price)
                for row, fill in zip(sell_books, valid_sells)
            ),
        )
        candidates.append(
            {
                "mechanism": "neg_risk_no_to_other_yes",
                "risk_class": "structural_conversion",
                "source_outcome_index": index,
                "quantity": quantity,
                "invested_quote": invested,
                "proceeds_quote": proceeds,
                "profit_quote": profit,
                "profit_bps": profit_bps,
                "max_slippage_bps": max_slippage,
                "inventory_required": False,
                "legs": [
                    _leg_payload(no_token_id, buy_fill, outcome_role="source_no"),
                    *[
                        _leg_payload(token_id, fill, outcome_role="converted_yes")
                        for token_id, fill in zip(sell_yes_ids, valid_sells)
                    ],
                ],
                "qualified": profit_bps >= min_profit_bps
                and max_slippage <= max_slippage_bps,
                "execution_notes": [
                    "all buy, conversion and sell legs must complete",
                    "augmented Other outcome requires manual resolution-rule review",
                ],
                "manual_review_required": bool(
                    strategy.parameters["augmented_neg_risk"]
                ),
                "live_submit_allowed": False,
            }
        )
    return candidates


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def _normal_pdf(value: float) -> float:
    return math.exp(-(value**2) / 2) / math.sqrt(2 * math.pi)


def _digital_probability_and_delta(
    *,
    spot: float,
    strike: float,
    time_years: float,
    volatility: float,
    direction: str,
) -> tuple[float, float]:
    denominator = volatility * math.sqrt(time_years)
    d2 = (math.log(spot / strike) - 0.5 * volatility**2 * time_years) / denominator
    above_probability = _normal_cdf(d2)
    above_delta = _normal_pdf(d2) / (spot * denominator)
    if direction == "above":
        return above_probability, above_delta
    return 1 - above_probability, -above_delta


def _cross_venue_candidate(
    strategy: UserStrategy,
    accounts: list[UserExchangeAccount],
    hedge_books: dict[str, OrderBookSnapshot],
    books: dict[str, PolymarketBook],
    quote_rates: dict[str, float],
    *,
    usdc_rate: float,
    common_budget: float,
    now: float,
) -> dict[str, Any] | None:
    token_ids = strategy.parameters["outcome_asset_ids"]
    if not token_ids or token_ids[0] not in books or not accounts:
        return None
    if not strategy.parameters["resolution_source_confirmed"]:
        return None
    expiry = float(strategy.parameters["resolution_timestamp"])
    remaining = expiry - now
    if remaining < float(strategy.parameters["min_time_to_resolution_seconds"]):
        return None
    eligible_accounts = [
        account
        for account in accounts
        if account.id in hedge_books
        and (
            not strategy.parameters["require_dex_hedge"]
            or account.exchange in DEX_VENUES_BY_ID
        )
    ]
    references: list[tuple[UserExchangeAccount, float, float]] = []
    for account in eligible_accounts:
        book = hedge_books[account.id]
        quote_rate = quote_rates.get(_quote_currency(account.symbol))
        if quote_rate is None or not book.bids or not book.asks:
            continue
        mid = (book.bids[0].price + book.asks[0].price) / 2
        spread_bps = (book.asks[0].price - book.bids[0].price) / mid * 10_000
        references.append((account, mid * quote_rate, spread_bps))
    if not references:
        return None
    reference_price = statistics.median(row[1] for row in references)
    account, _, _ = min(references, key=lambda row: row[2])
    hedge_book = hedge_books[account.id]
    probability, binary_delta = _digital_probability_and_delta(
        spot=reference_price,
        strike=float(strategy.parameters["strike_price"]),
        time_years=remaining / (365.25 * 86_400),
        volatility=float(strategy.parameters["annualized_volatility_pct"]) / 100,
        direction=strategy.parameters["event_direction"],
    )
    prediction_book = books[token_ids[0]].snapshot
    fee_bps = float(strategy.risk["paper_fee_bps"])
    max_slippage_bps = float(strategy.risk["max_slippage_bps"])
    min_profit_bps = float(strategy.parameters["min_profit_bps"])
    candidates: list[dict[str, Any]] = []
    for prediction_side in ("buy", "sell"):
        prediction_levels = (
            prediction_book.asks if prediction_side == "buy" else prediction_book.bids
        )
        if not prediction_levels:
            continue
        usdc_budget = common_budget / usdc_rate
        if prediction_side == "buy":
            prediction_quantity = max_base_for_quote(
                prediction_levels,
                usdc_budget / (1 + fee_bps / 10_000),
            )
        else:
            prediction_quantity = min(available_base(prediction_levels), usdc_budget)
        prediction_quantity = min(
            prediction_quantity,
            available_base(prediction_levels),
        )
        minimum = books[token_ids[0]].min_order_size
        if prediction_quantity <= 1e-12 or prediction_quantity + 1e-12 < minimum:
            continue
        hedge_quantity = (
            prediction_quantity
            * abs(binary_delta)
            * float(strategy.parameters["hedge_ratio"])
        )
        if binary_delta >= 0:
            hedge_side = "sell" if prediction_side == "buy" else "buy"
        else:
            hedge_side = "buy" if prediction_side == "buy" else "sell"
        hedge_levels = hedge_book.asks if hedge_side == "buy" else hedge_book.bids
        if hedge_quantity > available_base(hedge_levels):
            continue
        prediction_fill = estimate_fill(
            prediction_levels,
            side=prediction_side,
            quantity_base=prediction_quantity,
            fee_bps=fee_bps,
        )
        hedge_fill = estimate_fill(
            hedge_levels,
            side=hedge_side,
            quantity_base=hedge_quantity,
            fee_bps=fee_bps,
        )
        if prediction_fill is None or hedge_fill is None:
            continue
        quote_rate = quote_rates[_quote_currency(account.symbol)]
        hedge_mid = (hedge_book.bids[0].price + hedge_book.asks[0].price) / 2
        hedge_crossing = (
            max(0.0, hedge_fill.average_price - hedge_mid)
            if hedge_side == "buy"
            else max(0.0, hedge_mid - hedge_fill.average_price)
        )
        hedge_execution_cost = (
            hedge_crossing * hedge_quantity + hedge_fill.fee_quote
        ) * quote_rate
        prediction_unit = (
            probability - prediction_fill.average_price
            if prediction_side == "buy"
            else prediction_fill.average_price - probability
        )
        prediction_edge_common = (
            prediction_unit * prediction_quantity - prediction_fill.fee_quote
        ) * usdc_rate
        model_edge = prediction_edge_common - hedge_execution_cost
        invested_common = max(
            prediction_fill.gross_quote * usdc_rate,
            hedge_fill.gross_quote * quote_rate,
        )
        model_edge_bps = (
            model_edge / invested_common * 10_000 if invested_common > 0 else 0.0
        )
        max_slippage = max(
            _slippage_bps(
                prediction_book,
                prediction_side,
                prediction_fill.average_price,
            ),
            _slippage_bps(hedge_book, hedge_side, hedge_fill.average_price),
        )
        candidates.append(
            {
                "mechanism": "cross_venue_digital_delta_hedge",
                "risk_class": "model_relative_value",
                "model_probability": probability,
                "reference_price_common": reference_price,
                "strike_price_common": float(strategy.parameters["strike_price"]),
                "time_to_resolution_seconds": remaining,
                "annualized_volatility_pct": float(
                    strategy.parameters["annualized_volatility_pct"]
                ),
                "binary_delta": binary_delta,
                "hedge_ratio": float(strategy.parameters["hedge_ratio"]),
                "model_edge_quote": model_edge / usdc_rate,
                "model_edge_common": model_edge,
                "model_edge_bps": model_edge_bps,
                "max_slippage_bps": max_slippage,
                "inventory_required": prediction_side == "sell",
                "qualified": model_edge_bps >= min_profit_bps
                and max_slippage <= max_slippage_bps,
                "legs": [
                    _leg_payload(
                        token_ids[0],
                        prediction_fill,
                        outcome_role="yes_outcome",
                    ),
                    {
                        "venue": account.exchange,
                        "venue_type": (
                            "dex" if account.exchange in DEX_VENUES_BY_ID else "cex"
                        ),
                        "account_id": account.id,
                        "market_type": account.market_type,
                        "symbol": account.symbol,
                        "side": hedge_side,
                        "quantity": hedge_quantity,
                        "average_price": hedge_fill.average_price,
                        "gross_quote": hedge_fill.gross_quote,
                        "fee_quote": hedge_fill.fee_quote,
                        "quote_currency": _quote_currency(account.symbol),
                    },
                ],
                "execution_notes": [
                    "model-relative-value signal; not a guaranteed arbitrage",
                    "resolution source, timestamp and strike semantics must match",
                    "prediction and hedge legs require partial-fill recovery",
                ],
                "live_submit_allowed": False,
            }
        )
    return max(candidates, key=_candidate_rank) if candidates else None


def scan_polymarket_arbitrage(
    strategy: UserStrategy,
    project: UserProject,
    accounts: list[UserExchangeAccount],
    hedge_books: dict[str, OrderBookSnapshot],
    prediction_books: dict[str, PolymarketBook],
    quote_rates: dict[str, float],
    *,
    now: float,
) -> tuple[str, str, str, dict[str, Any], dict[str, Any]]:
    token_ids = strategy.parameters["outcome_asset_ids"]
    required_ids = prediction_token_ids(strategy.parameters)
    missing = [token_id for token_id in required_ids if token_id not in prediction_books]
    if missing:
        reason = "Polymarket order book is unavailable: " + ", ".join(missing[:2])
        return "blocked_market_data", reason, "blocked", {}, {}
    max_age = float(strategy.risk["max_order_book_age_seconds"])
    for token_id in required_ids:
        snapshot = prediction_books[token_id].snapshot
        if not snapshot.bids and not snapshot.asks:
            reason = f"Polymarket order book has no liquidity: {token_id}"
            return "blocked_market_data", reason, "blocked", {}, {}
        received_age = now - float(snapshot.received_at)
        if received_age < -5 or (max_age > 0 and received_age > max_age):
            reason = f"Polymarket order book is stale: {token_id}"
            return "blocked_market_data", reason, "blocked", {}, {}
        if snapshot.timestamp_ms is not None and snapshot.timestamp_ms > 0:
            exchange_age = now - snapshot.timestamp_ms / 1000
            if exchange_age < -30 or (max_age > 0 and exchange_age > max_age):
                reason = f"Polymarket exchange timestamp is stale: {token_id}"
                return "blocked_market_data", reason, "blocked", {}, {}
    usdc_rate = quote_rates.get("USDC")
    project_rate = quote_rates.get(project.quote_currency)
    if usdc_rate is None or project_rate is None:
        reason = "USDC or project quote conversion rate is unavailable"
        return "blocked_quote_rate", reason, "blocked", {}, {}
    common_budget = float(strategy.parameters["max_cycle_quote"]) * project_rate
    usdc_budget = common_budget / usdc_rate
    mechanism = strategy.parameters["mechanism"]
    observations: list[dict[str, Any]] = []
    if mechanism in {"auto", "complete_set"}:
        observations.extend(
            _complete_set_candidates(
                strategy,
                prediction_books,
                token_ids,
                usdc_budget=usdc_budget,
            )
        )
    if mechanism in {"auto", "neg_risk"}:
        observations.extend(
            _neg_risk_candidates(
                strategy,
                prediction_books,
                token_ids,
                strategy.parameters["neg_risk_no_asset_ids"],
                usdc_budget=usdc_budget,
            )
        )
    if mechanism in {"auto", "cross_venue"}:
        cross_candidate = _cross_venue_candidate(
            strategy,
            accounts,
            hedge_books,
            prediction_books,
            quote_rates,
            usdc_rate=usdc_rate,
            common_budget=common_budget,
            now=now,
        )
        if cross_candidate is not None:
            observations.append(cross_candidate)
    ranked = sorted(observations, key=_candidate_rank, reverse=True)
    qualified = [row for row in ranked if row.get("qualified")]
    best = ranked[0] if ranked else None
    scan = {
        "mechanism": mechanism,
        "event_group_id": strategy.parameters["event_group_id"],
        "candidate_count": len(qualified),
        "observation_count": len(observations),
        "best": best,
        "token_count": len(required_ids),
        "max_cycle_quote": float(strategy.parameters["max_cycle_quote"]),
        "project_quote_currency": project.quote_currency,
        "common_notional": common_budget,
        "paper_scan_only": True,
        "live_submit_allowed": False,
    }
    if not qualified:
        if best is None:
            reason = "no compatible Polymarket structure or cross-venue model is available"
        elif float(best.get("max_slippage_bps") or 0.0) > float(
            strategy.risk["max_slippage_bps"]
        ):
            reason = "Polymarket candidate exceeds max slippage"
        else:
            edge = float(best.get("profit_bps") or best.get("model_edge_bps") or 0.0)
            reason = (
                f"best Polymarket edge {edge:.2f} bps is below "
                f"{float(strategy.parameters['min_profit_bps']):.2f} bps"
            )
        return "waiting", reason, "waiting", best or {}, scan
    selected = qualified[0]
    if selected.get("manual_review_required"):
        return (
            "review_required",
            "augmented Neg-Risk candidate requires manual Other-outcome review",
            "attention",
            selected,
            scan,
        )
    edge = float(selected.get("profit_bps") or selected.get("model_edge_bps") or 0.0)
    return (
        "candidate",
        f"Polymarket {selected['mechanism']} candidate: {edge:.2f} bps",
        "candidate",
        selected,
        scan,
    )
