from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from arbitrage_bot.config import (
    ExchangeConfig,
    OptionComboConfig,
    OptionsArbitrageConfig,
)
from arbitrage_bot.models import BookLevel, FillEstimate, Opportunity, OpportunityLeg, OrderBookSnapshot
from arbitrage_bot.orderbook import estimate_fill, max_base_for_quote


@dataclass(frozen=True)
class OptionFill:
    side: str
    contracts: float
    underlying_base: float
    average_price: float
    gross_quote: float
    fee_quote: float

    @property
    def net_quote(self) -> float:
        if self.side == "buy":
            return self.gross_quote + self.fee_quote
        return self.gross_quote - self.fee_quote


def _quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0].upper()


def _days_to_expiry(expiry: str, *, now: float | None = None) -> float | None:
    if not expiry:
        return None
    value = expiry.strip()
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            expires_at = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now_ts = time.time() if now is None else now
    return (expires_at.timestamp() - now_ts) / 86_400


def _discounted_strike(combo: OptionComboConfig, cfg: OptionsArbitrageConfig) -> float:
    days = _days_to_expiry(combo.expiry)
    if days is None:
        days = 0.0
    annual_rate = (cfg.risk_free_rate_bps - cfg.borrow_rate_bps) / 10_000
    return combo.strike * math.exp(-annual_rate * max(days, 0.0) / 365)


def _effective_option_levels(
    levels: list[BookLevel],
    *,
    contract_size: float,
) -> list[BookLevel]:
    return [
        BookLevel(price=level.price, amount=level.amount * contract_size)
        for level in levels
    ]


def _option_capacity_underlying(
    levels: list[BookLevel],
    *,
    contract_size: float,
) -> float:
    return sum(max(0.0, level.amount) for level in levels) * contract_size


def _option_fill(
    levels: list[BookLevel],
    *,
    side: str,
    underlying_base: float,
    contract_size: float,
    fee_bps: float,
) -> OptionFill | None:
    fill = estimate_fill(
        _effective_option_levels(levels, contract_size=contract_size),
        side=side,  # type: ignore[arg-type]
        quantity_base=underlying_base,
        fee_bps=fee_bps,
    )
    if fill is None:
        return None
    return OptionFill(
        side=side,
        contracts=fill.quantity_base / contract_size,
        underlying_base=fill.quantity_base,
        average_price=fill.average_price,
        gross_quote=fill.gross_quote,
        fee_quote=fill.fee_quote,
    )


def _spot_fill(
    levels: list[BookLevel],
    *,
    side: str,
    underlying_base: float,
    fee_bps: float,
) -> FillEstimate | None:
    return estimate_fill(
        levels,
        side=side,  # type: ignore[arg-type]
        quantity_base=underlying_base,
        fee_bps=fee_bps,
    )


def _exchange_by_key(exchanges: list[ExchangeConfig]) -> dict[str, ExchangeConfig]:
    return {exchange.key: exchange for exchange in exchanges}


def _best_bid_ask(book: OrderBookSnapshot) -> tuple[float, float] | None:
    if not book.bids or not book.asks:
        return None
    bid = book.bids[0].price
    ask = book.asks[0].price
    if bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return bid, ask


def _spread_bps(book: OrderBookSnapshot) -> float | None:
    best = _best_bid_ask(book)
    if best is None:
        return None
    bid, ask = best
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 10_000.0 if mid > 0 else None


def _depth_quote(levels: list[BookLevel]) -> float:
    return sum(max(0.0, level.price) * max(0.0, level.amount) for level in levels)


def _min_two_sided_depth_quote(book: OrderBookSnapshot) -> float:
    return min(_depth_quote(book.bids), _depth_quote(book.asks))


def _passes_option_liquidity(
    book: OrderBookSnapshot,
    cfg: OptionsArbitrageConfig,
) -> bool:
    spread = _spread_bps(book)
    if spread is None:
        return False
    if cfg.max_option_spread_bps > 0 and spread > cfg.max_option_spread_bps:
        return False
    if (
        cfg.min_option_depth_quote > 0
        and _min_two_sided_depth_quote(book) < cfg.min_option_depth_quote
    ):
        return False
    return True


def _passes_thresholds(
    *,
    edge_quote: float,
    capital_quote: float,
    cfg: OptionsArbitrageConfig,
) -> tuple[bool, float]:
    edge_bps = edge_quote / capital_quote * 10_000 if capital_quote > 0 else 0.0
    return (
        edge_quote >= cfg.min_edge_quote and edge_bps >= cfg.min_edge_bps,
        edge_bps,
    )


