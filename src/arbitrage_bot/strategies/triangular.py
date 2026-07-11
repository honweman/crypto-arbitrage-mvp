from __future__ import annotations

from dataclasses import dataclass
from arbitrage_bot.config import (
    ExchangeConfig,
    TriangleRouteConfig,
    TriangularArbitrageConfig,
)
from arbitrage_bot.models import BookLevel, Opportunity, OpportunityLeg, OrderBookSnapshot, Side


@dataclass(frozen=True)
class TriangleLegPlan:
    symbol: str
    side: Side
    input_currency: str
    output_currency: str


@dataclass(frozen=True)
class TriangleLegFill:
    plan: TriangleLegPlan
    input_amount: float
    output_amount: float
    quantity_base: float
    average_price: float
    gross_quote: float
    fee_quote: float


def _split_symbol(symbol: str) -> tuple[str, str]:
    if "/" not in symbol:
        return "", ""
    base, quote = symbol.split("/", 1)
    return base.upper(), quote.split(":", 1)[0].upper()


def _buy_base_with_quote(
    levels: list[BookLevel],
    *,
    quote_budget: float,
    fee_bps: float,
) -> tuple[float, float, float, float] | None:
    if quote_budget <= 0:
        return None
    fee_rate = fee_bps / 10_000
    gross_quote_budget = quote_budget / (1 + fee_rate)
    remaining_quote = gross_quote_budget
    acquired_base = 0.0
    gross_quote = 0.0
    for level in levels:
        max_quote = level.price * level.amount
        take_quote = min(remaining_quote, max_quote)
        acquired_base += take_quote / level.price
        gross_quote += take_quote
        remaining_quote -= take_quote
        if remaining_quote <= 1e-12:
            break
    if remaining_quote > 1e-9 or acquired_base <= 0:
        return None
    average_price = gross_quote / acquired_base
    fee_quote = gross_quote * fee_rate
    return acquired_base, average_price, gross_quote, fee_quote


def _sell_base_for_quote(
    levels: list[BookLevel],
    *,
    base_amount: float,
    fee_bps: float,
) -> tuple[float, float, float, float] | None:
    if base_amount <= 0:
        return None
    remaining_base = base_amount
    gross_quote = 0.0
    for level in levels:
        take_base = min(remaining_base, level.amount)
        gross_quote += take_base * level.price
        remaining_base -= take_base
        if remaining_base <= 1e-12:
            break
    if remaining_base > 1e-9 or gross_quote <= 0:
        return None
    average_price = gross_quote / base_amount
    fee_quote = gross_quote * fee_bps / 10_000
    return gross_quote - fee_quote, average_price, gross_quote, fee_quote


def _leg_plan_for_currency(symbol: str, input_currency: str) -> TriangleLegPlan | None:
    base, quote = _split_symbol(symbol)
    if not base or not quote:
        return None
    if input_currency == quote:
        return TriangleLegPlan(
            symbol=symbol,
            side="buy",
            input_currency=quote,
            output_currency=base,
        )
    if input_currency == base:
        return TriangleLegPlan(
            symbol=symbol,
            side="sell",
            input_currency=base,
            output_currency=quote,
        )
    return None


def _route_plans(route: TriangleRouteConfig) -> list[list[TriangleLegPlan]]:
    start = route.start_currency.upper()
    symbols = [symbol.upper() for symbol in route.symbols]
    plans: list[list[TriangleLegPlan]] = []

    def walk(
        current_currency: str,
        remaining_symbols: list[str],
        path: list[TriangleLegPlan],
    ) -> None:
        if len(path) == 3:
            if current_currency == start:
                plans.append(path)
            return
        for index, symbol in enumerate(remaining_symbols):
            leg = _leg_plan_for_currency(symbol, current_currency)
            if leg is None:
                continue
            walk(
                leg.output_currency,
                [*remaining_symbols[:index], *remaining_symbols[index + 1 :]],
                [*path, leg],
            )

    walk(start, symbols, [])
    seen: set[tuple[tuple[str, Side], ...]] = set()
    unique: list[list[TriangleLegPlan]] = []
    for plan in plans:
        key = tuple((leg.symbol, leg.side) for leg in plan)
        if key in seen:
            continue
        seen.add(key)
        unique.append(plan)
    return unique


