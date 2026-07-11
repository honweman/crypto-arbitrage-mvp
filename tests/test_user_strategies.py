from __future__ import annotations

import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.user_strategies import (
    UserStrategy,
    strategy_parameter_blockers,
    user_strategy_catalog,
)
from arbitrage_bot.user_workspace import (
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)


MASTER_KEY = base64.urlsafe_b64encode(b"s" * 32).decode("ascii").rstrip("=")


class UserStrategyTest(unittest.TestCase):
    def _store(self, path: Path) -> UserWorkspaceStore:
        return UserWorkspaceStore(
            path,
            master_key_env="TEST_USER_STRATEGY_MASTER_KEY",
        )

    def _project(self, store: UserWorkspaceStore) -> UserProject:
        return store.upsert_project(
            UserProject.from_dict(
                {
                    "id": "project-acs",
                    "owner_email": "trader@example.com",
                    "name": "ACS Trading",
                    "asset": "ACS",
                    "quote_currency": "USDC",
                    "status": "active",
                }
            )
        )

    def _account(
        self,
        store: UserWorkspaceStore,
        project: UserProject,
        *,
        account_id: str,
        exchange: str,
        symbol: str,
    ) -> UserExchangeAccount:
        account = store.upsert_account(
            UserExchangeAccount.from_dict(
                {
                    "id": account_id,
                    "owner_email": project.owner_email,
                    "project_id": project.id,
                    "label": account_id,
                    "exchange": exchange,
                    "symbol": symbol,
                    "enabled": False,
                    "withdrawal_disabled_confirmed": True,
                }
            ),
            credentials={"api_key": f"key-{account_id}", "secret": f"secret-{account_id}"},
        )
        account = store.update_account_connection(account.id, status="healthy")
        return store.upsert_account(
            UserExchangeAccount.from_dict({**account.to_dict(), "enabled": True})
        )

    def test_user_strategy_is_paper_only_and_rejects_secret_fields(self) -> None:
        base = {
            "owner_email": "trader@example.com",
            "project_id": "project-acs",
            "name": "ACS MM",
            "strategy_type": "market_maker",
        }
        strategy = UserStrategy.from_dict(base)

        self.assertEqual(strategy.mode, "paper")
        self.assertFalse(strategy.to_dict()["live_enabled"])
        self.assertEqual(strategy.parameters["levels"], 2)
        self.assertEqual(len(user_strategy_catalog()), 5)
        with self.assertRaisesRegex(ValueError, "paper-only"):
            UserStrategy.from_dict({**base, "live_enabled": True})
        with self.assertRaisesRegex(ValueError, "credential values"):
            UserStrategy.from_dict(
                {**base, "parameters": {"api_key": "must-not-be-stored"}}
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            UserStrategy.from_dict(
                {**base, "risk": {"max_order_quote": float("nan")}}
            )
        with self.assertRaisesRegex(ValueError, "enabled must be true or false"):
            UserStrategy.from_dict({**base, "enabled": "false"})
        with self.assertRaisesRegex(ValueError, "post_only must be true or false"):
            UserStrategy.from_dict(
                {**base, "parameters": {"post_only": "false"}}
            )
        with self.assertRaisesRegex(ValueError, "account_ids must be a list"):
            UserStrategy.from_dict({**base, "account_ids": "coinbase-main"})
        with self.assertRaisesRegex(ValueError, "parameters must be an object"):
            UserStrategy.from_dict({**base, "parameters": []})
        with self.assertRaisesRegex(ValueError, "risk must be an object"):
            UserStrategy.from_dict({**base, "risk": []})

    def test_parameter_budget_must_fit_strategy_risk(self) -> None:
        strategy = UserStrategy.from_dict(
            {
                "owner_email": "trader@example.com",
                "project_id": "project-acs",
                "strategy_type": "market_maker",
                "parameters": {"levels": 10, "quote_per_level": 10},
                "risk": {"max_order_quote": 5, "max_total_quote": 50},
            }
        )

        blockers = strategy_parameter_blockers(strategy)

        self.assertIn("strategy order size exceeds max order quote", blockers)
        self.assertIn("strategy budget exceeds max total quote", blockers)

        too_many_orders = UserStrategy.from_dict(
            {
                "owner_email": "trader@example.com",
                "project_id": "project-acs",
                "strategy_type": "spot_grid",
                "parameters": {
                    "lower_price": 0.1,
                    "upper_price": 0.2,
                    "grid_count": 20,
                },
                "risk": {"max_open_orders": 10},
            }
        )
        self.assertIn(
            "strategy planned orders exceed max open orders",
            strategy_parameter_blockers(too_many_orders),
        )

    def test_store_builds_ready_paper_strategy_and_disables_it_on_account_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            store = self._store(Path(tmp) / "workspace.sqlite3")
            project = self._project(store)
            account = self._account(
                store,
                project,
                account_id="coinbase-main",
                exchange="coinbase",
                symbol="ACS/USDC",
            )
            strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "strategy-mm",
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "name": "ACS Coinbase MM",
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                        "enabled": True,
                    }
                )
            )
            ready_payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )
            store.update_account_connection(
                account.id,
                status="error",
                error="authentication failed",
            )
            stopped_payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )

        ready = ready_payload["strategies"][0]
        stopped = stopped_payload["strategies"][0]
        self.assertEqual(strategy.mode, "paper")
        self.assertTrue(ready["readiness"]["ready"])
        self.assertTrue(ready["effective_enabled"])
        self.assertEqual(ready["status"], "paper_ready")
        self.assertFalse(ready["readiness"]["live_submit_allowed"])
        self.assertFalse(stopped["enabled"])
        self.assertEqual(stopped["status"], "paused")
        self.assertNotIn("key-coinbase-main", str(ready_payload))
        self.assertNotIn("secret-coinbase-main", str(ready_payload))

    def test_spot_arbitrage_requires_two_distinct_exchange_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            store = self._store(Path(tmp) / "workspace.sqlite3")
            project = self._project(store)
            coinbase = self._account(
                store,
                project,
                account_id="coinbase-main",
                exchange="coinbase",
                symbol="ACS/USDC",
            )
            bybit = self._account(
                store,
                project,
                account_id="bybit-main",
                exchange="bybit",
                symbol="ACS/USDT",
            )
            one_account = UserStrategy.from_dict(
                {
                    "owner_email": project.owner_email,
                    "project_id": project.id,
                    "strategy_type": "spot_spread",
                    "account_ids": [coinbase.id],
                }
            )
            two_accounts = UserStrategy.from_dict(
                {
                    "owner_email": project.owner_email,
                    "project_id": project.id,
                    "strategy_type": "spot_spread",
                    "account_ids": [coinbase.id, bybit.id],
                }
            )

            one_readiness = store.strategy_readiness(one_account)
            two_readiness = store.strategy_readiness(two_accounts)

        self.assertFalse(one_readiness["ready"])
        self.assertTrue(any("at least 2" in row for row in one_readiness["blockers"]))
        self.assertTrue(two_readiness["ready"])

    def test_referenced_account_cannot_be_deleted_and_project_disable_pauses_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            store = self._store(Path(tmp) / "workspace.sqlite3")
            project = self._project(store)
            account = self._account(
                store,
                project,
                account_id="coinbase-main",
                exchange="coinbase",
                symbol="ACS/USDC",
            )
            strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                        "enabled": True,
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "strategies using this account"):
                store.delete_account(account.id)
            store.set_project_status(project.id, "disabled")
            disabled = store.get_strategy(strategy.id)

        self.assertIsNotNone(disabled)
        self.assertFalse(disabled.enabled)

    def test_strategy_listing_is_scoped_by_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            path = Path(tmp) / "workspace.sqlite3"
            store = self._store(path)
            first_project = self._project(store)
            second_project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "id": "project-btc",
                        "owner_email": "other@example.com",
                        "asset": "BTC",
                        "quote_currency": "USDT",
                        "status": "active",
                    }
                )
            )
            store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "strategy-first",
                        "owner_email": first_project.owner_email,
                        "project_id": first_project.id,
                        "strategy_type": "market_maker",
                    }
                )
            )
            store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "strategy-second",
                        "owner_email": second_project.owner_email,
                        "project_id": second_project.id,
                        "strategy_type": "market_maker",
                    }
                )
            )

            first_rows = store.list_strategies(
                owner_email=first_project.owner_email,
                is_admin=False,
            )
            all_rows = self._store(path).list_strategies(
                owner_email=first_project.owner_email,
                is_admin=True,
            )

        self.assertEqual([row.id for row in first_rows], ["strategy-first"])
        self.assertEqual({row.id for row in all_rows}, {"strategy-first", "strategy-second"})

    def test_existing_ids_cannot_be_taken_over_by_another_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            store = self._store(Path(tmp) / "workspace.sqlite3")
            first_project = self._project(store)
            first_account = self._account(
                store,
                first_project,
                account_id="shared-account-id",
                exchange="coinbase",
                symbol="ACS/USDC",
            )
            first_strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "shared-strategy-id",
                        "owner_email": first_project.owner_email,
                        "project_id": first_project.id,
                        "strategy_type": "market_maker",
                        "account_ids": [first_account.id],
                    }
                )
            )
            second_project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "id": "project-other",
                        "owner_email": "other@example.com",
                        "asset": "BTC",
                        "quote_currency": "USDT",
                        "status": "active",
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "project owner cannot be changed"):
                store.upsert_project(
                    UserProject.from_dict(
                        {
                            "id": first_project.id,
                            "owner_email": second_project.owner_email,
                            "asset": "BTC",
                            "quote_currency": "USDT",
                        }
                    )
                )
            with self.assertRaisesRegex(
                ValueError,
                "exchange account owner cannot be changed",
            ):
                store.upsert_account(
                    UserExchangeAccount.from_dict(
                        {
                            "id": first_account.id,
                            "owner_email": second_project.owner_email,
                            "project_id": second_project.id,
                            "exchange": "coinbase",
                            "symbol": "BTC/USDT",
                        }
                    )
                )
            with self.assertRaisesRegex(ValueError, "strategy owner cannot be changed"):
                store.upsert_strategy(
                    UserStrategy.from_dict(
                        {
                            "id": first_strategy.id,
                            "owner_email": second_project.owner_email,
                            "project_id": second_project.id,
                            "strategy_type": "market_maker",
                        }
                    )
                )

    def test_project_scope_and_credential_changes_pause_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            store = self._store(Path(tmp) / "workspace.sqlite3")
            project = self._project(store)
            account = self._account(
                store,
                project,
                account_id="coinbase-main",
                exchange="coinbase",
                symbol="ACS/USDC",
            )
            strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "strategy-mm",
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                        "enabled": True,
                    }
                )
            )

            changed_account = store.upsert_account(
                account,
                credentials={"secret": "rotated-secret"},
            )
            paused_after_secret = store.get_strategy(strategy.id)

            self.assertFalse(changed_account.enabled)
            self.assertEqual(changed_account.connection_status, "unverified")
            self.assertIsNotNone(paused_after_secret)
            self.assertFalse(paused_after_secret.enabled)

            retested_account = store.update_account_connection(
                changed_account.id,
                status="healthy",
            )
            reenabled_account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {**retested_account.to_dict(), "enabled": True}
                )
            )
            resumed_strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {**paused_after_secret.to_dict(), "enabled": True}
                )
            )
            self.assertTrue(reenabled_account.enabled)
            self.assertTrue(resumed_strategy.enabled)

            changed_project = store.upsert_project(
                UserProject.from_dict(
                    {
                        **project.to_dict(),
                        "asset": "BTC",
                        "quote_currency": "USDT",
                        "status": "active",
                    }
                )
            )
            paused_account = store.get_account(account.id)
            paused_after_scope = store.get_strategy(strategy.id)

        self.assertEqual(changed_project.symbol, "BTC/USDT")
        self.assertIsNotNone(paused_account)
        self.assertFalse(paused_account.enabled)
        self.assertIsNotNone(paused_after_scope)
        self.assertFalse(paused_after_scope.enabled)

    def test_ready_strategy_blocks_when_credential_vault_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_USER_STRATEGY_MASTER_KEY": MASTER_KEY},
        ):
            path = Path(tmp) / "workspace.sqlite3"
            store = self._store(path)
            project = self._project(store)
            account = self._account(
                store,
                project,
                account_id="coinbase-main",
                exchange="coinbase",
                symbol="ACS/USDC",
            )
            strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                    }
                )
            )
            no_vault_store = UserWorkspaceStore(path, master_key_env=None)
            readiness = no_vault_store.strategy_readiness(strategy)

        self.assertFalse(readiness["ready"])
        self.assertTrue(
            any("credential vault is unavailable" in row for row in readiness["blockers"])
        )


if __name__ == "__main__":
    unittest.main()
