import unittest

from arbitrage_bot.solana import aggregate_largest_token_accounts_by_owner


class SolanaAggregationTest(unittest.TestCase):
    def test_aggregate_largest_token_accounts_by_owner(self) -> None:
        largest_accounts = [
            {"address": "token-account-a", "uiAmountString": "100.5"},
            {"address": "token-account-b", "uiAmountString": "50"},
            {"address": "token-account-c", "uiAmountString": "25"},
        ]
        account_infos = {
            "token-account-a": {
                "data": {"parsed": {"info": {"owner": "wallet-1"}}}
            },
            "token-account-b": {
                "data": {"parsed": {"info": {"owner": "wallet-2"}}}
            },
            "token-account-c": {
                "data": {"parsed": {"info": {"owner": "wallet-1"}}}
            },
        }

        rows = aggregate_largest_token_accounts_by_owner(
            largest_accounts,
            account_infos,
            top_n=2,
            total_supply_ui=1000,
        )

        self.assertEqual(rows[0]["owner"], "wallet-1")
        self.assertEqual(rows[0]["amount"], 125.5)
        self.assertEqual(rows[0]["token_account_count"], 2)
        self.assertAlmostEqual(rows[0]["share_pct"], 12.55)
        self.assertEqual(rows[1]["owner"], "wallet-2")


if __name__ == "__main__":
    unittest.main()
