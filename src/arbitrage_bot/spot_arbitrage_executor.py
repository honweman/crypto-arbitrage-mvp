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


def _paper_execution_payload(
    opportunity: Opportunity,
    orders: list[ExecutableArbitrageOrder],
) -> dict[str, Any]:
    max_slippage = max((order.slippage_bps for order in orders), default=0.0)
    return {
        "status": "estimated",
        "order_count": len(orders),
        "estimated_profit_quote": opportunity.profit_quote,
        "estimated_profit_bps": opportunity.profit_bps,
        "estimated_total_quote_notional": sum(
            order.quote_notional_common for order in orders
        ),
        "max_slippage_bps": max_slippage,
        "orders": [order.to_plan_dict() for order in orders],
    }


def _protection_payload(
    cfg: BotConfig,
    opportunity: Opportunity,
    orders: list[ExecutableArbitrageOrder],
    *,
    evaluated_at: float,
) -> dict[str, Any]:
    max_slippage = max((order.slippage_bps for order in orders), default=0.0)
    opportunity_to_decision_ms = max(0.0, (evaluated_at - opportunity.observed_at) * 1000)
    return {
        "max_allowed_slippage_bps": cfg.risk.max_slippage_bps,
        "max_slippage_bps": max_slippage,
        "slippage_ok": (
            cfg.risk.max_slippage_bps <= 0
            or max_slippage <= cfg.risk.max_slippage_bps
        ),
        "max_plan_age_seconds": cfg.risk.max_plan_age_seconds,
        "opportunity_to_decision_ms": opportunity_to_decision_ms,
        "plan_age_ok": (
            cfg.risk.max_plan_age_seconds <= 0
            or opportunity_to_decision_ms / 1000 <= cfg.risk.max_plan_age_seconds
        ),
    }


def _raw_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("orderId") or raw.get("uuid") or "")


def _raw_order_filled_base(raw: dict[str, Any]) -> float:
    for key in ("filled", "filled_amount", "executedQty", "executed_amount"):
        value = raw.get(key)
        if value is not None:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    return 0.0


def _raw_order_cost(raw: dict[str, Any]) -> float:
    for key in ("cost", "filled_quote", "executed_quote", "cumQuote"):
        value = raw.get(key)
        if value is not None:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    filled = _raw_order_filled_base(raw)
    price = raw.get("average") or raw.get("price")
    try:
        return max(0.0, filled * float(price))
    except (TypeError, ValueError):
        return 0.0


