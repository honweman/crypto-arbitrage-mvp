import unittest

from arbitrage_bot.config import CashAndCarryPair, ExchangeConfig
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.strategies.cash_and_carry import find_cash_and_carry_opportunities
from arbitrage_bot.strategies.spot_spread import find_spot_spread_opportunities


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


if __name__ == "__main__":
    unittest.main()