def _simulate_leg(
    plan: TriangleLegPlan,
    book: OrderBookSnapshot,
    *,
    input_amount: float,
    fee_bps: float,
) -> TriangleLegFill | None:
    if plan.side == "buy":
        fill = _buy_base_with_quote(
            book.asks,
            quote_budget=input_amount,
            fee_bps=fee_bps,
        )
        if fill is None:
            return None
        output_amount, average_price, gross_quote, fee_quote = fill
        return TriangleLegFill(
            plan=plan,
            input_amount=input_amount,
            output_amount=output_amount,
            quantity_base=output_amount,
            average_price=average_price,
            gross_quote=gross_quote,
            fee_quote=fee_quote,
        )
    fill = _sell_base_for_quote(
        book.bids,
        base_amount=input_amount,
        fee_bps=fee_bps,
    )
    if fill is None:
        return None
    output_amount, average_price, gross_quote, fee_quote = fill
    return TriangleLegFill(
        plan=plan,
        input_amount=input_amount,
        output_amount=output_amount,
        quantity_base=input_amount,
        average_price=average_price,
        gross_quote=gross_quote,
        fee_quote=fee_quote,
    )


def _simulate_route(
    *,
    exchange: ExchangeConfig,
    route: TriangleRouteConfig,
    plan: list[TriangleLegPlan],
    books: dict[tuple[str, str], OrderBookSnapshot],
    notional_quote: float,
) -> tuple[float, list[TriangleLegFill]] | None:
    amount = notional_quote
    fills: list[TriangleLegFill] = []
    for leg in plan:
        book = books.get((route.exchange, leg.symbol))
        if book is None:
            return None
        fill = _simulate_leg(
            leg,
            book,
            input_amount=amount,
            fee_bps=exchange.fee_bps,
        )
        if fill is None:
            return None
        fills.append(fill)
        amount = fill.output_amount
    return amount, fills


def _fees_by_currency(fills: list[TriangleLegFill]) -> dict[str, float]:
    fees: dict[str, float] = {}
    for fill in fills:
        fee_currency = _split_symbol(fill.plan.symbol)[1]
        if not fee_currency:
            continue
        fees[fee_currency] = fees.get(fee_currency, 0.0) + fill.fee_quote
    return {currency: amount for currency, amount in sorted(fees.items())}


def find_triangular_arbitrage_opportunities(
    *,
    books: dict[tuple[str, str], OrderBookSnapshot],
    exchanges: list[ExchangeConfig],
    cfg: TriangularArbitrageConfig,
) -> list[Opportunity]:
    exchange_by_key = {exchange.key: exchange for exchange in exchanges}
    opportunities: list[Opportunity] = []
    if cfg.notional_quote <= 0:
        return opportunities

    for route in cfg.routes:
        exchange = exchange_by_key.get(route.exchange)
        if exchange is None:
            continue
        for plan in _route_plans(route):
            simulated = _simulate_route(
                exchange=exchange,
                route=route,
                plan=plan,
                books=books,
                notional_quote=cfg.notional_quote,
            )
            if simulated is None:
                continue
            final_amount, fills = simulated
            profit_quote = final_amount - cfg.notional_quote
            profit_bps = profit_quote / cfg.notional_quote * 10_000
            if (
                profit_quote < cfg.min_profit_quote
                or profit_bps < cfg.min_profit_bps
            ):
                continue

            opportunities.append(
                Opportunity(
                    strategy="triangular-arbitrage",
                    profit_quote=profit_quote,
                    profit_bps=profit_bps,
                    legs=[
                        OpportunityLeg(
                            exchange=route.exchange,
                            symbol=fill.plan.symbol,
                            side=fill.plan.side,
                            quantity_base=fill.quantity_base,
                            average_price=fill.average_price,
                            fee_quote=fill.fee_quote,
                            quote_currency=_split_symbol(fill.plan.symbol)[1],
                            gross_quote=fill.gross_quote,
                            net_quote=(
                                fill.gross_quote + fill.fee_quote
                                if fill.plan.side == "buy"
                                else fill.gross_quote - fill.fee_quote
                            ),
                        )
                        for fill in fills
                    ],
                    metadata={
                        "exchange": route.exchange,
                        "route_label": route.label,
                        "start_currency": route.start_currency.upper(),
                        "start_amount": cfg.notional_quote,
                        "final_amount": final_amount,
                        "fees_by_currency": _fees_by_currency(fills),
                        "requires_cross_exchange_transfer": False,
                        "requires_prefunded_inventory": True,
                        "execution_risk": "three-leg atomicity is not guaranteed",
                        "path": [
                            {
                                "symbol": fill.plan.symbol,
                                "side": fill.plan.side,
                                "input_currency": fill.plan.input_currency,
                                "input_amount": fill.input_amount,
                                "output_currency": fill.plan.output_currency,
                                "output_amount": fill.output_amount,
                            }
                            for fill in fills
                        ],
                    },
                )
            )

    opportunities.sort(key=lambda item: item.profit_bps, reverse=True)
    return opportunities
