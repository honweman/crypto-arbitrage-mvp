from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from arbitrage_bot.config import ExchangeConfig, SpotMarketConfig
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


def find_converted_spot_spread_opportunities(
    books: dict[tuple[str, str], OrderBookSnapshot],
    exchanges: Iterable[ExchangeConfig],
    markets: Iterable[SpotMarketConfig],
    notional_quote: float,
    min_profit_quote: float,
    min_profit_bps: float,
    quote_rates: dict[str, float],
    common_quote_currency: str = "USD",
) -> list[Opportunity]:
    exchange_by_key = {exchange.key: exchange for exchange in exchanges}
    markets_by_asset: dict[str, list[SpotMarketConfig]] = defaultdict(list)
    for market in markets:
        markets_by_asset[market.asset].append(market)

    opportunities: list[Opportunity] = []

    for asset, asset_markets in markets_by_asset.items():
        for buy_market in asset_markets:
            buy_cfg = exchange_by_key.get(buy_market.exchange)
            buy_rate = quote_rates.get(buy_market.quote_currency.upper())
            buy_book = books.get((buy_market.exchange, buy_market.symbol))
            if buy_cfg is None or buy_rate is None or buy_rate <= 0:
                continue
            if buy_book is None or not buy_book.asks:
                continue

            buy_budget_local_quote = notional_quote / buy_rate
            buy_capacity = max_base_for_quote(
                buy_book.asks,
                buy_budget_local_quote,
            )

            for sell_market in asset_markets:
                if buy_market.exchange == sell_market.exchange:
                    continue

                sell_cfg = exchange_by_key.get(sell_market.exchange)
                sell_rate = quote_rates.get(sell_market.quote_currency.upper())
                sell_book = books.get((sell_market.exchange, sell_market.symbol))
                if sell_cfg is None or sell_rate is None or sell_rate <= 0:
                    continue
                if sell_book is None or not sell_book.bids:
                    continue

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

                buy_common_quote = buy_fill.net_quote * buy_rate
                sell_common_quote = sell_fill.net_quote * sell_rate
                profit_quote = sell_common_quote - buy_common_quote
                profit_bps = profit_quote / buy_common_quote * 10_000

                if profit_quote < min_profit_quote or profit_bps < min_profit_bps:
                    continue

                opportunities.append(
                    Opportunity(
                        strategy="spot-spread",
                        profit_quote=profit_quote,
                        profit_bps=profit_bps,
                        legs=[
                            OpportunityLeg(
                                exchange=buy_market.exchange,
                                symbol=buy_market.symbol,
                                side="buy",
                                quantity_base=quantity_base,
                                average_price=buy_fill.average_price,
                                fee_quote=buy_fill.fee_quote,
                                quote_currency=buy_market.quote_currency,
                                gross_quote=buy_fill.gross_quote,
                                net_quote=buy_fill.net_quote,
                                common_quote_rate=buy_rate,
                            ),
                            OpportunityLeg(
                                exchange=sell_market.exchange,
                                symbol=sell_market.symbol,
                                side="sell",
                                quantity_base=quantity_base,
                                average_price=sell_fill.average_price,
                                fee_quote=sell_fill.fee_quote,
                                quote_currency=sell_market.quote_currency,
                                gross_quote=sell_fill.gross_quote,
                                net_quote=sell_fill.net_quote,
                                common_quote_rate=sell_rate,
                            ),
                        ],
                        metadata={
                            "asset": asset,
                            "common_quote_currency": common_quote_currency,
                            "buy_common_quote": buy_common_quote,
                            "sell_common_quote": sell_common_quote,
                            "buy_quote_currency": buy_market.quote_currency,
                            "sell_quote_currency": sell_market.quote_currency,
                            "requires_prefunded_inventory": True,
                            "requires_fx_reconciliation": True,
                        },
                    )
                )

    opportunities.sort(key=lambda item: item.profit_bps, reverse=True)
    return opportunities
