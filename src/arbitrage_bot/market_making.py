from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

from .config import MarketMakerConfig
from .models import OrderBookSnapshot, Side
from .orderbook_metrics import order_book_metric_snapshot


@dataclass(frozen=True)
class MarketMakerOrder:
    side: Side
    level: int
    price: float
    amount: float
    quote_notional: float
    distance_bps: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "level": self.level,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "distance_bps": self.distance_bps,
        }


@dataclass(frozen=True)
class MarketMakerPlan:
    exchange: str
    symbol: str
    quote_currency: str
    best_bid: float
    best_ask: float
    mid_price: float
    existing_spread_bps: float
    price_band_pct: float
    levels: int
    quote_per_level: float
    bid_depth_quote: float = 0.0
    ask_depth_quote: float = 0.0
    max_level_gap_bps: float = 0.0
    order_book_timestamp_ms: int | None = None
    orders: list[MarketMakerOrder] = field(default_factory=list)
    observed_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "quote_currency": self.quote_currency,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "existing_spread_bps": self.existing_spread_bps,
            "price_band_pct": self.price_band_pct,
            "levels": self.levels,
            "quote_per_level": self.quote_per_level,
            "bid_depth_quote": self.bid_depth_quote,
            "ask_depth_quote": self.ask_depth_quote,
            "max_level_gap_bps": self.max_level_gap_bps,
            "order_book_timestamp_ms": self.order_book_timestamp_ms,
            "orders": [order.to_dict() for order in self.orders],
            "observed_at": self.observed_at,
        }


def build_symmetric_market_maker_plan(
    book: OrderBookSnapshot,
    cfg: MarketMakerConfig,
) -> MarketMakerPlan:
    if cfg.levels <= 0:
        raise ValueError("market maker levels must be positive")
    if cfg.price_band_pct <= 0:
        raise ValueError("market maker price_band_pct must be positive")
    if cfg.quote_per_level <= 0:
        raise ValueError("market maker quote_per_level must be positive")
    if not book.bids or not book.asks:
        raise ValueError("order book must have both bid and ask levels")

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("order book top bid/ask are not usable")

    mid_price = (best_bid + best_ask) / 2
    existing_spread_bps = (best_ask - best_bid) / mid_price * 10_000
    metrics = order_book_metric_snapshot(book)
    step_pct = cfg.price_band_pct / cfg.levels / 100
    orders: list[MarketMakerOrder] = []

    for level in range(1, cfg.levels + 1):
        distance_pct = step_pct * level
        distance_bps = distance_pct * 10_000
        if distance_bps < cfg.min_distance_bps:
            continue

        bid_price = mid_price * (1 - distance_pct)
        ask_price = mid_price * (1 + distance_pct)
        for side, price in (("buy", bid_price), ("sell", ask_price)):
            quote_notional = cfg.quote_per_level
            if quote_notional < cfg.min_order_quote:
                continue
            orders.append(
                MarketMakerOrder(
                    side=side,
                    level=level,
                    price=price,
                    amount=quote_notional / price,
                    quote_notional=quote_notional,
                    distance_bps=distance_bps,
                )
            )

    return MarketMakerPlan(
        exchange=cfg.exchange,
        symbol=cfg.symbol,
        quote_currency=cfg.symbol.split("/", 1)[1].upper()
        if "/" in cfg.symbol
        else "",
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        existing_spread_bps=existing_spread_bps,
        price_band_pct=cfg.price_band_pct,
        levels=cfg.levels,
        quote_per_level=cfg.quote_per_level,
        bid_depth_quote=float(metrics["bid_depth_quote"] or 0.0),
        ask_depth_quote=float(metrics["ask_depth_quote"] or 0.0),
        max_level_gap_bps=float(metrics["max_level_gap_bps"] or 0.0),
        order_book_timestamp_ms=(
            int(metrics["order_book_timestamp_ms"])
            if metrics["order_book_timestamp_ms"] is not None
            else None
        ),
        orders=orders,
    )
