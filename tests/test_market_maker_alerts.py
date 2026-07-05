import unittest

from arbitrage_bot.web.market_maker_alerts import market_maker_problem_warnings


class MarketMakerAlertsTest(unittest.TestCase):
    def test_problem_warnings_include_only_actionable_statuses(self) -> None:
        warnings = market_maker_problem_warnings(
            {
                "instances": [
                    {
                        "id": "coinbase-acs",
                        "display_name": "coinbase-spot ACS/USDC",
                        "status": "cancel_retry",
                        "status_reason": "cancel failed",
                    },
                    {
                        "id": "upbit-acs",
                        "display_name": "upbit-spot ACS/USDT",
                        "status": "unchanged",
                    },
                ]
            }
        )

        self.assertEqual(
            warnings,
            [
                (
                    "Market maker coinbase-spot ACS/USDC: "
                    "cancel_retry (cancel failed)"
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
