import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.solana import (
    aggregate_largest_token_accounts_by_owner,
    load_cached_holder_snapshot,
    update_holder_history,
)


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

    def test_holder_history_persists_cumulative_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "onchain_holder_changes.json"
            holders = [
                {
                    "owner": "wallet-1",
                    "rank": 1,
                    "amount": 100.0,
                    "token_account_count": 1,
                }
            ]

            first = update_holder_history(
                path=path,
                mint="mint-1",
                label="TOKEN",
                holders=holders,
                address_labels={"wallet-1": "Exchange"},
                observed_at=1000.0,
            )

            self.assertEqual(first["event_count"], 0)
            self.assertEqual(holders[0]["cumulative_delta_amount"], 0.0)

            holders = [
                {
                    "owner": "wallet-1",
                    "rank": 1,
                    "amount": 75.0,
                    "token_account_count": 1,
                }
            ]
            second = update_holder_history(
                path=path,
                mint="mint-1",
                label="TOKEN",
                holders=holders,
                address_labels={"wallet-1": "Exchange"},
                observed_at=1060.0,
            )

            self.assertEqual(second["event_count"], 1)
            self.assertEqual(second["recent_events"][0]["delta_amount"], -25.0)
            self.assertEqual(holders[0]["cumulative_delta_amount"], -25.0)

            holders = [
                {
                    "owner": "wallet-1",
                    "rank": 1,
                    "amount": 50.0,
                    "token_account_count": 1,
                }
            ]
            third = update_holder_history(
                path=path,
                mint="mint-1",
                label="TOKEN",
                holders=holders,
                address_labels={"wallet-1": "Exchange"},
                observed_at=1120.0,
            )

            self.assertEqual(third["event_count"], 2)
            self.assertEqual(third["recent_events"][0]["delta_amount"], -25.0)
            self.assertEqual(holders[0]["cumulative_delta_amount"], -50.0)
            self.assertEqual(third["baseline_at"], 1000.0)

    def test_holder_history_loads_cached_latest_holders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "onchain_holder_changes.json"
            holders = [
                {
                    "owner": "wallet-1",
                    "rank": 1,
                    "amount": 100.0,
                    "token_account_count": 2,
                    "share_pct": 10.0,
                }
            ]

            update_holder_history(
                path=path,
                mint="mint-1",
                label="TOKEN",
                holders=holders,
                address_labels={"wallet-1": "Exchange"},
                observed_at=1000.0,
            )
            snapshot = load_cached_holder_snapshot(
                path=path,
                mint="mint-1",
                label="TOKEN",
                address_labels={"wallet-1": "Exchange"},
                top_n=20,
            )

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot["status"], "cached")
            self.assertEqual(snapshot["last_finished"], 1000.0)
            self.assertEqual(snapshot["holders"][0]["owner"], "wallet-1")
            self.assertEqual(snapshot["holders"][0]["label"], "Exchange")
            self.assertEqual(snapshot["holders"][0]["token_account_count"], 2)

    def test_holder_history_reconstructs_cache_from_legacy_amounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "onchain_holder_changes.json"
            path.write_text(
                """{
  "version": 1,
  "mint": "mint-1",
  "label": "TOKEN",
  "baseline_at": 1000.0,
  "updated_at": 1120.0,
  "baseline_amounts": {"wallet-1": 100.0},
  "latest_amounts": {"wallet-1": 75.0},
  "latest_ranks": {"wallet-1": 1},
  "events": []
}""",
                encoding="utf-8",
            )

            snapshot = load_cached_holder_snapshot(
                path=path,
                mint="mint-1",
                label="TOKEN",
                address_labels={"wallet-1": "Exchange"},
                top_n=20,
            )

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot["holders"][0]["owner"], "wallet-1")
            self.assertEqual(snapshot["holders"][0]["amount"], 75.0)
            self.assertEqual(snapshot["holders"][0]["rank"], 1)
            self.assertEqual(
                snapshot["holders"][0]["cumulative_delta_amount"],
                -25.0,
            )


if __name__ == "__main__":
    unittest.main()
