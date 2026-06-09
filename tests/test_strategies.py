import unittest

from arbitrage_bot.config import CashAndCarryPair, ExchangeConfig, SpotMarketConfig
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.strategies.cash_and_carry import find_cash_and_carry_opportunities
from arbitrage_bot.strategies.spot_spread import (
    find_converted_spot_spread_opportunities,
    find_spot_spread_opportunities,
)


def book(exchange: str, symbol: str, bid: float, ask: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange=exchange,
        symbol=symbol,
        bids=[BookLevel(price=bid, amount=10)],
        asks=[BookLevel(price=ask, amount=10)],
    )


class StrategyTest(unittest.TestCase):
    def test_spot_spread_finds_profitable_cross_exchange_edge(self) -> None:
        exchanges = [
            ExchangeConfig(id="a", label="a", fee_bps=10),
            ExchangeConfig(id="b", label="b", fee_bps=10),
        ]
        books = {
            ("a", "BTC/USDT"): book("a", "BTC/USDT", bid=99, ask=100),
            ("b", "BTC/USDT"): book("b", "BTC/USDT", bid=103, ask=104),
        }

        opportunities = find_spot_spread_opportunities(
            books=books,
            exchanges=exchanges,
            symbols=["BTC/USDT"],
            notional_quote=1000,
            min_profit_quote=1,
            min_profit_bps=1,
        )

        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].legs[0].exchange, "a")
        self.assertEqual(opportunities[0].legs[1].exchange, "b")
        self.assertGreater(opportunities[0].profit_quote, 0)

    def test_cash_and_carry_finds_positive_basis(self) -> None:
        spot_exchanges = [ExchangeConfig(id="spot", label="spot", fee_bps=10)]
        derivative_exchanges = [
            ExchangeConfig(id="perp", label="perp", market_type="swap", fee_bps=5)
        ]
        spot_books = {
            ("spot", "BTC/USDT"): book("spot", "BTC/USDT", bid=99, ask=100),
        }
        derivative_books = {
            ("perp", "BTC/USDT:USDT"): book(
                "perp",
                "BTC/USDT:USDT",
                bid=103,
                ask=104,
            ),
        }

        opportunities = find_cash_and_carry_opportunities(
            spot_books=spot_books,
            derivative_books=derivative_books,
            spot_exchanges=spot_exchanges,
            derivative_exchanges=derivative_exchanges,
            pairs=[
                CashAndCarryPair(
                    spot_symbol="BTC/USDT",
                    derivative_symbol="BTC/USDT:USDT",
                )
            ],
            notional_quote=1000,
            min_profit_quote=1,
            min_basis_bps=1,
        )

        self.assertEqual(len(opportunities), 1)
        self.assertGreater(opportunities[0].metadata["basis_bps"], 0)

    def test_converted_spot_spread_compares_different_quote_currencies(self) -> None:
        exchanges = [
            ExchangeConfig(id="bithumb", label="bithumb-spot", fee_bps=0),
            ExchangeConfig(id="coinbase", label="coinbase-spot", fee_bps=0),
        ]
        markets = [
            SpotMarketConfig(
                asset="ACS",
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                quote_currency="KRW",
            ),
            SpotMarketConfig(
                asset="ACS",
                exchange="coinbase-spot",
                symbol="ACS/USD",
                quote_currency="USD",
            ),
        ]
        books = {
            ("bithumb-spot", "ACS/KRW"): OrderBookSnapshot(
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                bids=[BookLevel(price=0.19, amount=1_000_000)],
                asks=[BookLevel(price=0.20, amount=1_000_000)],
            ),
            ("coinbase-spot", "ACS/USD"): OrderBookSnapshot(
                exchange="coinbase-spot",
                symbol="ACS/USD",
                bids=[BookLevel(price=0.00018, amount=1_000_000)],
                asks=[BookLevel(price=0.00019, amount=1_000_000)],
            ),
        }

        opportunities = find_converted_spot_spread_opportunities(
            books=books,
            exchanges=exchanges,
            markets=markets,
            notional_quote=100,
            min_profit_quote=1,
            min_profit_bps=1,
            quote_rates={"USD": 1.0, "KRW": 0.00075},
            common_quote_currency="USD",
        )

        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].legs[0].symbol, "ACS/KRW")
        self.assertEqual(opportunities[0].legs[0].quote_currency, "KRW")
        self.assertEqual(opportunities[0].legs[1].symbol, "ACS/USD")
        self.assertEqual(opportunities[0].metadata["common_quote_currency"], "USD")
        self.assertGreater(opportunities[0].profit_quote, 1)


if __name__ == "__main__":
    unittest.main()
