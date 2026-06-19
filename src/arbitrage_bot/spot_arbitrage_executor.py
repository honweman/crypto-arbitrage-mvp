from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .config import BotConfig, ExchangeConfig
from .exchanges import ExchangeManager
from .models import BookLevel, Opportunity, OpportunityLeg, OrderBookSnapshot
from .risk import (
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)


DEFAULT_ORDER_TTL_SECONDS = 2.0


@dataclass(frozen=True)
class ExecutableArbitrageOrder:
    exchange: ExchangeConfig
    leg: OpportunityLeg
    price: float
    quote_notional_common: float
    quote_notional_local: float
    slippage_bps: float
    prepared: dict[str, Any] | None = None

    def to_plan_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange.key,
            "symbol": self.leg.symbol,
            "side": self.leg.side,
            "amount": self.leg.quantity_base,
            "price": self.price,
            "quote_notional": self.quote_notional_common,
            "local_quote_notional": self.quote_notional_local,
            "quote_currency": self.leg.quote_currency,
            "slippage_bps": self.slippage_bps,
        }


def _quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0].upper()


def _base_currency(symbol: str) -> str:
    return symbol.split("/", 1)[0].upper()


def _leg_quote_rate(leg: OpportunityLeg, quote_rates: dict[str, float]) -> float | None:
    if leg.common_quote_rate is not None and leg.common_quote_rate > 0:
        return float(leg.common_quote_rate)
    quote = (leg.quote_currency or _quote_currency(leg.symbol)).upper()
    rate = quote_rates.get(quote)
    return float(rate) if rate is not None and rate > 0 else None


def _price_for_quantity(
    levels: list[BookLevel],
    *,
    quantity_base: float,
) -> float | None:
    remaining = quantity_base
    last_price: float | None = None
    for level in levels:
        if remaining <= 0:
            break
        if level.amount <= 0 or level.price <= 0:
            continue
        last_price = level.price
        remaining -= min(remaining, level.amount)
    if remaining > max(quantity_base * 1e-9, 1e-12):
        return None
    return last_price


def _leg_limit_price(
    leg: OpportunityLeg,
    book: OrderBookSnapshot,
) -> float | None:
    levels = book.asks if leg.side == "buy" else book.bids
    return _price_for_quantity(levels, quantity_base=leg.quantity_base)


def _leg_slippage_bps(
    leg: OpportunityLeg,
    book: OrderBookSnapshot,
    price: float,
) -> float:
    if not book.bids or not book.asks:
        return 0.0
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return 0.0
    if leg.side == "buy":
        return max(0.0, price - best_ask) / mid * 10_000
    return max(0.0, best_bid - price) / mid * 10_000


def _plan_symbol(opportunity: Opportunity) -> str:
    asset = opportunity.metadata.get("asset")
    if asset:
        return str(asset).upper()
    if opportunity.legs:
        return _base_currency(opportunity.legs[0].symbol)
    return ""


def build_spot_arbitrage_orders(
    cfg: BotConfig,
    opportunity: Opportunity,
    *,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
) -> tuple[list[ExecutableArbitrageOrder], list[str]]:
    exchange_by_key = {exchange.key: exchange for exchange in cfg.spot_exchanges}
    orders: list[ExecutableArbitrageOrder] = []
    errors: list[str] = []

    for leg in opportunity.legs:
        exchange = exchange_by_key.get(leg.exchange)
        if exchange is None:
            errors.append(f"unknown exchange account: {leg.exchange}")
            continue
        book = books.get((leg.exchange, leg.symbol))
        if book is None:
            errors.append(f"missing order book: {leg.exchange} {leg.symbol}")
            continue
        price = _leg_limit_price(leg, book)
        if price is None:
            errors.append(f"insufficient order book depth: {leg.exchange} {leg.symbol}")
            continue
        quote_rate = _leg_quote_rate(leg, quote_rates)
        if quote_rate is None:
            quote = leg.quote_currency or _quote_currency(leg.symbol)
            errors.append(f"missing quote rate for {quote} -> {cfg.common_quote_currency}")
            continue
        local_notional = price * leg.quantity_base
        orders.append(
            ExecutableArbitrageOrder(
                exchange=exchange,
                leg=leg,
                price=price,
                quote_notional_common=local_notional * quote_rate,
                quote_notional_local=local_notional,
                slippage_bps=_leg_slippage_bps(leg, book, price),
            )
        )

    return orders, errors


