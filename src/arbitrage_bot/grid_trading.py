from __future__ import annotations

import math
from dataclasses import dataclass, field
from time import time
from typing import Any

from .config import DcaConfig, SpotGridConfig
from .models import OrderBookSnapshot, Side
from .orderbook_metrics import order_book_metric_snapshot


@dataclass(frozen=True)
class GridOrder:
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
class GridFill:
    side: Side
    level: int
    price: float
    amount: float
    quote_notional: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GridFill":
        side = str(raw.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("grid fill side must be buy or sell")
        return cls(
            side=side,  # type: ignore[arg-type]
            level=int(raw.get("level") or 0),
            price=float(raw.get("price") or 0.0),
            amount=float(raw.get("amount") or raw.get("filled") or 0.0),
            quote_notional=float(raw.get("quote_notional") or raw.get("cost") or 0.0),
        )


@dataclass(frozen=True)
class GridFillReplacementPlan:
    status: str
    reason: str
    replacements: list[GridOrder] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "replacements": [order.to_dict() for order in self.replacements],
        }


@dataclass(frozen=True)
class SpotGridPlan:
    exchange: str
    symbol: str
    quote_currency: str
    best_bid: float
    best_ask: float
    mid_price: float
    lower_price: float
    upper_price: float
    grid_count: int
    spacing: str
    quote_per_grid: float
    grid_step_bps: float
    status: str
    reason: str
    auto_rebuild: bool
    max_open_orders: int
    min_grid_step_bps: float
    cancel_retry_attempts: int
    grid_prices: list[float] = field(default_factory=list)
    orders: list[GridOrder] = field(default_factory=list)
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
            "quote_currency": self.quote_currency,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "grid_count": self.grid_count,
            "spacing": self.spacing,
            "quote_per_grid": self.quote_per_grid,
            "grid_step_bps": self.grid_step_bps,
            "status": self.status,
            "reason": self.reason,
            "auto_rebuild": self.auto_rebuild,
            "max_open_orders": self.max_open_orders,
            "min_grid_step_bps": self.min_grid_step_bps,
            "cancel_retry_attempts": self.cancel_retry_attempts,
            "grid_prices": self.grid_prices,
            "orders": [order.to_dict() for order in self.orders],
            "observed_at": self.observed_at,
            "bid_depth_quote": self.bid_depth_quote,
            "ask_depth_quote": self.ask_depth_quote,
            "max_level_gap_bps": self.max_level_gap_bps,
            "order_book_timestamp_ms": self.order_book_timestamp_ms,
            "order_book_received_at": self.order_book_received_at,
        }


@dataclass(frozen=True)
class DcaOrder:
    side: Side
    order_index: int
    price: float
    amount: float
    quote_notional: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "order_index": self.order_index,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
        }


@dataclass(frozen=True)
class DcaPlan:
    exchange: str
    symbol: str
    side: Side
    quote_currency: str
    best_bid: float
    best_ask: float
    mid_price: float
    trigger_price: float
    interval_seconds: float
    quote_per_order: float
    size_multiplier: float
    max_orders: int
    average_entry_price: float
    take_profit_price: float
    max_position_base: float
    max_loss_quote: float
    price_mode: str
    price_offset_bps: float
    status: str
    reason: str
    next_order: DcaOrder | None
    order_schedule: list[dict[str, Any]]
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
            "quote_currency": self.quote_currency,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "trigger_price": self.trigger_price,
            "interval_seconds": self.interval_seconds,
            "quote_per_order": self.quote_per_order,
            "size_multiplier": self.size_multiplier,
            "max_orders": self.max_orders,
            "average_entry_price": self.average_entry_price,
            "take_profit_price": self.take_profit_price,
            "max_position_base": self.max_position_base,
            "max_loss_quote": self.max_loss_quote,
            "price_mode": self.price_mode,
            "price_offset_bps": self.price_offset_bps,
            "status": self.status,
            "reason": self.reason,
            "next_order": None if self.next_order is None else self.next_order.to_dict(),
            "order_schedule": self.order_schedule,
            "observed_at": self.observed_at,
            "bid_depth_quote": self.bid_depth_quote,
            "ask_depth_quote": self.ask_depth_quote,
            "max_level_gap_bps": self.max_level_gap_bps,
            "order_book_timestamp_ms": self.order_book_timestamp_ms,
            "order_book_received_at": self.order_book_received_at,
        }


def _quote_currency(symbol: str) -> str:
    return symbol.split("/", 1)[1].upper() if "/" in symbol else ""


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


