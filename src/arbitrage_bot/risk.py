from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import time
from typing import Any

from .config import BotConfig, PortfolioConfig, RiskConfig
from .fill_store import load_daily_pnl_summary
from .models import Side


_STRATEGY_OVERRIDE_FIELDS = {
    "max_order_quote",
    "max_cycle_quote",
    "max_exposure_quote",
    "max_daily_loss_quote",
    "max_orders_per_cycle",
    "max_open_orders",
    "max_cancels_per_cycle",
    "min_seconds_between_cancels",
    "max_existing_spread_bps",
    "max_price_distance_bps",
    "max_slippage_bps",
    "min_order_book_depth_quote",
    "max_order_book_gap_bps",
    "max_price_jump_bps",
    "max_plan_age_seconds",
    "max_order_book_age_seconds",
}


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
    slippage_bps: float = 0.0

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
            "slippage_bps": self.slippage_bps,
        }


@dataclass(frozen=True)
class RiskMarketContext:
    exchange: str
    symbol: str
    best_bid: float
    best_ask: float
    mid_price: float
    bid_depth_quote: float = 0.0
    ask_depth_quote: float = 0.0
    max_level_gap_bps: float = 0.0
    order_book_timestamp_ms: int | None = None
    order_book_received_at: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    level: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    order_count: int = 0
    total_quote_notional: float = 0.0
    projected_open_orders: int | None = None
    expected_cancel_count: int = 0
    expected_create_count: int = 0
    projected_positions_base: dict[str, float] = field(default_factory=dict)
    projected_exposure_quote: dict[str, float] = field(default_factory=dict)
    evaluated_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "level": self.level,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "order_count": self.order_count,
            "total_quote_notional": self.total_quote_notional,
            "projected_open_orders": self.projected_open_orders,
            "expected_cancel_count": self.expected_cancel_count,
            "expected_create_count": self.expected_create_count,
            "projected_positions_base": self.projected_positions_base,
            "projected_exposure_quote": self.projected_exposure_quote,
            "evaluated_at": self.evaluated_at,
        }


def portfolio_positions_base(portfolio: PortfolioConfig) -> dict[str, float]:
    positions = portfolio.positions
    if not positions and portfolio.asset:
        return {portfolio.asset.upper(): portfolio.position_base}
    return {
        position.asset.upper(): position.position_base
        for position in positions
    }


def portfolio_realized_pnl_quote(portfolio: PortfolioConfig) -> float:
    return sum(portfolio.realized_pnl.values())


def current_daily_pnl_quote(cfg: BotConfig) -> float:
    configured_pnl = portfolio_realized_pnl_quote(cfg.portfolio)
    if not cfg.pnl_store.enabled:
        return configured_pnl
    try:
        daily = load_daily_pnl_summary(
            cfg.pnl_store,
            currency=cfg.common_quote_currency,
        )
    except Exception:  # noqa: BLE001
        return configured_pnl
    return configured_pnl + float(daily.get("total_realized_pnl") or 0.0)


def _base_asset(symbol: str) -> str:
    return symbol.split("/", 1)[0].upper()


def _asset_limit(default_limit: float, by_asset: dict[str, float], asset: str) -> float:
    return by_asset.get(asset.upper(), default_limit)


def risk_config_for_strategy(cfg: RiskConfig, strategy: str) -> RiskConfig:
    overrides = cfg.strategy_overrides.get(strategy)
    if not overrides:
        return cfg
    clean = {
        field_name: value
        for field_name, value in overrides.items()
        if field_name in _STRATEGY_OVERRIDE_FIELDS
        and isinstance(value, (int, float))
        and value >= 0
    }
    if not clean:
        return cfg
    return replace(cfg, strategy_overrides={}, **clean)


def _adverse_slippage_bps(order: RiskOrder, market: RiskMarketContext | None) -> float:
    if market is None or market.mid_price <= 0:
        return order.slippage_bps
    if order.side == "buy":
        computed = max(0.0, order.price - market.best_ask) / market.mid_price * 10_000
    else:
        computed = max(0.0, market.best_bid - order.price) / market.mid_price * 10_000
    return max(order.slippage_bps, computed)


