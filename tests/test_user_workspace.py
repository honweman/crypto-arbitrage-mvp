from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.user_workspace import (
    CredentialCipher,
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)


MASTER_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii").rstrip("=")


class UserWorkspaceStoreTest(unittest.TestCase):
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

    def test_cipher_rejects_invalid_key_length(self) -> None:
        short_key = base64.urlsafe_b64encode(b"short").decode("ascii")
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            CredentialCipher(short_key)


if __name__ == "__main__":
    unittest.main()
