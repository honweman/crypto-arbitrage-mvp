import unittest

from arbitrage_bot.order_validation import (
    summarize_order_validations,
    validate_prepared_limit_order,
)


class OrderValidationTest(unittest.TestCase):
    def test_rejects_order_below_exchange_minimum_cost(self) -> None:
        result = validate_prepared_limit_order(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            side="buy",
            requested_amount=1_000.0,
            requested_price=0.00015,
            amount=1_000.0,
            price=0.00015,
            market={
                "limits": {
                    "amount": {"min": 1.0},
                    "cost": {"min": 1.0},
                },
                "precision": {
                    "amount": 1.0,
                    "price": 0.0000001,
                },
            },
        )

        self.assertEqual(result["status"], "error")
        self.assertTrue(any("cost" in error for error in result["errors"]))
        self.assertEqual(result["limits"]["cost"]["min"], 1.0)

    def test_reports_precision_rounding_warnings(self) -> None:
        result = validate_prepared_limit_order(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="sell",
            requested_amount=1000.1234,
            requested_price=0.00015004,
            amount=1000.0,
            price=0.00015,
            market={
                "limits": {
                    "amount": {"min": 1.0},
                    "cost": {"min": 0.1},
                },
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["warnings"]), 2)

    def test_summarizes_batch_errors(self) -> None:
        first = validate_prepared_limit_order(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            side="buy",
            requested_amount=1.0,
            requested_price=0.00015,
            amount=1.0,
            price=0.00015,
            market={"limits": {"cost": {"min": 1.0}}},
        )
        second = validate_prepared_limit_order(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            side="sell",
            requested_amount=10_000.0,
            requested_price=0.00015,
            amount=10_000.0,
            price=0.00015,
            market={"limits": {"cost": {"min": 1.0}}},
        )

        summary = summarize_order_validations([first, second])

        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["order_count"], 2)
        self.assertEqual(summary["error_count"], 1)
        self.assertAlmostEqual(summary["total_cost"], 1.50015)


if __name__ == "__main__":
    unittest.main()
