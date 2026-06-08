from __future__ import annotations

from collections.abc import Iterable

from arbitrage_bot.config import CashAndCarryPair, ExchangeConfig
from arbitrage_bot.models import Opportunity, OpportunityLeg, OrderBookSnapshot
from arbitrage_bot.orderbook import available_base, estimate_fill, max_base_for_quote


def find_cash_and_carry_opportunities(
    spot_books: dict[tuple[str, str], OrderBookSnapshot],
    derivative_books: dict[tuple[str, str], OrderBookSnapshot],
    spot_exchanges: Iterable[ExchangeConfig],
    derivative_exchanges: Iterable[ExchangeConfig],
    pairs: Iterable[CashAndCarryPair],
    notional_quote: float,
    min_profit_quote: float,
    min_basis_bps: float,
    funding_rates: dict[tuple[str, str], float] | None = None,
) -> list[Opportunity]:
    spot_by_key = {exchange.key: exchange for exchange in spot_exchanges}
    derivative_by_key = {exchange.key: exchange for exchange in derivative_exchanges}
    funding_rates = funding_rates or {}
    opportunities: list[Opportunity] = []

    for pair in pairs:
        for spot_key, spot_cfg in spot_by_key.items():
            spot_book = spot_books.get((spot_key, pair.spot_symbol))
            if spot_book is None or not spot_book.asks:
                continue

            for derivative_key, derivative_cfg in derivative_by_key.items():
                derivative_book = derivative_books.get(
                    (derivative_key, pair.derivative_symbol)
                )
                if derivative_book is None or not derivative_book.bids:
                    continue

                spot_capacity = max_base_for_quote(spot_book.asks, notional_quote)
                derivative_capacity = available_base(derivative_book.bids)
                quantity_base = min(spot_capacity, derivative_capacity)
                if quantity_base <= 0:
                    continue

                spot_fill = estimate_fill(
                    spot_book.asks,
                    side="buy",
                    quantity_base=quantity_base,
                    fee_bps=spot_cfg.fee_bps,
                )
                derivative_fill = estimate_fill(
                    derivative_book.bids,
                    side="sell",
                    quantity_base=quantity_base,
                    fee_bps=derivative_cfg.fee_bps,
                )
                if spot_fill is None or derivative_fill is None:
                    continue

                basis_bps = (
                    (derivative_fill.average_price - spot_fill.average_price)
                    / spot_fill.average_price
                    * 10_000
                )
                entry_edge_quote = derivative_fill.net_quote - spot_fill.net_quote

                if basis_bps < min_basis_bps or entry_edge_quote < min_profit_quote:
                    continue

                funding_rate = funding_rates.get((derivative_key, pair.derivative_symbol))
                opportunities.append(
                    Opportunity(
                        strategy="cash-and-carry",
                        profit_quote=entry_edge_quote,
                        profit_bps=entry_edge_quote / spot_fill.net_quote * 10_000,
                        legs=[
                            OpportunityLeg(
                                exchange=spot_key,
                                symbol=pair.spot_symbol,
                                side="buy",
                                quantity_base=quantity_base,
                                average_price=spot_fill.average_price,
                                fee_quote=spot_fill.fee_quote,
                            ),
                            OpportunityLeg(
                                exchange=derivative_key,
                                symbol=pair.derivative_symbol,
                                side="sell",
                                quantity_base=quantity_base,
                                average_price=derivative_fill.average_price,
                                fee_quote=derivative_fill.fee_quote,
                            ),
                        ],
                        metadata={
                            "basis_bps": basis_bps,
                            "entry_edge_quote": entry_edge_quote,
                            "funding_rate": funding_rate,
                            "requires_margin_controls": True,
                        },
                    )
                )

    opportunities.sort(key=lambda item: item.profit_bps, reverse=True)
    return opportunities
