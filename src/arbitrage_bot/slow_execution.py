from __future__ import annotations

import random
from dataclasses import dataclass
from time import time
from typing import Any, Callable

from .config import SlowExecutionConfig
from .models import OrderBookSnapshot, Side
from .orderbook_metrics import order_book_metric_snapshot


@dataclass(frozen=True)
class SlowExecutionOrder:
    side: Side
    price: float
    amount: float
    quote_notional: float
    submitted_base_before: float
    submitted_base_after: float
    submitted_quote_before: float
    submitted_quote_after: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "submitted_base_before": self.submitted_base_before,
            "submitted_base_after": self.submitted_base_after,
            "submitted_quote_before": self.submitted_quote_before,
            "submitted_quote_after": self.submitted_quote_after,
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
    total_quote: float
    submitted_quote: float
    remaining_quote: float
    progress_mode: str
    unlimited_total: bool
    interval_seconds: float
    order_ttl_seconds: float
    slice_mode: str
    slice_base: float
    slice_base_min: float
    slice_base_max: float
    slice_quote: float
    randomize_slice: bool
    start_price: float
    stop_price: float
    price_mode: str
    price_offset_bps: float
    trigger_price: float
    order: SlowExecutionOrder | None
    status: str
    observed_at: float
    bid_depth_quote: float = 0.0
    ask_depth_quote: float = 0.0
    max_level_gap_bps: float = 0.0
    order_book_timestamp_ms: int | None = None
    order_book_received_at: float | None = None

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
            "remaining_base": None if self.unlimited_total else self.remaining_base,
            "total_quote": self.total_quote,
            "submitted_quote": self.submitted_quote,
            "remaining_quote": None if self.unlimited_total else self.remaining_quote,
            "progress_mode": self.progress_mode,
            "unlimited_total": self.unlimited_total,
            "interval_seconds": self.interval_seconds,
            "order_ttl_seconds": self.order_ttl_seconds,
            "slice_mode": self.slice_mode,
            "slice_base": self.slice_base,
            "slice_base_min": self.slice_base_min,
            "slice_base_max": self.slice_base_max,
            "slice_quote": self.slice_quote,
            "randomize_slice": self.randomize_slice,
            "start_price": self.start_price,
            "stop_price": self.stop_price,
            "price_mode": self.price_mode,
            "price_offset_bps": self.price_offset_bps,
            "trigger_price": self.trigger_price,
            "order": None if self.order is None else self.order.to_dict(),
            "status": self.status,
            "observed_at": self.observed_at,
            "bid_depth_quote": self.bid_depth_quote,
            "ask_depth_quote": self.ask_depth_quote,
            "max_level_gap_bps": self.max_level_gap_bps,
            "order_book_timestamp_ms": self.order_book_timestamp_ms,
            "order_book_received_at": self.order_book_received_at,
        }


def _validate_side(side: str) -> Side:
    if side not in {"buy", "sell"}:
        raise ValueError("slow_execution.side must be buy or sell")
    return side  # type: ignore[return-value]


def _validate_price_mode(price_mode: str) -> str:
    normalized = (price_mode or "taker").lower()
    if normalized not in {"taker", "maker"}:
        raise ValueError("slow_execution.price_mode must be taker or maker")
    return normalized


def _validate_slice_mode(slice_mode: str) -> str:
    normalized = (slice_mode or "configured").lower()
    if normalized not in {"configured", "top_level"}:
        raise ValueError("slow_execution.slice_mode must be configured or top_level")
    return normalized


def _execution_price(
    *,
    side: Side,
    price_mode: str,
    price_offset_bps: float,
    best_bid: float,
    best_ask: float,
) -> float:
    if price_mode == "maker":
        price = best_bid if side == "buy" else best_ask
    else:
        price = best_ask if side == "buy" else best_bid
    if price_offset_bps <= 0:
        return price
    offset = price_offset_bps / 10_000
    if side == "buy":
        price *= 1 - offset
    else:
        price *= 1 + offset
    return price


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


