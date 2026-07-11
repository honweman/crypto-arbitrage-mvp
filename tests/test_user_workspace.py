from __future__ import annotations

import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.user_workspace import (
    CONNECTION_MAX_AGE_SECONDS,
    CredentialCipher,
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)
from arbitrage_bot.user_strategies import UserStrategy


MASTER_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii").rstrip("=")


class UserWorkspaceStoreTest(unittest.TestCase):
    def test_project_readiness_guides_each_onboarding_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_WORKSPACE_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "name": "ACS Trading",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            project_row = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )["projects"][0]
            self.assertEqual(
                project_row["readiness"]["next_action"]["code"],
                "add_exchange_account",
            )

            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "label": "Coinbase Main",
                        "exchange": "coinbase",
                        "symbol": project.symbol,
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )
            account_row = payload["accounts"][0]
            self.assertEqual(
                account_row["readiness"]["next_action"]["code"],
                "test_connection",
            )
            self.assertEqual(account_row["readiness"]["completed_steps"], 5)

            account = store.update_account_connection(account.id, status="healthy")
            payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )
            account_row = payload["accounts"][0]
            self.assertEqual(
                account_row["readiness"]["next_action"]["code"],
                "enable_account",
            )
            self.assertGreater(
                account_row["readiness"]["connection_remaining_seconds"],
                86_000,
            )

            account = store.upsert_account(
                UserExchangeAccount.from_dict({**account.to_dict(), "enabled": True})
            )
            payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )
            self.assertTrue(payload["accounts"][0]["readiness"]["ready"])
            self.assertEqual(
                payload["projects"][0]["readiness"]["next_action"]["code"],
                "create_strategy",
            )

            strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "name": "ACS Coinbase MM",
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                    }
                )
            )
            payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )
            self.assertTrue(payload["strategies"][0]["readiness"]["ready"])
            self.assertEqual(
                payload["projects"][0]["readiness"]["next_action"]["code"],
                "enable_strategy",
            )

            store.upsert_strategy(
                UserStrategy.from_dict({**strategy.to_dict(), "enabled": True})
            )
            original_connect = store._connect
            with patch.object(store, "_connect", side_effect=original_connect) as connect:
                payload = store.public_payload(
                    owner_email=project.owner_email,
                    is_admin=False,
                )
            self.assertLessEqual(connect.call_count, 4)

        self.assertTrue(payload["projects"][0]["readiness"]["ready"])
        self.assertEqual(payload["summary"]["ready_project_count"], 1)
        self.assertEqual(payload["summary"]["ready_account_count"], 1)
        self.assertEqual(payload["summary"]["setup_progress_pct"], 100.0)

    def test_account_boolean_fields_are_strict(self) -> None:
        base = {
            "owner_email": "trader@example.com",
            "project_id": "project-acs",
            "exchange": "coinbase",
        }
        with self.assertRaisesRegex(ValueError, "account enabled must be true or false"):
            UserExchangeAccount.from_dict({**base, "enabled": "false"})
        with self.assertRaisesRegex(
            ValueError,
            "withdrawal-disabled confirmation must be true or false",
        ):
            UserExchangeAccount.from_dict(
                {**base, "withdrawal_disabled_confirmed": "true"}
            )

    def test_encrypts_credentials_and_public_payload_never_returns_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
        ):
            path = Path(tmp) / "workspace.sqlite3"
            store = UserWorkspaceStore(
                path,
                master_key_env="TEST_WORKSPACE_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "name": "ACS Trading",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "project_id": project.id,
                        "label": "Coinbase Main",
                        "exchange": "coinbase",
                        "market_type": "spot",
                        "enabled": True,
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={
                    "api_key": "organizations/test/apiKeys/key-id",
                    "secret": "private-key-secret-value",
                },
            )
            payload = store.public_payload(
                owner_email="trader@example.com",
                is_admin=False,
            )
            decrypted = store.decrypt_credentials(
                account_id=account.id,
                owner_email="trader@example.com",
            )
            database_bytes = path.read_bytes()

        self.assertTrue(payload["accounts"][0]["credentials"]["configured"])
        self.assertEqual(
            payload["accounts"][0]["credentials"]["fields"],
            ["api_key", "secret"],
        )
        self.assertNotIn("api_key", payload["accounts"][0])
        self.assertNotIn("secret", payload["accounts"][0])
        self.assertEqual(decrypted["secret"], "private-key-secret-value")
        self.assertNotIn(b"private-key-secret-value", database_bytes)
        self.assertNotIn(b"organizations/test/apiKeys/key-id", database_bytes)

    def test_account_credentials_require_master_key_and_no_withdrawal_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="MISSING_WORKSPACE_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                    }
                )
            )
            account = UserExchangeAccount.from_dict(
                {
                    "owner_email": "trader@example.com",
                    "project_id": project.id,
                    "exchange": "coinbase",
                }
            )
            with self.assertRaisesRegex(ValueError, "withdrawal permission"):
                store.upsert_account(
                    account,
                    credentials={"api_key": "key", "secret": "secret"},
                )
            confirmed = UserExchangeAccount.from_dict(
                {
                    **account.to_dict(),
                    "withdrawal_disabled_confirmed": True,
                }
            )
            with self.assertRaisesRegex(RuntimeError, "encryption is not configured"):
                store.upsert_account(
                    confirmed,
                    credentials={"api_key": "key", "secret": "secret"},
                )

    def test_projects_and_accounts_are_filtered_by_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_WORKSPACE_MASTER_KEY",
            )
            first = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "first@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                    }
                )
            )
            store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "second@example.com",
                        "asset": "BTC",
                        "quote_currency": "USDT",
                    }
                )
            )
            store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": "first@example.com",
                        "project_id": first.id,
                        "exchange": "bybit",
                    }
                )
            )
            first_payload = store.public_payload(
                owner_email="first@example.com",
                is_admin=False,
            )
            admin_payload = store.public_payload(
                owner_email="admin@example.com",
                is_admin=True,
            )

        self.assertEqual(len(first_payload["projects"]), 1)
        self.assertEqual(len(first_payload["accounts"]), 1)
        self.assertEqual(len(admin_payload["projects"]), 2)

    def test_project_cannot_be_deleted_before_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                    }
                )
            )
            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "project_id": project.id,
                        "exchange": "upbit",
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "accounts first"):
                store.delete_project(project.id)
            store.delete_account(account.id)
            store.delete_project(project.id)

            self.assertIsNone(store.get_project(project.id))
            self.assertIsNone(store.get_account(account.id))

    def test_disabling_project_disables_child_accounts_without_deleting_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_WORKSPACE_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "project_id": project.id,
                        "exchange": "coinbase",
                        "enabled": True,
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )

            store.set_project_status(project.id, "disabled")
            disabled = store.get_account(account.id)
            credentials = store.decrypt_credentials(
                account_id=account.id,
                owner_email=account.owner_email,
            )

        self.assertIsNotNone(disabled)
        self.assertFalse(disabled.enabled)
        self.assertEqual(credentials, {"api_key": "key", "secret": "secret"})

    def test_connection_error_disables_account_and_keeps_credentials_encrypted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_WORKSPACE_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "exchange": "upbit",
                        "api_variant": "indonesia",
                        "symbol": "ACS/USDT",
                        "enabled": True,
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )

            failed = store.update_account_connection(
                account.id,
                status="error",
                error="authentication failed",
            )
            payload = store.public_payload(
                owner_email=account.owner_email,
                is_admin=False,
            )

        self.assertFalse(failed.enabled)
        self.assertEqual(failed.connection_status, "error")
        self.assertIsNotNone(failed.connection_checked_at)
        self.assertEqual(failed.connection_error, "authentication failed")
        self.assertTrue(payload["accounts"][0]["credentials"]["configured"])
        self.assertFalse(payload["accounts"][0]["connection_fresh"])
        self.assertEqual(payload["accounts"][0]["api_variant"], "indonesia")
        self.assertEqual(payload["accounts"][0]["symbol"], "ACS/USDT")

    def test_legacy_accounts_gain_project_symbol_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            store = UserWorkspaceStore(path, master_key_env=None)
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "exchange": "coinbase",
                        "enabled": True,
                    }
                )
            )

            restarted_store = UserWorkspaceStore(path, master_key_env=None)
            migrated = restarted_store.get_account(account.id)

        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.symbol, "ACS/USDC")
        self.assertFalse(migrated.enabled)

    def test_stale_healthy_connection_is_disabled_on_read_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
        ):
            path = Path(tmp) / "workspace.sqlite3"
            store = UserWorkspaceStore(
                path,
                master_key_env="TEST_WORKSPACE_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            account = store.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "owner_email": project.owner_email,
                        "project_id": project.id,
                        "exchange": "coinbase",
                        "symbol": project.symbol,
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            account = store.update_account_connection(account.id, status="healthy")
            account = store.upsert_account(
                UserExchangeAccount.from_dict({**account.to_dict(), "enabled": True})
            )

            future = time.time() + CONNECTION_MAX_AGE_SECONDS + 1
            with patch("arbitrage_bot.user_workspace._now", return_value=future):
                expired = store.get_account(account.id)
                restarted_store = UserWorkspaceStore(
                    path,
                    master_key_env="TEST_WORKSPACE_MASTER_KEY",
                )
                persisted = restarted_store.get_account(account.id)

        self.assertIsNotNone(expired)
        self.assertEqual(expired.connection_status, "healthy")
        self.assertFalse(expired.enabled)
        self.assertFalse(persisted.enabled)

    def test_cipher_rejects_invalid_key_length(self) -> None:
        short_key = base64.urlsafe_b64encode(b"short").decode("ascii")
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            CredentialCipher(short_key)


if __name__ == "__main__":
    unittest.main()
