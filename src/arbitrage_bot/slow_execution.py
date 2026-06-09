from __future__ import annotations

import random
from dataclasses import dataclass
from time import time
from typing import Any, Callable

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
    order_ttl_seconds: float
    slice_base: float
    slice_base_min: float
    slice_base_max: float
    slice_quote: float
    randomize_slice: bool
    stop_price: float
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
            "order_ttl_seconds": self.order_ttl_seconds,
            "slice_base": self.slice_base,
            "slice_base_min": self.slice_base_min,
            "slice_base_max": self.slice_base_max,
            "slice_quote": self.slice_quote,
            "randomize_slice": self.randomize_slice,
            "stop_price": self.stop_price,
            "order": None if self.order is None else self.order.to_dict(),
            "status": self.status,
            "observed_at": self.observed_at,
        }


def _validate_side(side: str) -> Side:
    if side not in {"buy", "sell"}:
        raise ValueError("slow_execution.side must be buy or sell")
    return side  # type: ignore[return-value]


def _range_slice_base(
    cfg: SlowExecutionConfig,
    random_value: float,
) -> float | None:
    if cfg.slice_base_min <= 0 and cfg.slice_base_max <= 0:
        return None
    if cfg.slice_base_min <= 0 or cfg.slice_base_max <= 0:
        raise ValueError(
            "slow_execution.slice_base_min and slice_base_max must both be positive"
        )
    if cfg.slice_base_max < cfg.slice_base_min:
        raise ValueError(
            "slow_execution.slice_base_max must be greater than or equal to slice_base_min"
        )
    if cfg.randomize_slice:
        return cfg.slice_base_min + (cfg.slice_base_max - cfg.slice_base_min) * random_value
    return cfg.slice_base_min


def _configured_slice_base(
    cfg: SlowExecutionConfig,
    mid_price: float,
    random_value: float,
) -> float:
    range_slice = _range_slice_base(cfg, random_value)
    slice_sources = [
        cfg.slice_base > 0,
        cfg.slice_quote > 0,
        range_slice is not None,
    ]
    if sum(1 for item in slice_sources if item) != 1:
        raise ValueError(
            "configure exactly one of slow_execution.slice_base, "
            "slice_quote, or slice_base_min/slice_base_max"
        )
    if range_slice is not None:
        return range_slice
    if cfg.slice_base > 0:
        return cfg.slice_base
    return cfg.slice_quote / mid_price


def _is_stopped_by_price(
    side: Side,
    mid_price: float,
    stop_price: float,
) -> bool:
    if stop_price <= 0:
        return False
    if side == "sell":
        return mid_price <= stop_price
    return mid_price >= stop_price


def build_slow_execution_plan(
    book: OrderBookSnapshot,
    cfg: SlowExecutionConfig,
    *,
    submitted_base: float = 0.0,
    random_fn: Callable[[], float] | None = None,
) -> SlowExecutionPlan:
    side = _validate_side(cfg.side)
    if cfg.total_base <= 0:
        raise ValueError("slow_execution.total_base must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("slow_execution.interval_seconds must be positive")
    if cfg.order_ttl_seconds < 0:
        raise ValueError("slow_execution.order_ttl_seconds must be non-negative")
    if not book.bids or not book.asks:
        raise ValueError("order book must have both bid and ask levels")

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("order book top bid/ask are not usable")

    mid_price = (best_bid + best_ask) / 2
    existing_spread_bps = (best_ask - best_bid) / mid_price * 10_000
    selected_slice_base = _configured_slice_base(
        cfg,
        mid_price,
        (random_fn or random.random)(),
    )
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
            order_ttl_seconds=cfg.order_ttl_seconds,
            slice_base=cfg.slice_base,
            slice_base_min=cfg.slice_base_min,
            slice_base_max=cfg.slice_base_max,
            slice_quote=cfg.slice_quote,
            randomize_slice=cfg.randomize_slice,
            stop_price=cfg.stop_price,
            order=None,
            status="complete",
            observed_at=time(),
        )

    if _is_stopped_by_price(side, mid_price, cfg.stop_price):
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
            order_ttl_seconds=cfg.order_ttl_seconds,
            slice_base=cfg.slice_base,
            slice_base_min=cfg.slice_base_min,
            slice_base_max=cfg.slice_base_max,
            slice_quote=cfg.slice_quote,
            randomize_slice=cfg.randomize_slice,
            stop_price=cfg.stop_price,
            order=None,
            status="stopped_by_price",
            observed_at=time(),
        )

    order_base = min(remaining_base, selected_slice_base)
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
            order_ttl_seconds=cfg.order_ttl_seconds,
            slice_base=cfg.slice_base,
            slice_base_min=cfg.slice_base_min,
            slice_base_max=cfg.slice_base_max,
            slice_quote=cfg.slice_quote,
            randomize_slice=cfg.randomize_slice,
            stop_price=cfg.stop_price,
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
        order_ttl_seconds=cfg.order_ttl_seconds,
        slice_base=cfg.slice_base,
        slice_base_min=cfg.slice_base_min,
        slice_base_max=cfg.slice_base_max,
        slice_quote=cfg.slice_quote,
        randomize_slice=cfg.randomize_slice,
        stop_price=cfg.stop_price,
        order=order,
        status="planned",
        observed_at=time(),
    )
