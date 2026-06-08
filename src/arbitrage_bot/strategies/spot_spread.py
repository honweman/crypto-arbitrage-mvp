from __future__ import annotations

from collections.abc import Iterable

from arbitrage_bot.config import ExchangeConfig
from arbitrage_bot.models import Opportunity, OpportunityLeg, OrderBookSnapshot
from arbitrage_bot.orderbook import available_base, estimate_fill, max_base_for_quote


def find_spot_spread_opportunities(
    books: dict[tuple[str, str], OrderBookSnapshot],
    exchanges: Iterable[ExchangeConfig],
    symbols: Iterable[str],
    notional_quote: float,
    min_profit_quote: float,
    min_profit_bps: float,
) -> list[Opportunity]:
    exchange_by_key = {exchange.key: exchange for exchange in exchanges}
    opportunities: list[Opportunity] = []

    for symbol in symbols:
        for buy_key, buy_cfg in exchange_by_key.items():
            buy_book = books.get((buy_key, symbol))
            if buy_book is None or not buy_book.asks:
                continue

            for sell_key, sell_cfg in exchange_by_key.items():
                if buy_key == sell_key:
                    continue

                sell_book = books.get((sell_key, symbol))
                if sell_book is None or not sell_book.bids:
                    continue

                buy_capacity = max_base_for_quote(buy_book.asks, notional_quote)
                sell_capacity = available_base(sell_book.bids)
                quantity_base = min(buy_capacity, sell_capacity)
                if quantity_base <= 0:
                    continue

                buy_fill = estimate_fill(
                    buy_book.asks,
                    side="buy",
                    quantity_base=quantity_base,
                    fee_bps=buy_cfg.fee_bps,
                )
                sell_fill = estimate_fill(
                    sell_book.bids,
                    side="sell",
                    quantity_base=quantity_base,
                    fee_bps=sell_cfg.fee_bps,
                )
                if buy_fill is None or sell_fill is None:
                    continue

                profit_quote = sell_fill.net_quote - buy_fill.net_quote
                profit_bps = profit_quote / buy_fill.net_quote * 10_000

                if profit_quote < min_profit_quote or profit_bps < min_profit_bps:
                    continue

                opportunities.append(
                    Opportunity(
                        strategy="spot-spread",
                        profit_quote=profit_quote,
                        profit_bps=profit_bps,
                        legs=[
                            OpportunityLeg(
                                exchange=buy_key,
                                symbol=symbol,
                                side="buy",
                                quantity_base=quantity_base,
                                average_price=buy_fill.average_price,
                                fee_quote=buy_fill.fee_quote,
                            ),
                            OpportunityLeg(
                                exchange=sell_key,
                                symbol=symbol,
                                side="sell",
                                quantity_base=quantity_base,
                                average_price=sell_fill.average_price,
                                fee_quote=sell_fill.fee_quote,
                            ),
                        ],
                        metadata={
                            "buy_gross_quote": buy_fill.gross_quote,
                            "sell_gross_quote": sell_fill.gross_quote,
                            "requires_prefunded_inventory": True,
                        },
                    )
                )

    opportunities.sort(key=lambda item: item.profit_bps, reverse=True)
    return opportunities
