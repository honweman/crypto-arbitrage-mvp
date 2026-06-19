from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

from .config import ExecutionAlgoConfig
from .models import OrderBookSnapshot, Side
from .orderbook_metrics import order_book_metric_snapshot


@dataclass(frozen=True)
class ExecutionSlice:
    side: Side
    slice_index: int
    scheduled_at_seconds: float
    price: float
    amount: float
    quote_notional: float
    participation_rate: float
    expected_market_volume_quote: float
    status: str = "scheduled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "slice_index": self.slice_index,
            "scheduled_at_seconds": self.scheduled_at_seconds,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "participation_rate": self.participation_rate,
            "expected_market_volume_quote": self.expected_market_volume_quote,
            "status": self.status,
        }


@dataclass(frozen=True)
class ExecutionAlgoPlan:
    exchange: str
    symbol: str
    side: Side
    algo: str
    quote_currency: str
    best_bid: float
    best_ask: float
    mid_price: float
    execution_price: float
    total_base: float
    total_quote: float
    duration_seconds: float
    slice_count: int
    interval_seconds: float
    participation_rate: float
    volume_lookback_seconds: float
    price_mode: str
    price_offset_bps: float
    start_price: float
    stop_price: float
    max_slippage_bps: float
    status: str
    reason: str
    next_slice: ExecutionSlice | None
    schedule: list[ExecutionSlice]
    observed_at: float = field(default_factory=time)
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
            "algo": self.algo,
            "quote_currency": self.quote_currency,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "execution_price": self.execution_price,
            "total_base": self.total_base,
            "total_quote": self.total_quote,
            "duration_seconds": self.duration_seconds,
            "slice_count": self.slice_count,
            "interval_seconds": self.interval_seconds,
            "participation_rate": self.participation_rate,
            "volume_lookback_seconds": self.volume_lookback_seconds,
            "price_mode": self.price_mode,
            "price_offset_bps": self.price_offset_bps,
            "start_price": self.start_price,
            "stop_price": self.stop_price,
            "max_slippage_bps": self.max_slippage_bps,
            "status": self.status,
            "reason": self.reason,
            "next_slice": None if self.next_slice is None else self.next_slice.to_dict(),
            "schedule": [item.to_dict() for item in self.schedule],
            "observed_at": self.observed_at,
            "bid_depth_quote": self.bid_depth_quote,
            "ask_depth_quote": self.ask_depth_quote,
            "max_level_gap_bps": self.max_level_gap_bps,
            "order_book_timestamp_ms": self.order_book_timestamp_ms,
            "order_book_received_at": self.order_book_received_at,
        }


def _quote_currency(symbol: str) -> str:
    return symbol.split("/", 1)[1].partition(":")[0].upper() if "/" in symbol else ""


def _top_of_book(book: OrderBookSnapshot) -> tuple[float, float, float]:
    if not book.bids or not book.asks:
        raise ValueError("order book must have both bid and ask levels")
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("order book top bid/ask are not usable")
    return best_bid, best_ask, (best_bid + best_ask) / 2