def _risk_orders(orders: list[ExecutableArbitrageOrder]) -> list[RiskOrder]:
    return [
        RiskOrder(
            strategy="spot_spread",
            exchange=order.exchange.key,
            symbol=order.leg.symbol,
            side=order.leg.side,
            amount=order.leg.quantity_base,
            price=order.price,
            quote_notional=order.quote_notional_common,
            distance_bps=order.slippage_bps,
            slippage_bps=order.slippage_bps,
        )
        for order in orders
    ]


async def _existing_open_order_count(
    manager: ExchangeManager,
    orders: list[ExecutableArbitrageOrder],
) -> tuple[int | None, str | None]:
    count = 0
    seen: set[tuple[str, str]] = set()
    for order in orders:
        key = (order.exchange.key, order.leg.symbol)
        if key in seen:
            continue
        seen.add(key)
        try:
            count += len(
                await manager.fetch_open_orders(
                    order.exchange,
                    symbol=order.leg.symbol,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"{order.exchange.key} {order.leg.symbol}: {exc}"
    return count, None


async def _balance_errors(
    manager: ExchangeManager,
    orders: list[ExecutableArbitrageOrder],
) -> list[str]:
    errors: list[str] = []
    balance_by_exchange: dict[str, dict[str, Any]] = {}
    for order in orders:
        if order.exchange.key not in balance_by_exchange:
            balance_by_exchange[order.exchange.key] = await manager.fetch_balance(
                order.exchange
            )
        balance = balance_by_exchange[order.exchange.key]
        if order.leg.side == "buy":
            currency = (order.leg.quote_currency or _quote_currency(order.leg.symbol)).upper()
            required = float(order.leg.net_quote or order.quote_notional_local)
        else:
            currency = _base_currency(order.leg.symbol)
            required = float(order.leg.quantity_base)
        free = float((balance.get(currency) or {}).get("free") or 0.0)
        if free + 1e-12 < required:
            errors.append(
                f"{order.exchange.key} {currency} free {free:.8f} "
                f"is below required {required:.8f}"
            )
    return errors


async def _prepare_orders(
    manager: ExchangeManager,
    orders: list[ExecutableArbitrageOrder],
) -> tuple[list[ExecutableArbitrageOrder], list[str], list[str]]:
    prepared_orders: list[ExecutableArbitrageOrder] = []
    errors: list[str] = []
    warnings: list[str] = []
    for order in orders:
        prepared = await manager.prepare_limit_order(
            order.exchange,
            symbol=order.leg.symbol,
            side=order.leg.side,
            amount=order.leg.quantity_base,
            price=order.price,
        )
        errors.extend(
            f"{order.exchange.key} {order.leg.symbol}: {error}"
            for error in prepared.get("errors", [])
        )
        warnings.extend(
            f"{order.exchange.key} {order.leg.symbol}: {warning}"
            for warning in prepared.get("warnings", [])
        )
        prepared_orders.append(
            ExecutableArbitrageOrder(
                exchange=order.exchange,
                leg=order.leg,
                price=order.price,
                quote_notional_common=order.quote_notional_common,
                quote_notional_local=order.quote_notional_local,
                slippage_bps=order.slippage_bps,
                prepared=prepared,
            )
        )
    return prepared_orders, errors, warnings


async def _place_orders(
    cfg: BotConfig,
    manager: ExchangeManager,
    orders: list[ExecutableArbitrageOrder],
    *,
    order_ttl_seconds: float = DEFAULT_ORDER_TTL_SECONDS,
) -> dict[str, Any]:
    timestamp = int(time.time() * 1000)

    async def place_one(index: int, order: ExecutableArbitrageOrder) -> Any:
        return await manager.create_prepared_limit_order(
            order.exchange,
            symbol=order.leg.symbol,
            side=order.leg.side,
            prepared=order.prepared or {},
            post_only=False,
            client_order_id=f"crypto-arb-spot-{timestamp}-{index}",
        )

    results = await asyncio.gather(
        *[place_one(index, order) for index, order in enumerate(orders, start=1)],
        return_exceptions=True,
    )
    placed: list[dict[str, Any]] = []
    create_errors: list[dict[str, Any]] = []
    for order, result in zip(orders, results):
        if isinstance(result, Exception):
            create_errors.append(
                {
                    "exchange": order.exchange.key,
                    "symbol": order.leg.symbol,
                    "side": order.leg.side,
                    "error": f"{result.__class__.__name__}: {result}",
                }
            )
        elif isinstance(result, dict):
            placed.append(result)

    cancel_errors: list[dict[str, Any]] = []
    canceled: list[dict[str, Any]] = []
    emergency_cancel = bool(create_errors and placed)
    if placed and (emergency_cancel or order_ttl_seconds > 0):
        if not emergency_cancel:
            await asyncio.sleep(order_ttl_seconds)
        for order, raw in zip(orders, results):
            if not isinstance(raw, dict):
                continue
            order_id = str(raw.get("id") or "")
            if not order_id:
                continue
            try:
                open_orders = await manager.fetch_open_orders(
                    order.exchange,
                    symbol=order.leg.symbol,
                )
                still_open = any(str(item.get("id") or "") == order_id for item in open_orders)
                if still_open:
                    canceled.append(
                        await manager.cancel_order(
                            order.exchange,
                            symbol=order.leg.symbol,
                            order_id=order_id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                cancel_errors.append(
                    {
                        "exchange": order.exchange.key,
                        "symbol": order.leg.symbol,
                        "order_id": order_id,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )

    return {
        "placed_count": len(placed),
        "placed_order_ids": [str(order.get("id") or "") for order in placed],
        "placed_orders": placed,
        "create_errors": create_errors,
        "canceled_count": len(canceled),
        "canceled_order_ids": [str(order.get("id") or "") for order in canceled],
        "cancel_errors": cancel_errors,
        "order_ttl_seconds": order_ttl_seconds,
        "emergency_cancel": emergency_cancel,
        "cancel_reason": (
            "create_error" if emergency_cancel else "ttl_expired" if placed else None
        ),
    }


async def run_spot_arbitrage_execution_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    opportunities: list[Opportunity],
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    live: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "spot_spread_execution",
        "strategy": "spot_spread",
        "mode": "live" if live else "dry_run",
        "status": "no_opportunity",
    }
    if not opportunities:
        return payload

    opportunity = opportunities[0]
    orders, plan_errors = build_spot_arbitrage_orders(
        cfg,
        opportunity,
        books=books,
        quote_rates=quote_rates,
    )
    plan = {
        "exchange": "multi",
        "symbol": _plan_symbol(opportunity),
        "orders": [order.to_plan_dict() for order in orders],
        "profit_quote": opportunity.profit_quote,
        "profit_bps": opportunity.profit_bps,
        "observed_at": opportunity.observed_at,
    }
    payload.update(
        {
            "status": "planned",
            "opportunity": opportunity.to_dict(),
            "plan": plan,
        }
    )
    if plan_errors:
        payload["status"] = "blocked_by_plan"
        payload["errors"] = plan_errors
        return payload

    existing_open_order_count, open_order_error = await _existing_open_order_count(
        manager,
        orders,
    )
    risk = evaluate_order_batch(
        cfg.risk,
        _risk_orders(orders),
        strategy="spot_spread",
        live=live,
        plan_observed_at=opportunity.observed_at,
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=existing_open_order_count,
        open_order_error=open_order_error,
        post_only=False,
    )
    payload["risk"] = risk.to_dict()
    payload["risk"]["currency"] = cfg.common_quote_currency
    if not risk.approved:
        payload["status"] = "blocked_by_risk"
        return payload

    prepared_orders, validation_errors, validation_warnings = await _prepare_orders(
        manager,
        orders,
    )
    payload["order_validation"] = {
        "status": "error" if validation_errors else "ok",
        "errors": validation_errors,
        "warnings": validation_warnings,
        "orders": [
            order.prepared for order in prepared_orders if order.prepared is not None
        ],
    }
    if validation_errors:
        payload["status"] = "blocked_by_validation"
        payload["risk"]["approved"] = False
        payload["risk"]["level"] = "blocked"
        payload["risk"]["reasons"] = [
            *payload["risk"].get("reasons", []),
            *[f"order validation: {error}" for error in validation_errors],
        ]
        return payload

    if live:
        balance_errors = await _balance_errors(manager, prepared_orders)
        if balance_errors:
            payload["status"] = "blocked_by_balance"
            payload["risk"]["approved"] = False
            payload["risk"]["level"] = "blocked"
            payload["risk"]["reasons"] = [
                *payload["risk"].get("reasons", []),
                *balance_errors,
            ]
            return payload
        execution = await _place_orders(cfg, manager, prepared_orders)
        payload["execution"] = execution
        payload["status"] = (
            "execution_error"
            if execution["create_errors"] or execution["cancel_errors"]
            else "placed"
        )

    return payload
