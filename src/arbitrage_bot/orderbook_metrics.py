from __future__ import annotations

from .models import BookLevel, OrderBookSnapshot


def _depth_quote(levels: list[BookLevel]) -> float:
    return sum(level.price * level.amount for level in levels)


def _side_max_gap_bps(levels: list[BookLevel], mid_price: float) -> float:
    if mid_price <= 0 or len(levels) < 2:
        return 0.0
    gaps = [
        abs(levels[index].price - levels[index - 1].price) / mid_price * 10_000
        for index in range(1, len(levels))
    ]
    return max(gaps, default=0.0)


def order_book_metric_snapshot(book: OrderBookSnapshot) -> dict[str, float | int | None]:
    if not book.bids or not book.asks:
        return {
            "bid_depth_quote": 0.0,
            "ask_depth_quote": 0.0,
            "max_level_gap_bps": 0.0,
            "order_book_timestamp_ms": book.timestamp_ms,
            "order_book_received_at": book.received_at,
        }

    mid_price = (book.bids[0].price + book.asks[0].price) / 2
    return {
        "bid_depth_quote": _depth_quote(book.bids),
        "ask_depth_quote": _depth_quote(book.asks),
        "max_level_gap_bps": max(
            _side_max_gap_bps(book.bids, mid_price),
            _side_max_gap_bps(book.asks, mid_price),
        ),
        "order_book_timestamp_ms": book.timestamp_ms,
        "order_book_received_at": book.received_at,
    }