def _metric_fields(book: OrderBookSnapshot) -> dict[str, Any]:
    metrics = order_book_metric_snapshot(book)
    return {
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


def _execution_side(side: str) -> Side:
    normalized = str(side or "buy").lower()
    if normalized not in {"buy", "sell"}:
        raise ValueError("execution_algo.side must be buy or sell")
    return normalized  # type: ignore[return-value]


def _execution_algo(algo: str) -> str:
    normalized = str(algo or "twap").lower()
    if normalized not in {"twap", "vwap", "pov"}:
        raise ValueError("execution_algo.algo must be twap, vwap, or pov")
    return normalized


def _execution_price(
    cfg: ExecutionAlgoConfig,
    *,
    side: Side,
    best_bid: float,
    best_ask: float,
) -> tuple[str, float]:
    price_mode = str(cfg.price_mode or "taker").lower()
    if price_mode not in {"taker", "maker"}:
        raise ValueError("execution_algo.price_mode must be taker or maker")
    price = (best_ask if side == "buy" else best_bid) if price_mode == "taker" else (
        best_bid if side == "buy" else best_ask
    )
    if cfg.price_offset_bps > 0:
        offset = cfg.price_offset_bps / 10_000
        price = price * (1 - offset if side == "buy" else 1 + offset)
    return price_mode, price


def _book_depth_quote(book: OrderBookSnapshot, side: Side) -> float:
    levels = book.asks if side == "buy" else book.bids
    return sum(level.price * level.amount for level in levels)


def _slice_weights(algo: str, slice_count: int) -> list[float]:
    if algo == "twap":
        return [1.0 for _ in range(slice_count)]
    if slice_count == 1:
        return [1.0]
    center = (slice_count - 1) / 2
    return [0.75 + abs(index - center) / max(center, 1.0) for index in range(slice_count)]


def _apply_slice_bounds(value: float, *, min_value: float, max_value: float) -> float:
    if max_value > 0:
        value = min(value, max_value)
    if min_value > 0 and 0 < value < min_value:
        value = min_value
    return value


def _target_notional(
    cfg: ExecutionAlgoConfig,
    price: float,
) -> tuple[float, float]:
    total_quote = cfg.total_quote
    total_base = cfg.total_base
    if total_quote <= 0 and total_base <= 0:
        raise ValueError("execution_algo total_base or total_quote must be positive")
    if total_quote <= 0:
        total_quote = total_base * price
    if total_base <= 0:
        total_base = total_quote / price
    return total_base, total_quote


def _status_from_price_gates(
    cfg: ExecutionAlgoConfig,
    *,
    side: Side,
    price: float,
) -> tuple[str, str]:
    if cfg.start_price > 0:
        if side == "buy" and price > cfg.start_price:
            return "waiting_for_start", "buy execution waits for price at or below start"
        if side == "sell" and price < cfg.start_price:
            return "waiting_for_start", "sell execution waits for price at or above start"
    if cfg.stop_price > 0:
        if side == "buy" and price >= cfg.stop_price:
            return "stopped_by_price", "buy execution stopped because price reached stop"
        if side == "sell" and price <= cfg.stop_price:
            return "stopped_by_price", "sell execution stopped because price reached stop"
    return "ready", "execution schedule is ready"


def build_execution_algo_plan(
    book: OrderBookSnapshot,
    cfg: ExecutionAlgoConfig,
) -> ExecutionAlgoPlan:
    if cfg.duration_seconds <= 0:
        raise ValueError("execution_algo.duration_seconds must be positive")
    if cfg.slice_count <= 0:
        raise ValueError("execution_algo.slice_count must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("execution_algo.interval_seconds must be positive")
    if cfg.participation_rate < 0:
        raise ValueError("execution_algo.participation_rate must be non-negative")
    if cfg.volume_lookback_seconds <= 0:
        raise ValueError("execution_algo.volume_lookback_seconds must be positive")

    side = _execution_side(cfg.side)
    algo = _execution_algo(cfg.algo)
    best_bid, best_ask, mid_price = _top_of_book(book)
    price_mode, price = _execution_price(
        cfg,
        side=side,
        best_bid=best_bid,
        best_ask=best_ask,
    )
    total_base, total_quote = _target_notional(cfg, price)
    status, reason = _status_from_price_gates(cfg, side=side, price=price)

    depth_quote = _book_depth_quote(book, side)
    schedule: list[ExecutionSlice] = []
    remaining_quote = total_quote
    weights = _slice_weights(algo, cfg.slice_count)
    weight_total = sum(weights) or 1.0
    for index in range(cfg.slice_count):
        if remaining_quote <= 0:
            break
        if algo == "pov":
            market_volume_quote = depth_quote
            quote_notional = market_volume_quote * cfg.participation_rate
            if quote_notional <= 0:
                quote_notional = min(remaining_quote, total_quote / cfg.slice_count)
        else:
            market_volume_quote = depth_quote * weights[index] / weight_total
            quote_notional = total_quote * weights[index] / weight_total
        quote_notional = _apply_slice_bounds(
            min(quote_notional, remaining_quote),
            min_value=cfg.min_slice_quote,
            max_value=cfg.max_slice_quote,
        )
        if quote_notional > remaining_quote:
            quote_notional = remaining_quote
        if quote_notional <= 0:
            continue
        schedule.append(
            ExecutionSlice(
                side=side,
                slice_index=index + 1,
                scheduled_at_seconds=min(
                    cfg.duration_seconds,
                    index * cfg.interval_seconds,
                ),
                price=price,
                amount=quote_notional / price,
                quote_notional=quote_notional,
                participation_rate=cfg.participation_rate if algo == "pov" else 0.0,
                expected_market_volume_quote=market_volume_quote,
                status="next" if index == 0 and status == "ready" else "scheduled",
            )
        )
        remaining_quote -= quote_notional

    if not schedule:
        status = "blocked_by_min_slice"
        reason = "no executable slices after applying slice limits"

    next_slice = schedule[0] if status == "ready" and schedule else None
    return ExecutionAlgoPlan(
        exchange=cfg.exchange,
        symbol=cfg.symbol,
        side=side,
        algo=algo,
        quote_currency=_quote_currency(cfg.symbol),
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        execution_price=price,
        total_base=total_base,
        total_quote=total_quote,
        duration_seconds=cfg.duration_seconds,
        slice_count=cfg.slice_count,
        interval_seconds=cfg.interval_seconds,
        participation_rate=cfg.participation_rate,
        volume_lookback_seconds=cfg.volume_lookback_seconds,
        price_mode=price_mode,
        price_offset_bps=cfg.price_offset_bps,
        start_price=cfg.start_price,
        stop_price=cfg.stop_price,
        max_slippage_bps=cfg.max_slippage_bps,
        status=status,
        reason=reason,
        next_slice=next_slice,
        schedule=schedule,
        **_metric_fields(book),
    )