async def _fetch_orders_by_id(
    manager: ExchangeManager,
    order: ExecutableArbitrageOrder,
    *,
    limit: int = 50,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    try:
        open_orders = await manager.fetch_open_orders(
            order.exchange,
            symbol=order.leg.symbol,
        )
    except Exception:  # noqa: BLE001
        open_orders = []
    for raw in open_orders:
        if isinstance(raw, dict):
            order_id = _raw_order_id(raw)
            if order_id:
                rows[order_id] = raw
    fetch_closed = getattr(manager, "fetch_closed_orders", None)
    if callable(fetch_closed):
        try:
            closed_orders = await fetch_closed(
                order.exchange,
                symbol=order.leg.symbol,
                limit=limit,
            )
        except Exception:  # noqa: BLE001
            closed_orders = []
        for raw in closed_orders:
            if isinstance(raw, dict):
                order_id = _raw_order_id(raw)
                if order_id:
                    rows[order_id] = raw
    return rows


async def _execution_fill_status(
    manager: ExchangeManager,
    orders: list[ExecutableArbitrageOrder],
    results: list[Any],
) -> dict[str, Any]:
    order_rows: list[dict[str, Any]] = []
    buy_filled_base = 0.0
    sell_filled_base = 0.0
    for order, raw in zip(orders, results):
        if not isinstance(raw, dict):
            continue
        order_id = _raw_order_id(raw)
        fetched = {}
        if order_id:
            fetched = (await _fetch_orders_by_id(manager, order)).get(order_id, {})
        filled_base = max(
            _raw_order_filled_base(raw),
            _raw_order_filled_base(fetched),
        )
        filled_quote = max(_raw_order_cost(raw), _raw_order_cost(fetched))
        if order.leg.side == "buy":
            buy_filled_base += filled_base
        else:
            sell_filled_base += filled_base
        order_rows.append(
            {
                "exchange": order.exchange.key,
                "symbol": order.leg.symbol,
                "side": order.leg.side,
                "order_id": order_id,
                "requested_base": order.leg.quantity_base,
                "filled_base": filled_base,
                "remaining_base": max(0.0, order.leg.quantity_base - filled_base),
                "filled_quote": filled_quote,
                "fill_ratio": (
                    filled_base / order.leg.quantity_base
                    if order.leg.quantity_base > 0
                    else 0.0
                ),
            }
        )
    imbalance_base = buy_filled_base - sell_filled_base
    hedge_required = abs(imbalance_base) > 1e-12
    hedge_side = "sell" if imbalance_base > 0 else "buy" if imbalance_base < 0 else ""
    return {
        "orders": order_rows,
        "buy_filled_base": buy_filled_base,
        "sell_filled_base": sell_filled_base,
        "imbalance_base": imbalance_base,
        "hedge_required": hedge_required,
        "hedge_side": hedge_side,
        "hedge_base": abs(imbalance_base),
        "status": (
            "hedge_required"
            if hedge_required
            else "balanced" if order_rows else "no_fills_detected"
        ),
    }


def _paper_vs_live_payload(
    opportunity: Opportunity,
    execution: dict[str, Any],
) -> dict[str, Any]:
    error_count = len(execution.get("create_errors", [])) + len(
        execution.get("cancel_errors", [])
    )
    fill_status = execution.get("fill_status")
    hedge_required = (
        bool(fill_status.get("hedge_required"))
        if isinstance(fill_status, dict)
        else False
    )
    return {
        "paper_profit_quote": opportunity.profit_quote,
        "paper_profit_bps": opportunity.profit_bps,
        "live_placed_count": execution.get("placed_count", 0),
        "live_error_count": error_count,
        "hedge_required": hedge_required,
        "comparison_status": (
            "hedge_required"
            if hedge_required
            else "execution_error" if error_count else "orders_submitted"
        ),
        "actual_fill_profit_quote": None,
    }


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
    opportunity_observed_at: float | None = None,
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

    submit_started_at = time.time()
    results = await asyncio.gather(
        *[place_one(index, order) for index, order in enumerate(orders, start=1)],
        return_exceptions=True,
    )
    submitted_at = time.time()
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

    fill_status = await _execution_fill_status(manager, orders, results)
    manual_intervention_required = bool(
        cancel_errors
        or (
            isinstance(fill_status, dict)
            and fill_status.get("hedge_required")
        )
        or (emergency_cancel and len(canceled) < len(placed))
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
        "manual_intervention_required": manual_intervention_required,
        "cancel_reason": (
            "create_error" if emergency_cancel else "ttl_expired" if placed else None
        ),
        "submit_started_at": submit_started_at,
        "submitted_at": submitted_at,
        "create_latency_ms": (submitted_at - submit_started_at) * 1000,
        "opportunity_to_submit_ms": (
            (submit_started_at - opportunity_observed_at) * 1000
            if opportunity_observed_at is not None
            else None
        ),
        "fill_status": fill_status,
    }


async def run_spot_arbitrage_execution_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    opportunities: list[Opportunity],
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    live: bool,
    order_ttl_seconds: float = DEFAULT_ORDER_TTL_SECONDS,
) -> dict[str, Any]:
    cycle_started_at = time.time()
    payload: dict[str, Any] = {
        "type": "spot_spread_execution",
        "strategy": "spot_spread",
        "mode": "live" if live else "dry_run",
        "status": "no_opportunity",
        "timing": {
            "cycle_started_at": cycle_started_at,
        },
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
    protection = _protection_payload(
        cfg,
        opportunity,
        orders,
        evaluated_at=time.time(),
    )
    payload.update(
        {
            "status": "planned",
            "opportunity": opportunity.to_dict(),
            "plan": plan,
            "paper_execution": _paper_execution_payload(opportunity, orders),
            "protection": protection,
            "timing": {
                **payload["timing"],
                "opportunity_observed_at": opportunity.observed_at,
                "opportunity_age_ms": max(
                    0.0,
                    (cycle_started_at - opportunity.observed_at) * 1000,
                ),
                "plan_ready_at": time.time(),
            },
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

    validation_started_at = time.time()
    prepared_orders, validation_errors, validation_warnings = await _prepare_orders(
        manager,
        orders,
    )
    validation_finished_at = time.time()
    payload["timing"] = {
        **payload.get("timing", {}),
        "validation_started_at": validation_started_at,
        "validation_finished_at": validation_finished_at,
        "validation_latency_ms": (
            validation_finished_at - validation_started_at
        )
        * 1000,
    }
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
        execution = await _place_orders(
            cfg,
            manager,
            prepared_orders,
            order_ttl_seconds=order_ttl_seconds,
            opportunity_observed_at=opportunity.observed_at,
        )
        payload["execution"] = execution
        payload["paper_vs_live"] = _paper_vs_live_payload(opportunity, execution)
        payload["status"] = (
            "execution_error"
            if execution["create_errors"] or execution["cancel_errors"]
            else "placed"
        )
        fill_status = execution.get("fill_status")
        if (
            isinstance(fill_status, dict)
            and fill_status.get("hedge_required")
            and payload["status"] == "placed"
        ):
            payload["status"] = "hedge_required"

    return payload