def _selected_slice_base(
    cfg: SlowExecutionConfig,
    order_price: float,
    random_value: float,
    *,
    side: Side,
    best_bid_amount: float,
    best_ask_amount: float,
) -> float:
    slice_mode = _validate_slice_mode(cfg.slice_mode)
    if slice_mode == "top_level":
        amount = best_ask_amount if side == "sell" else best_bid_amount
        if amount <= 0:
            raise ValueError("top order book level amount must be positive")
        return amount

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
    return cfg.slice_quote / order_price


def _is_stopped_by_price(
    side: Side,
    order_price: float,
    stop_price: float,
) -> bool:
    if stop_price <= 0:
        return False
    if side == "sell":
        return order_price <= stop_price
    return order_price <= stop_price


def _is_waiting_for_start_price(
    side: Side,
    order_price: float,
    start_price: float,
) -> bool:
    if start_price <= 0:
        return False
    if side == "sell":
        return order_price < start_price
    return order_price > start_price


def build_slow_execution_plan(
    book: OrderBookSnapshot,
    cfg: SlowExecutionConfig,
    *,
    submitted_base: float = 0.0,
    submitted_quote: float = 0.0,
    start_price_triggered: bool = False,
    random_fn: Callable[[], float] | None = None,
) -> SlowExecutionPlan:
    side = _validate_side(cfg.side)
    price_mode = _validate_price_mode(cfg.price_mode)
    slice_mode = _validate_slice_mode(cfg.slice_mode)
    if cfg.total_base < 0 or cfg.total_quote < 0:
        raise ValueError("slow_execution.total_base and total_quote must be non-negative")
    if not cfg.unlimited_total and cfg.total_base <= 0 and cfg.total_quote <= 0:
        raise ValueError("slow_execution.total_base or total_quote must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("slow_execution.interval_seconds must be positive")
    if cfg.order_ttl_seconds < 0:
        raise ValueError("slow_execution.order_ttl_seconds must be non-negative")
    if cfg.price_offset_bps < 0:
        raise ValueError("slow_execution.price_offset_bps must be non-negative")
    if side == "buy" and cfg.price_offset_bps >= 10_000:
        raise ValueError("slow_execution.price_offset_bps is too large for buy orders")
    if not book.bids or not book.asks:
        raise ValueError("order book must have both bid and ask levels")

    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    best_bid_amount = book.bids[0].amount
    best_ask_amount = book.asks[0].amount
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("order book top bid/ask are not usable")
    if best_bid_amount <= 0 or best_ask_amount <= 0:
        raise ValueError("order book top bid/ask amounts are not usable")

    mid_price = (best_bid + best_ask) / 2
    existing_spread_bps = (best_ask - best_bid) / mid_price * 10_000
    trigger_price = best_ask if side == "buy" else best_bid
    order_price = _execution_price(
        side=side,
        price_mode=price_mode,
        price_offset_bps=cfg.price_offset_bps,
        best_bid=best_bid,
        best_ask=best_ask,
    )
    if order_price <= 0:
        raise ValueError("slow_execution order price is not usable")
    metrics = order_book_metric_snapshot(book)
    metric_kwargs = {
        "bid_depth_quote": float(metrics["bid_depth_quote"] or 0.0),
        "ask_depth_quote": float(metrics["ask_depth_quote"] or 0.0),
        "max_level_gap_bps": float(metrics["max_level_gap_bps"] or 0.0),
        "order_book_timestamp_ms": (
            int(metrics["order_book_timestamp_ms"])
            if metrics["order_book_timestamp_ms"] is not None
            else None
        ),
        "order_book_received_at": (
            float(metrics["order_book_received_at"])
            if metrics["order_book_received_at"] is not None
            else None
        ),
    }
    selected_slice_base = _selected_slice_base(
        cfg,
        order_price,
        (random_fn or random.random)(),
        side=side,
        best_bid_amount=best_bid_amount,
        best_ask_amount=best_ask_amount,
    )
    base_target_enabled = not cfg.unlimited_total and cfg.total_base > 0
    quote_target_enabled = not cfg.unlimited_total and cfg.total_quote > 0
    safe_submitted_base = (
        min(max(0.0, submitted_base), cfg.total_base)
        if base_target_enabled
        else max(0.0, submitted_base)
    )
    safe_submitted_quote = (
        min(max(0.0, submitted_quote), cfg.total_quote)
        if quote_target_enabled
        else max(0.0, submitted_quote)
    )
    remaining_base_cap = (
        max(0.0, cfg.total_base - safe_submitted_base)
        if base_target_enabled
        else float("inf")
    )
    remaining_quote_cap = (
        max(0.0, cfg.total_quote - safe_submitted_quote)
        if quote_target_enabled
        else float("inf")
    )
    remaining_base = (
        remaining_base_cap
        if base_target_enabled
        else remaining_quote_cap / order_price
    )
    remaining_quote = remaining_quote_cap if quote_target_enabled else 0.0
    progress_mode = (
        "unlimited"
        if cfg.unlimited_total
        else "quote"
        if quote_target_enabled
        else "base"
    )

    def make_plan(status: str, order: SlowExecutionOrder | None = None) -> SlowExecutionPlan:
        return SlowExecutionPlan(
            exchange=cfg.exchange,
            symbol=cfg.symbol,
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            existing_spread_bps=existing_spread_bps,
            total_base=cfg.total_base,
            submitted_base=safe_submitted_base,
            remaining_base=remaining_base,
            total_quote=cfg.total_quote,
            submitted_quote=safe_submitted_quote,
            remaining_quote=remaining_quote,
            progress_mode=progress_mode,
            unlimited_total=cfg.unlimited_total,
            interval_seconds=cfg.interval_seconds,
            order_ttl_seconds=cfg.order_ttl_seconds,
            slice_mode=slice_mode,
            slice_base=cfg.slice_base,
            slice_base_min=cfg.slice_base_min,
            slice_base_max=cfg.slice_base_max,
            slice_quote=cfg.slice_quote,
            randomize_slice=cfg.randomize_slice,
            start_price=cfg.start_price,
            stop_price=cfg.stop_price,
            price_mode=price_mode,
            price_offset_bps=cfg.price_offset_bps,
            trigger_price=trigger_price,
            order=order,
            status=status,
            observed_at=time(),
            **metric_kwargs,
        )

    if (
        (base_target_enabled and remaining_base_cap <= 0)
        or (quote_target_enabled and remaining_quote_cap <= 0)
    ):
        remaining_base = 0.0 if base_target_enabled else remaining_base
        remaining_quote = 0.0 if quote_target_enabled else remaining_quote
        return make_plan("complete")

    if side == "buy" and _is_stopped_by_price(side, trigger_price, cfg.stop_price):
        return make_plan("stopped_by_price")

    if (
        not start_price_triggered
        and _is_waiting_for_start_price(side, trigger_price, cfg.start_price)
    ):
        return make_plan("waiting_for_start_price")

    if side == "sell" and _is_stopped_by_price(side, trigger_price, cfg.stop_price):
        return make_plan("stopped_by_price")

    remaining_quote_as_base = (
        remaining_quote_cap / order_price if quote_target_enabled else float("inf")
    )
    order_base = min(remaining_base_cap, remaining_quote_as_base, selected_slice_base)
    quote_notional = order_base * order_price
    if quote_notional < cfg.min_order_quote:
        return make_plan("below_min_order_quote")

    order = SlowExecutionOrder(
        side=side,
        price=order_price,
        amount=order_base,
        quote_notional=quote_notional,
        submitted_base_before=safe_submitted_base,
        submitted_base_after=(
            min(cfg.total_base, safe_submitted_base + order_base)
            if base_target_enabled
            else safe_submitted_base + order_base
        ),
        submitted_quote_before=safe_submitted_quote,
        submitted_quote_after=(
            min(cfg.total_quote, safe_submitted_quote + quote_notional)
            if quote_target_enabled
            else safe_submitted_quote + quote_notional
        ),
    )
    return make_plan("planned", order)