def _projected_position_data(
    orders: list[RiskOrder],
    *,
    current_positions_base: dict[str, float],
    market: RiskMarketContext | None,
) -> tuple[dict[str, float], dict[str, float]]:
    projected_positions: dict[str, float] = {}
    projected_exposure: dict[str, float] = {}
    assets = {_base_asset(order.symbol) for order in orders}
    for asset in assets:
        current_base = current_positions_base.get(asset, 0.0)
        buy_base = sum(
            order.amount
            for order in orders
            if _base_asset(order.symbol) == asset and order.side == "buy"
        )
        sell_base = sum(
            order.amount
            for order in orders
            if _base_asset(order.symbol) == asset and order.side == "sell"
        )
        max_abs_base = max(abs(current_base + buy_base), abs(current_base - sell_base))
        projected_positions[asset] = max_abs_base

        buy_quote = sum(
            order.quote_notional
            for order in orders
            if _base_asset(order.symbol) == asset and order.side == "buy"
        )
        sell_quote = sum(
            order.quote_notional
            for order in orders
            if _base_asset(order.symbol) == asset and order.side == "sell"
        )
        mid_price = market.mid_price if market and _base_asset(market.symbol) == asset else 0.0
        current_quote = abs(current_base) * mid_price
        projected_exposure[asset] = max(
            abs(current_quote + buy_quote),
            abs(current_quote - sell_quote),
        )
    return projected_positions, projected_exposure


