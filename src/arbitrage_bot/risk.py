from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

from .config import RiskConfig
from .models import Side


@dataclass(frozen=True)
class RiskOrder:
    strategy: str
    exchange: str
    symbol: str
    side: Side
    amount: float
    price: float
    quote_notional: float
    distance_bps: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "quote_notional": self.quote_notional,
            "distance_bps": self.distance_bps,
        }


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    level: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    order_count: int = 0
    total_quote_notional: float = 0.0
    evaluated_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "level": self.level,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "order_count": self.order_count,
            "total_quote_notional": self.total_quote_notional,
            "evaluated_at": self.evaluated_at,
        }


def evaluate_order_batch(
    cfg: RiskConfig,
    orders: list[RiskOrder],
    *,
    strategy: str,
    live: bool,
    existing_spread_bps: float | None = None,
    plan_observed_at: float | None = None,
    post_only: bool = True,
) -> RiskDecision:
    if not cfg.enabled:
        return RiskDecision(
            approved=True,
            level="off",
            order_count=len(orders),
            total_quote_notional=sum(order.quote_notional for order in orders),
        )

    reasons: list[str] = []
    warnings: list[str] = []
    total_quote = sum(order.quote_notional for order in orders)

    if live and not cfg.allow_live_trading:
        reasons.append("risk.allow_live_trading is false")
    if strategy == "market_maker" and not cfg.allow_market_maker:
        reasons.append("risk.allow_market_maker is false")
    if strategy == "slow_execution" and not cfg.allow_slow_execution:
        reasons.append("risk.allow_slow_execution is false")
    if cfg.require_post_only and not post_only:
        reasons.append("post-only orders are required")

    if cfg.max_orders_per_cycle > 0 and len(orders) > cfg.max_orders_per_cycle:
        reasons.append(
            f"order count {len(orders)} exceeds max_orders_per_cycle "
            f"{cfg.max_orders_per_cycle}"
        )
    if cfg.max_cycle_quote > 0 and total_quote > cfg.max_cycle_quote:
        reasons.append(
            f"cycle quote notional {total_quote:.8f} exceeds max_cycle_quote "
            f"{cfg.max_cycle_quote:.8f}"
        )
    if (
        existing_spread_bps is not None
        and cfg.max_existing_spread_bps > 0
        and existing_spread_bps > cfg.max_existing_spread_bps
    ):
        reasons.append(
            f"existing spread {existing_spread_bps:.2f} bps exceeds "
            f"max_existing_spread_bps {cfg.max_existing_spread_bps:.2f}"
        )
    if (
        plan_observed_at is not None
        and cfg.max_plan_age_seconds > 0
        and time() - plan_observed_at > cfg.max_plan_age_seconds
    ):
        reasons.append(
            f"plan age exceeds max_plan_age_seconds {cfg.max_plan_age_seconds:.2f}"
        )

    allowed_exchanges = set(cfg.allowed_exchanges)
    blocked_exchanges = set(cfg.blocked_exchanges)
    allowed_symbols = set(cfg.allowed_symbols)
    blocked_symbols = set(cfg.blocked_symbols)

    for order in orders:
        if order.amount <= 0:
            reasons.append(f"{order.exchange} {order.symbol} amount must be positive")
        if order.price <= 0:
            reasons.append(f"{order.exchange} {order.symbol} price must be positive")
        if order.quote_notional <= 0:
            reasons.append(
                f"{order.exchange} {order.symbol} quote_notional must be positive"
            )
        if (
            cfg.max_order_quote > 0
            and order.quote_notional > cfg.max_order_quote
        ):
            reasons.append(
                f"{order.exchange} {order.symbol} order quote "
                f"{order.quote_notional:.8f} exceeds max_order_quote "
                f"{cfg.max_order_quote:.8f}"
            )
        if (
            cfg.max_price_distance_bps > 0
            and order.distance_bps > cfg.max_price_distance_bps
        ):
            reasons.append(
                f"{order.exchange} {order.symbol} distance {order.distance_bps:.2f} "
                f"bps exceeds max_price_distance_bps "
                f"{cfg.max_price_distance_bps:.2f}"
            )
        if allowed_exchanges and order.exchange not in allowed_exchanges:
            reasons.append(f"{order.exchange} is not in risk.allowed_exchanges")
        if order.exchange in blocked_exchanges:
            reasons.append(f"{order.exchange} is in risk.blocked_exchanges")
        if allowed_symbols and order.symbol not in allowed_symbols:
            reasons.append(f"{order.symbol} is not in risk.allowed_symbols")
        if order.symbol in blocked_symbols:
            reasons.append(f"{order.symbol} is in risk.blocked_symbols")

    approved = len(reasons) == 0
    if not live and not cfg.allow_live_trading:
        warnings.append("live trading is disabled until risk.allow_live_trading=true")
    if not orders:
        warnings.append("no orders to evaluate")

    return RiskDecision(
        approved=approved,
        level="ok" if approved else "blocked",
        reasons=reasons,
        warnings=warnings,
        order_count=len(orders),
        total_quote_notional=total_quote,
    )
