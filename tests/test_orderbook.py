import unittest

from arbitrage_bot.models import BookLevel
from arbitrage_bot.orderbook import estimate_fill, max_base_for_quote


class OrderBookTest(unittest.TestCase):
    def test_estimate_fill_uses_depth_weighted_average(self) -> None:
        levels = [BookLevel(price=100, amount=1), BookLevel(price=110, amount=2)]

        fill = estimate_fill(levels, side="buy", quantity_base=2, fee_bps=10)

        self.assertIsNotNone(fill)
        assert fill is not None
        self.assertEqual(fill.gross_quote, 210)
        self.assertEqual(fill.average_price, 105)
        self.assertEqual(fill.fee_quote, 0.21)
        self.assertEqual(fill.net_quote, 210.21)

    def test_estimate_fill_returns_none_when_depth_is_insufficient(self) -> None:
        levels = [BookLevel(price=100, amount=1)]

        self.assertIsNone(
            estimate_fill(levels, side="sell", quantity_base=2, fee_bps=10)
        )

    def test_max_base_for_quote_handles_partial_level(self) -> None:
        levels = [BookLevel(price=100, amount=1), BookLevel(price=200, amount=2)]

        self.assertEqual(max_base_for_quote(levels, quote_budget=300), 2)


if __name__ == "__main__":
    unittest.main()