def evaluate_order_batch(
    cfg: RiskConfig,
    orders: list[RiskOrder],
    *,
    strategy: str,
    live: bool,
    existing_spread_bps: float | None = None,
    plan_observed_at: float | None = None,
    market: RiskMarketContext | None = None,
    previous_mid_price: float | None = None,
    current_positions_base: dict[str, float] | None = None,
    daily_pnl_quote: float | None = None,
    existing_open_order_count: int | None = None,
    expected_cancel_count: int = 0,
    expected_create_count: int | None = None,
    last_cancel_at: float | None = None,
    open_order_error: str | None = None,
    post_only: bool = True,
) -> RiskDecision:
    cfg = risk_config_for_strategy(cfg, strategy)
    if not cfg.enabled:
        return RiskDecision(
            approved=True,
            level="off",
            order_count=len(orders),
            total_quote_notional=sum(order.quote_notional for order in orders),
        )

    reasons: list[str] = []
    warnings: list[str] = []
    projected_create_count = (
        len(orders)
        if expected_create_count is None
        else max(0, int(expected_create_count))
    )
    total_quote = sum(order.quote_notional for order in orders)
    current_positions = current_positions_base or {}
    projected_positions, projected_exposure = _projected_position_data(
        orders,
        current_positions_base=current_positions,
        market=market,
    )

    if not cfg.trading_enabled:
        reasons.append("risk.trading_enabled is false")
    if live and not cfg.allow_live_trading:
        reasons.append("risk.allow_live_trading is false")
    if strategy == "market_maker" and not cfg.allow_market_maker:
        reasons.append("risk.allow_market_maker is false")
    if strategy == "slow_execution" and not cfg.allow_slow_execution:
        reasons.append("risk.allow_slow_execution is false")
    if strategy in cfg.strategy_enabled and not cfg.strategy_enabled[strategy]:
        reasons.append(f"risk.strategy_enabled.{strategy} is false")
    if cfg.require_post_only and not post_only:
        reasons.append("post-only orders are required")
    if (
        cfg.max_daily_loss_quote > 0
        and daily_pnl_quote is not None
        and daily_pnl_quote <= -cfg.max_daily_loss_quote
    ):
        reasons.append(
            f"daily loss {daily_pnl_quote:.8f} exceeds "
            f"max_daily_loss_quote {cfg.max_daily_loss_quote:.8f}"
        )

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
    projected_open_orders: int | None = None
    if open_order_error:
        reasons.append(f"open order count unavailable: {open_order_error}")
    elif existing_open_order_count is not None:
        projected_open_orders = max(
            0,
            existing_open_order_count - expected_cancel_count,
        ) + projected_create_count
        if cfg.max_open_orders > 0 and projected_open_orders > cfg.max_open_orders:
            reasons.append(
                f"projected open orders {projected_open_orders} exceeds "
                f"max_open_orders {cfg.max_open_orders}"
            )
    elif live and cfg.max_open_orders > 0:
        warnings.append("open order count unavailable")

    if (
        cfg.max_cancels_per_cycle > 0
        and expected_cancel_count > cfg.max_cancels_per_cycle
    ):
        reasons.append(
            f"expected cancels {expected_cancel_count} exceeds "
            f"max_cancels_per_cycle {cfg.max_cancels_per_cycle}"
        )
    if (
        expected_cancel_count > 0
        and last_cancel_at is not None
        and cfg.min_seconds_between_cancels > 0
        and time() - last_cancel_at < cfg.min_seconds_between_cancels
    ):
        reasons.append(
            f"last cancel is within min_seconds_between_cancels "
            f"{cfg.min_seconds_between_cancels:.2f}"
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
    if market is None:
        if orders and cfg.min_order_book_depth_quote > 0:
            reasons.append("order book depth is unavailable")
    else:
        has_buy = any(order.side == "buy" for order in orders)
        has_sell = any(order.side == "sell" for order in orders)
        if (
            has_buy
            and cfg.min_order_book_depth_quote > 0
            and market.ask_depth_quote < cfg.min_order_book_depth_quote
        ):
            reasons.append(
                f"ask depth {market.ask_depth_quote:.8f} is below "
                f"min_order_book_depth_quote {cfg.min_order_book_depth_quote:.8f}"
            )
        if (
            has_sell
            and cfg.min_order_book_depth_quote > 0
            and market.bid_depth_quote < cfg.min_order_book_depth_quote
        ):
            reasons.append(
                f"bid depth {market.bid_depth_quote:.8f} is below "
                f"min_order_book_depth_quote {cfg.min_order_book_depth_quote:.8f}"
            )
        if (
            cfg.max_order_book_gap_bps > 0
            and market.max_level_gap_bps > cfg.max_order_book_gap_bps
        ):
            reasons.append(
                f"order book gap {market.max_level_gap_bps:.2f} bps exceeds "
                f"max_order_book_gap_bps {cfg.max_order_book_gap_bps:.2f}"
            )
        if (
            cfg.max_price_jump_bps > 0
            and previous_mid_price is not None
            and previous_mid_price > 0
        ):
            jump_bps = abs(market.mid_price - previous_mid_price) / previous_mid_price * 10_000
            if jump_bps > cfg.max_price_jump_bps:
                reasons.append(
                    f"price jump {jump_bps:.2f} bps exceeds "
                    f"max_price_jump_bps {cfg.max_price_jump_bps:.2f}"
                )
        if market.order_book_received_at is not None:
            age_seconds = time() - market.order_book_received_at
            if (
                cfg.max_order_book_age_seconds > 0
                and age_seconds > cfg.max_order_book_age_seconds
            ):
                reasons.append(
                    f"order book age {age_seconds:.2f}s exceeds "
                    f"max_order_book_age_seconds {cfg.max_order_book_age_seconds:.2f}s"
                )
        elif market.order_book_timestamp_ms is None:
            if cfg.require_order_book_timestamp:
                reasons.append("order book timestamp is required")
        elif cfg.max_order_book_age_seconds > 0:
            age_seconds = time() - market.order_book_timestamp_ms / 1000
            if age_seconds > cfg.max_order_book_age_seconds:
                reasons.append(
                    f"order book age {age_seconds:.2f}s exceeds "
                    f"max_order_book_age_seconds {cfg.max_order_book_age_seconds:.2f}s"
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
        if order.exchange in cfg.account_enabled and not cfg.account_enabled[order.exchange]:
            reasons.append(f"risk.account_enabled.{order.exchange} is false")
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
        slippage_bps = _adverse_slippage_bps(order, market)
        if cfg.max_slippage_bps > 0 and slippage_bps > cfg.max_slippage_bps:
            reasons.append(
                f"{order.exchange} {order.symbol} slippage {slippage_bps:.2f} "
                f"bps exceeds max_slippage_bps {cfg.max_slippage_bps:.2f}"
            )
        if allowed_exchanges and order.exchange not in allowed_exchanges:
            reasons.append(f"{order.exchange} is not in risk.allowed_exchanges")
        if order.exchange in blocked_exchanges:
            reasons.append(f"{order.exchange} is in risk.blocked_exchanges")
        if allowed_symbols and order.symbol not in allowed_symbols:
            reasons.append(f"{order.symbol} is not in risk.allowed_symbols")
        if order.symbol in blocked_symbols:
            reasons.append(f"{order.symbol} is in risk.blocked_symbols")

    for asset, projected_base in projected_positions.items():
        limit = _asset_limit(
            cfg.max_position_base,
            cfg.max_position_base_by_asset,
            asset,
        )
        if limit > 0 and projected_base > limit:
            reasons.append(
                f"{asset} projected position {projected_base:.8f} exceeds "
                f"max_position_base {limit:.8f}"
            )

    for asset, projected_quote in projected_exposure.items():
        limit = _asset_limit(
            cfg.max_exposure_quote,
            cfg.max_exposure_quote_by_asset,
            asset,
        )
        if limit > 0 and projected_quote > limit:
            reasons.append(
                f"{asset} projected exposure {projected_quote:.8f} exceeds "
                f"max_exposure_quote {limit:.8f}"
            )

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
        projected_open_orders=projected_open_orders,
        expected_cancel_count=expected_cancel_count,
        expected_create_count=projected_create_count,
        projected_positions_base=projected_positions,
        projected_exposure_quote=projected_exposure,
    )