def _grid_prices(cfg: SpotGridConfig) -> tuple[list[float], float]:
    spacing = str(cfg.spacing or "arithmetic").lower()
    if spacing not in {"arithmetic", "geometric"}:
        raise ValueError("spot_grid.spacing must be arithmetic or geometric")
    if cfg.lower_price <= 0 or cfg.upper_price <= 0:
        raise ValueError("spot_grid lower_price and upper_price must be positive")
    if cfg.upper_price <= cfg.lower_price:
        raise ValueError("spot_grid upper_price must be greater than lower_price")
    if cfg.grid_count <= 0:
        raise ValueError("spot_grid grid_count must be positive")
    if cfg.quote_per_grid <= 0:
        raise ValueError("spot_grid quote_per_grid must be positive")

    if spacing == "geometric":
        ratio = (cfg.upper_price / cfg.lower_price) ** (1 / cfg.grid_count)
        prices = [cfg.lower_price * (ratio**idx) for idx in range(cfg.grid_count + 1)]
    else:
        step = (cfg.upper_price - cfg.lower_price) / cfg.grid_count
        prices = [cfg.lower_price + step * idx for idx in range(cfg.grid_count + 1)]

    step_bps = min(
        (prices[idx] - prices[idx - 1]) / prices[idx - 1] * 10_000
        for idx in range(1, len(prices))
    )
    return prices, step_bps


def build_spot_grid_plan(
    book: OrderBookSnapshot,
    cfg: SpotGridConfig,
) -> SpotGridPlan:
    best_bid, best_ask, mid_price = _top_of_book(book)
    prices, grid_step_bps = _grid_prices(cfg)
    status = "planned"
    reason = "inside configured grid range"
    if cfg.stop_loss_price > 0 and mid_price <= cfg.stop_loss_price:
        status = "stopped_by_stop_loss"
        reason = "mid price is at or below stop loss"
    elif cfg.take_profit_price > 0 and mid_price >= cfg.take_profit_price:
        status = "stopped_by_take_profit"
        reason = "mid price is at or above take profit"
    elif mid_price < cfg.lower_price:
        status = "below_range"
        reason = "mid price is below lower grid price"
    elif mid_price > cfg.upper_price:
        status = "above_range"
        reason = "mid price is above upper grid price"
    elif grid_step_bps < cfg.min_grid_step_bps:
        status = "blocked_by_min_grid_step"
        reason = "grid step is below min_grid_step_bps"

    orders: list[GridOrder] = []
    if status == "planned":
        for level, price in enumerate(prices, start=1):
            if math.isclose(price, mid_price, rel_tol=0.0, abs_tol=1e-18):
                continue
            side: Side = "buy" if price < mid_price else "sell"
            distance_bps = abs(price - mid_price) / mid_price * 10_000
            orders.append(
                GridOrder(
                    side=side,
                    level=level,
                    price=price,
                    amount=cfg.quote_per_grid / price,
                    quote_notional=cfg.quote_per_grid,
                    distance_bps=distance_bps,
                )
            )
        orders = sorted(orders, key=lambda item: item.distance_bps)
        if cfg.max_open_orders > 0:
            orders = orders[: cfg.max_open_orders]

    return SpotGridPlan(
        exchange=cfg.exchange,
        symbol=cfg.symbol,
        quote_currency=_quote_currency(cfg.symbol),
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        lower_price=cfg.lower_price,
        upper_price=cfg.upper_price,
        grid_count=cfg.grid_count,
        spacing=str(cfg.spacing or "arithmetic").lower(),
        quote_per_grid=cfg.quote_per_grid,
        grid_step_bps=grid_step_bps,
        status=status,
        reason=reason,
        auto_rebuild=cfg.auto_rebuild,
        max_open_orders=cfg.max_open_orders,
        min_grid_step_bps=cfg.min_grid_step_bps,
        cancel_retry_attempts=cfg.cancel_retry_attempts,
        grid_prices=prices,
        orders=orders,
        **_metric_fields(book),
    )


