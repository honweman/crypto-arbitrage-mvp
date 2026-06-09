from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any

from .config import SlowExecutionConfig
from .models import OrderBookSnapshot, Side


@dataclass(frozen=True)
class SlowExecutionOrder:
    side: Side
    price: float
    amount: float
    quote_notional: float
    submitted_base_before: float
    submitted_base_after: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "submitted_base_before": self.submitted_base_before,
            "submitted_base_after": self.submitted_base_after,
        }


@dataclass(frozen=True)
class SlowExecutionPlan:
    exchange: str
    symbol: str
    side: Side
    best_bid: float
    best_ask: float
    mid_price: float
    existing_spread_bps: float
    total_base: float
    submitted_base: float
    remaining_base: float
    interval_seconds: float
    slice_base: float
    slice_quote: float
    order: SlowExecutionOrder | None
    status: str
    observed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "existing_spread_bps": self.existing_spread_bps,
            "total_base": self.total_base,
            "submitted_base": self.submitted_base,
            "remaining_base": self.remaining_base,
            "interval_seconds": self.interval_seconds,
            "slice_base": self.slice_base,
            "slice_quote": self.slice_quote,
            "order": None if self.order is None else self.order.to_dict(),
            "status": self.status,
            "observed_at": self.observed_at,
        }


def _validate_side(side: str) -> Side:
    if side not in {"buy", "sell"}:
        raise ValueError("slow_execution.side must be buy or sell")
    return side  # type: ignore[return-value]


def build_slow_execution_plan(
    book: OrderBookSnapshot,
    cfg: SlowExecutionConfig,
    *,
    submitted_base: float = 0.0,
) -> SlowExecutionPlan:
    side = _validate_side(cfg.side)
    if cfg.total_base <= 0:
        raise ValueError("slow_execution.total_base must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("slow_execution.interval_seconds must be positive")
    if cfg.slice_base <= 0 and cfg.slice_quote <= 0:
        raise ValueError("slow_execution.slice_base or slice_quote must be positive")
    if cfg.slice_base > 0 and cfg.slice_quote > 0:
        raise ValueError("configure only one of slow_execution.slice_base or slice_quote")
    if not book.bids or not book.asks:
        raise ValueError("order book must have both bid and ask levels")

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("order book top bid/ask are not usable")

    mid_price = (best_bid + best_ask) / 2
    existing_spread_bps = (best_ask - best_bid) / mid_price * 10_000
    safe_submitted = min(max(0.0, submitted_base), cfg.total_base)
    remaining_base = max(0.0, cfg.total_base - safe_submitted)

    if remaining_base <= 0:
        return SlowExecutionPlan(
            exchange=cfg.exchange,
            symbol=cfg.symbol,
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            existing_spread_bps=existing_spread_bps,
            total_base=cfg.total_base,
            submitted_base=safe_submitted,
            remaining_base=0.0,
            interval_seconds=cfg.interval_seconds,
            slice_base=cfg.slice_base,
            slice_quote=cfg.slice_quote,
            order=None,
            status="complete",
            observed_at=time(),
        )

    configured_slice_base = cfg.slice_base if cfg.slice_base > 0 else cfg.slice_quote / mid_price
    order_base = min(remaining_base, configured_slice_base)
    quote_notional = order_base * mid_price
    if quote_notional < cfg.min_order_quote:
        return SlowExecutionPlan(
            exchange=cfg.exchange,
            symbol=cfg.symbol,
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            existing_spread_bps=existing_spread_bps,
            total_base=cfg.total_base,
            submitted_base=safe_submitted,
            remaining_base=remaining_base,
            interval_seconds=cfg.interval_seconds,
            slice_base=cfg.slice_base,
            slice_quote=cfg.slice_quote,
            order=None,
            status="below_min_order_quote",
            observed_at=time(),
        )

    order = SlowExecutionOrder(
        side=side,
        price=mid_price,
        amount=order_base,
        quote_notional=quote_notional,
        submitted_base_before=safe_submitted,
        submitted_base_after=safe_submitted + order_base,
    )
    return SlowExecutionPlan(
        exchange=cfg.exchange,
        symbol=cfg.symbol,
        side=side,
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        existing_spread_bps=existing_spread_bps,
        total_base=cfg.total_base,
        submitted_base=safe_submitted,
        remaining_base=remaining_base,
        interval_seconds=cfg.interval_seconds,
        slice_base=cfg.slice_base,
        slice_quote=cfg.slice_quote,
        order=order,
        status="planned",
        observed_at=time(),
    )