def _option_leg(
    *,
    exchange: str,
    symbol: str,
    side: str,
    fill: OptionFill,
    quote_currency: str,
) -> OpportunityLeg:
    return OpportunityLeg(
        exchange=exchange,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity_base=fill.contracts,
        average_price=fill.average_price,
        fee_quote=fill.fee_quote,
        quote_currency=quote_currency,
        gross_quote=fill.gross_quote,
        net_quote=fill.net_quote,
    )


def _spot_leg(
    *,
    exchange: str,
    symbol: str,
    side: str,
    fill: FillEstimate,
    quote_currency: str,
) -> OpportunityLeg:
    return OpportunityLeg(
        exchange=exchange,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity_base=fill.quantity_base,
        average_price=fill.average_price,
        fee_quote=fill.fee_quote,
        quote_currency=quote_currency,
        gross_quote=fill.gross_quote,
        net_quote=fill.net_quote,
    )


def find_options_arbitrage_opportunities(
    spot_books: dict[tuple[str, str], OrderBookSnapshot],
    option_books: dict[tuple[str, str], OrderBookSnapshot],
    spot_exchanges: list[ExchangeConfig],
    option_exchanges: list[ExchangeConfig],
    combos: list[OptionComboConfig],
    cfg: OptionsArbitrageConfig,
) -> list[Opportunity]:
    spot_by_key = _exchange_by_key(spot_exchanges)
    option_by_key = _exchange_by_key(option_exchanges)
    opportunities: list[Opportunity] = []

    for combo in combos:
        if combo.strike <= 0 or combo.contract_size <= 0:
            continue
        days_to_expiry = _days_to_expiry(combo.expiry)
        if days_to_expiry is not None:
            if days_to_expiry < 0:
                continue
            if cfg.max_days_to_expiry > 0 and days_to_expiry > cfg.max_days_to_expiry:
                continue

        spot_cfg = spot_by_key.get(combo.spot_exchange)
        option_cfg = option_by_key.get(combo.option_exchange)
        spot_book = spot_books.get((combo.spot_exchange, combo.spot_symbol))
        call_book = option_books.get((combo.option_exchange, combo.call_symbol))
        put_book = option_books.get((combo.option_exchange, combo.put_symbol))
        if (
            spot_cfg is None
            or option_cfg is None
            or spot_book is None
            or call_book is None
            or put_book is None
        ):
            continue
        if _best_bid_ask(spot_book) is None or _best_bid_ask(call_book) is None or _best_bid_ask(put_book) is None:
            continue
        if not (
            _passes_option_liquidity(call_book, cfg)
            and _passes_option_liquidity(put_book, cfg)
        ):
            continue

        spot_bid, spot_ask = _best_bid_ask(spot_book) or (0.0, 0.0)
        call_bid, call_ask = _best_bid_ask(call_book) or (0.0, 0.0)
        put_bid, put_ask = _best_bid_ask(put_book) or (0.0, 0.0)
        call_spread_bps = _spread_bps(call_book)
        put_spread_bps = _spread_bps(put_book)
        call_depth_quote = _min_two_sided_depth_quote(call_book)
        put_depth_quote = _min_two_sided_depth_quote(put_book)
        discounted_strike = _discounted_strike(combo, cfg)
        max_underlying_from_contracts = (
            cfg.max_contracts * combo.contract_size if cfg.max_contracts > 0 else math.inf
        )

        conversion_qty = min(
            max_base_for_quote(spot_book.asks, cfg.notional_quote),
            _option_capacity_underlying(call_book.bids, contract_size=combo.contract_size),
            _option_capacity_underlying(put_book.asks, contract_size=combo.contract_size),
            max_underlying_from_contracts,
        )
        if conversion_qty > 0:
            spot_buy = _spot_fill(
                spot_book.asks,
                side="buy",
                underlying_base=conversion_qty,
                fee_bps=spot_cfg.fee_bps,
            )
            call_sell = _option_fill(
                call_book.bids,
                side="sell",
                underlying_base=conversion_qty,
                contract_size=combo.contract_size,
                fee_bps=option_cfg.fee_bps,
            )
            put_buy = _option_fill(
                put_book.asks,
                side="buy",
                underlying_base=conversion_qty,
                contract_size=combo.contract_size,
                fee_bps=option_cfg.fee_bps,
            )
            if spot_buy and call_sell and put_buy:
                expiry_cash_value = conversion_qty * discounted_strike
                edge_quote = (
                    call_sell.net_quote
                    - put_buy.net_quote
                    - spot_buy.net_quote
                    + expiry_cash_value
                )
                capital_quote = spot_buy.net_quote + put_buy.net_quote
                passes, edge_bps = _passes_thresholds(
                    edge_quote=edge_quote,
                    capital_quote=capital_quote,
                    cfg=cfg,
                )
                if passes:
                    opportunities.append(
                        Opportunity(
                            strategy="options-arbitrage",
                            profit_quote=edge_quote,
                            profit_bps=edge_bps,
                            legs=[
                                _option_leg(
                                    exchange=combo.option_exchange,
                                    symbol=combo.call_symbol,
                                    side="sell",
                                    fill=call_sell,
                                    quote_currency=combo.quote_currency,
                                ),
                                _option_leg(
                                    exchange=combo.option_exchange,
                                    symbol=combo.put_symbol,
                                    side="buy",
                                    fill=put_buy,
                                    quote_currency=combo.quote_currency,
                                ),
                                _spot_leg(
                                    exchange=combo.spot_exchange,
                                    symbol=combo.spot_symbol,
                                    side="buy",
                                    fill=spot_buy,
                                    quote_currency=_quote_currency(combo.spot_symbol),
                                ),
                            ],
                            metadata={
                                "underlying": combo.underlying,
                                "direction": "conversion",
                                "strike": combo.strike,
                                "discounted_strike": discounted_strike,
                                "expiry": combo.expiry,
                                "days_to_expiry": days_to_expiry,
                                "contract_size": combo.contract_size,
                                "contracts": call_sell.contracts,
                                "synthetic_forward_bid": call_bid - put_ask + combo.strike,
                                "spot_reference": spot_ask,
                                "option_call_spread_bps": call_spread_bps,
                                "option_put_spread_bps": put_spread_bps,
                                "option_call_depth_quote": call_depth_quote,
                                "option_put_depth_quote": put_depth_quote,
                                "requires_option_assignment_controls": True,
                                "requires_margin_controls": True,
                            },
                        )
                    )

        reverse_qty = min(
            max_base_for_quote(spot_book.bids, cfg.notional_quote),
            _option_capacity_underlying(call_book.asks, contract_size=combo.contract_size),
            _option_capacity_underlying(put_book.bids, contract_size=combo.contract_size),
            max_underlying_from_contracts,
        )
        if reverse_qty > 0:
            spot_sell = _spot_fill(
                spot_book.bids,
                side="sell",
                underlying_base=reverse_qty,
                fee_bps=spot_cfg.fee_bps,
            )
            call_buy = _option_fill(
                call_book.asks,
                side="buy",
                underlying_base=reverse_qty,
                contract_size=combo.contract_size,
                fee_bps=option_cfg.fee_bps,
            )
            put_sell = _option_fill(
                put_book.bids,
                side="sell",
                underlying_base=reverse_qty,
                contract_size=combo.contract_size,
                fee_bps=option_cfg.fee_bps,
            )
            if spot_sell and call_buy and put_sell:
                expiry_cash_cost = reverse_qty * discounted_strike
                edge_quote = (
                    put_sell.net_quote
                    + spot_sell.net_quote
                    - call_buy.net_quote
                    - expiry_cash_cost
                )
                capital_quote = max(call_buy.net_quote, 0.0) + expiry_cash_cost
                passes, edge_bps = _passes_thresholds(
                    edge_quote=edge_quote,
                    capital_quote=capital_quote,
                    cfg=cfg,
                )
                if passes:
                    opportunities.append(
                        Opportunity(
                            strategy="options-arbitrage",
                            profit_quote=edge_quote,
                            profit_bps=edge_bps,
                            legs=[
                                _option_leg(
                                    exchange=combo.option_exchange,
                                    symbol=combo.call_symbol,
                                    side="buy",
                                    fill=call_buy,
                                    quote_currency=combo.quote_currency,
                                ),
                                _option_leg(
                                    exchange=combo.option_exchange,
                                    symbol=combo.put_symbol,
                                    side="sell",
                                    fill=put_sell,
                                    quote_currency=combo.quote_currency,
                                ),
                                _spot_leg(
                                    exchange=combo.spot_exchange,
                                    symbol=combo.spot_symbol,
                                    side="sell",
                                    fill=spot_sell,
                                    quote_currency=_quote_currency(combo.spot_symbol),
                                ),
                            ],
                            metadata={
                                "underlying": combo.underlying,
                                "direction": "reverse_conversion",
                                "strike": combo.strike,
                                "discounted_strike": discounted_strike,
                                "expiry": combo.expiry,
                                "days_to_expiry": days_to_expiry,
                                "contract_size": combo.contract_size,
                                "contracts": call_buy.contracts,
                                "synthetic_forward_ask": call_ask - put_bid + combo.strike,
                                "spot_reference": spot_bid,
                                "option_call_spread_bps": call_spread_bps,
                                "option_put_spread_bps": put_spread_bps,
                                "option_call_depth_quote": call_depth_quote,
                                "option_put_depth_quote": put_depth_quote,
                                "requires_option_assignment_controls": True,
                                "requires_margin_controls": True,
                            },
                        )
                    )

    opportunities.sort(key=lambda item: item.profit_bps, reverse=True)
    return opportunities