def build_spot_grid_fill_replacement_plan(
    cfg: SpotGridConfig,
    fills: list[GridFill | dict[str, Any]],
) -> GridFillReplacementPlan:
    try:
        prices, _ = _grid_prices(cfg)
    except ValueError as exc:
        return GridFillReplacementPlan(
            status="blocked",
            reason=str(exc),
        )
    if not fills:
        return GridFillReplacementPlan(
            status="idle",
            reason="no grid fills to replace",
        )

    replacements: list[GridOrder] = []
    for raw_fill in fills:
        fill = raw_fill if isinstance(raw_fill, GridFill) else GridFill.from_dict(raw_fill)
        if fill.amount <= 0 or fill.price <= 0:
            continue
        level_index = fill.level - 1 if fill.level > 0 else min(
            range(len(prices)),
            key=lambda index: abs(prices[index] - fill.price),
        )
        if level_index < 0 or level_index >= len(prices):
            continue
        target_index = level_index + 1 if fill.side == "buy" else level_index - 1
        if target_index < 0 or target_index >= len(prices):
            continue
        target_price = prices[target_index]
        if target_price <= 0:
            continue
        if fill.side == "buy":
            amount = fill.amount
            quote_notional = amount * target_price
            side: Side = "sell"
        else:
            quote_notional = fill.quote_notional if fill.quote_notional > 0 else fill.amount * fill.price
            amount = quote_notional / target_price
            side = "buy"
        replacements.append(
            GridOrder(
                side=side,
                level=target_index + 1,
                price=target_price,
                amount=amount,
                quote_notional=quote_notional,
                distance_bps=abs(target_price - fill.price) / fill.price * 10_000,
            )
        )

    if not replacements:
        return GridFillReplacementPlan(
            status="no_replacement",
            reason="fills were outside the grid boundaries or had zero size",
        )
    if cfg.max_open_orders > 0:
        replacements = replacements[: cfg.max_open_orders]
    return GridFillReplacementPlan(
        status="planned",
        reason="replace filled grid orders at adjacent levels",
        replacements=replacements,
    )


def _dca_side(side: str) -> Side:
    normalized = str(side or "buy").lower()
    if normalized not in {"buy", "sell"}:
        raise ValueError("dca.side must be buy or sell")
    return normalized  # type: ignore[return-value]


def _dca_price(
    cfg: DcaConfig,
    *,
    side: Side,
    best_bid: float,
    best_ask: float,
) -> float:
    price_mode = str(cfg.price_mode or "taker").lower()
    if price_mode not in {"taker", "maker"}:
        raise ValueError("dca.price_mode must be taker or maker")
    price = (best_ask if side == "buy" else best_bid) if price_mode == "taker" else (
        best_bid if side == "buy" else best_ask
    )
    if cfg.price_offset_bps <= 0:
        return price
    offset = cfg.price_offset_bps / 10_000
    return price * (1 - offset if side == "buy" else 1 + offset)


def build_dca_plan(book: OrderBookSnapshot, cfg: DcaConfig) -> DcaPlan:
    if cfg.quote_per_order <= 0:
        raise ValueError("dca.quote_per_order must be positive")
    if cfg.interval_seconds <= 0:
        raise ValueError("dca.interval_seconds must be positive")
    if cfg.size_multiplier < 1:
        raise ValueError("dca.size_multiplier must be greater than or equal to 1")
    if cfg.max_orders <= 0:
        raise ValueError("dca.max_orders must be positive")
    side = _dca_side(cfg.side)
    best_bid, best_ask, mid_price = _top_of_book(book)
    price = _dca_price(cfg, side=side, best_bid=best_bid, best_ask=best_ask)

    status = "ready"
    reason = "trigger conditions are satisfied"
    if cfg.trigger_price > 0:
        if side == "buy" and price > cfg.trigger_price:
            status = "waiting_for_trigger"
            reason = "buy DCA waits for price at or below trigger"
        elif side == "sell" and price < cfg.trigger_price:
            status = "waiting_for_trigger"
            reason = "sell DCA waits for price at or above trigger"
    if cfg.take_profit_price > 0:
        if side == "buy" and price >= cfg.take_profit_price:
            status = "take_profit_reached"
            reason = "price is at or above take profit"
        elif side == "sell" and price <= cfg.take_profit_price:
            status = "take_profit_reached"
            reason = "price is at or below take profit"

    order_schedule = []
    for index in range(cfg.max_orders):
        quote_notional = cfg.quote_per_order * (cfg.size_multiplier**index)
        order_schedule.append(
            {
                "order_index": index + 1,
                "quote_notional": quote_notional,
                "amount_at_current_price": quote_notional / price,
            }
        )
    next_order = (
        DcaOrder(
            side=side,
            order_index=1,
            price=price,
            amount=cfg.quote_per_order / price,
            quote_notional=cfg.quote_per_order,
        )
        if status == "ready"
        else None
    )

    return DcaPlan(
        exchange=cfg.exchange,
        symbol=cfg.symbol,
        side=side,
        quote_currency=_quote_currency(cfg.symbol),
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        trigger_price=cfg.trigger_price,
        interval_seconds=cfg.interval_seconds,
        quote_per_order=cfg.quote_per_order,
        size_multiplier=cfg.size_multiplier,
        max_orders=cfg.max_orders,
        average_entry_price=cfg.average_entry_price,
        take_profit_price=cfg.take_profit_price,
        max_position_base=cfg.max_position_base,
        max_loss_quote=cfg.max_loss_quote,
        price_mode=str(cfg.price_mode or "taker").lower(),
        price_offset_bps=cfg.price_offset_bps,
        status=status,
        reason=reason,
        next_order=next_order,
        order_schedule=order_schedule,
        **_metric_fields(book),
    )
