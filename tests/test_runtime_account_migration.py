from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import ExchangeConfig, load_config
from arbitrage_bot.runtime_account_migration import (
    import_runtime_accounts,
    write_runtime_credential_links,
)
from arbitrage_bot.user_workspace import UserApiConnection, UserWorkspaceStore


class RuntimeAccountMigrationTest(unittest.TestCase):
    def test_import_matches_existing_api_and_separates_multiple_exchange_accounts(self) -> None:
        base = load_config("config.acs.example.json")
        cfg = replace(
            base,
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    api_key_env="BYBIT_MAIN_KEY",
                    secret_env="BYBIT_MAIN_SECRET",
                ),
                ExchangeConfig(
                    id="bybit",
                    label="bybit-mm-2",
                    api_key_env="BYBIT_SECOND_KEY",
                    secret_env="BYBIT_SECOND_SECRET",
                ),
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                    api_key_env="COINBASE_KEY",
                    secret_env="COINBASE_SECRET",
                ),
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-swap",
                    market_type="swap",
                    api_key_env="BYBIT_MAIN_KEY",
                    secret_env="BYBIT_MAIN_SECRET",
                )
            ],
        )
        master_key = base64.urlsafe_b64encode(b"m" * 32).decode("ascii")
        environment = {
            "TEST_MASTER_KEY": master_key,
            "BYBIT_MAIN_KEY": "bybit-main-key",
            "BYBIT_MAIN_SECRET": "bybit-main-secret",
            "BYBIT_SECOND_KEY": "bybit-second-key",
            "BYBIT_SECOND_SECRET": "bybit-second-secret",
            "COINBASE_KEY": "coinbase-key",
            "COINBASE_SECRET": "coinbase-secret",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            environment,
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            existing = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "owner@example.com",
                        "label": "Bybit Main",
                        "exchange": "bybit",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={
                    "api_key": environment["BYBIT_MAIN_KEY"],
                    "secret": environment["BYBIT_MAIN_SECRET"],
                },
            )
            result = import_runtime_accounts(
                store,
                cfg,
                owner_email="owner@example.com",
                withdrawal_disabled_confirmed=True,
            )
            connections = store.list_api_connections(
                owner_email="owner@example.com",
                is_admin=False,
            )
            matched_runtime_keys = set(
                store.get_api_connection(existing.id).runtime_keys
            )

        bybit = sorted(
            (row for row in connections if row.exchange == "bybit"),
            key=lambda row: row.label,
        )
        self.assertEqual(len(connections), 3)
        self.assertEqual([row.label for row in bybit], ["Bybit 2", "Bybit Main"])
        self.assertEqual(result["runtime_links"]["bybit-spot"], existing.id)
        self.assertEqual(result["runtime_links"]["bybit-swap"], existing.id)
        self.assertEqual(
            matched_runtime_keys,
            {"bybit-spot", "bybit-swap"},
        )

    def test_duplicate_account_names_on_same_exchange_are_rejected(self) -> None:
        master_key = base64.urlsafe_b64encode(b"n" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_MASTER_KEY": master_key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            for index in range(2):
                connection = UserApiConnection.from_dict(
                    {
                        "owner_email": "owner@example.com",
                        "label": "Bybit Main",
                        "exchange": "bybit",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                )
                if index == 0:
                    store.upsert_api_connection(
                        connection,
                        credentials={"api_key": "key-1", "secret": "secret-1"},
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "distinct name"):
                        store.upsert_api_connection(
                            connection,
                            credentials={"api_key": "key-2", "secret": "secret-2"},
                        )

    def test_missing_legacy_env_links_the_only_encrypted_exchange_account(self) -> None:
        base = load_config("config.acs.example.json")
        cfg = replace(
            base,
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    api_key_env="MISSING_BYBIT_KEY",
                    secret_env="MISSING_BYBIT_SECRET",
                )
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-swap",
                    market_type="swap",
                    api_key_env="MISSING_BYBIT_KEY",
                    secret_env="MISSING_BYBIT_SECRET",
                )
            ],
        )
        master_key = base64.urlsafe_b64encode(b"u" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_MASTER_KEY": master_key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            existing = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "owner@example.com",
                        "label": "Bybit Main",
                        "exchange": "bybit",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={"api_key": "encrypted-key", "secret": "encrypted-secret"},
            )
            result = import_runtime_accounts(
                store,
                cfg,
                owner_email="owner@example.com",
                withdrawal_disabled_confirmed=True,
            )
            saved = store.get_api_connection(existing.id)

        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["runtime_links"]["bybit-spot"], existing.id)
        self.assertEqual(result["runtime_links"]["bybit-swap"], existing.id)
        self.assertTrue(result["imported"][0]["linked_without_legacy_env"])
        self.assertEqual(set(saved.runtime_keys), {"bybit-spot", "bybit-swap"})

    def test_missing_legacy_env_does_not_guess_between_multiple_accounts(self) -> None:
        base = load_config("config.acs.example.json")
        cfg = replace(
            base,
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    api_key_env="MISSING_BYBIT_KEY",
                    secret_env="MISSING_BYBIT_SECRET",
                )
            ],
            derivative_exchanges=[],
        )
        master_key = base64.urlsafe_b64encode(b"v" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_MASTER_KEY": master_key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            for label in ("Bybit Main", "Bybit 2"):
                store.upsert_api_connection(
                    UserApiConnection.from_dict(
                        {
                            "owner_email": "owner@example.com",
                            "label": label,
                            "exchange": "bybit",
                            "withdrawal_disabled_confirmed": True,
                            "trade_permission_confirmed": True,
                        }
                    ),
                    credentials={"api_key": label, "secret": f"{label}-secret"},
                )
            result = import_runtime_accounts(
                store,
                cfg,
                owner_email="owner@example.com",
                withdrawal_disabled_confirmed=True,
            )

        self.assertEqual(result["runtime_links"], {})
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("not unique", result["skipped"][0]["reason"])

    def test_config_links_preserve_runtime_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "spot_exchanges": [
                            {"id": "coinbase", "label": "coinbase-spot"}
                        ],
                        "derivative_exchanges": [],
                    }
                ),
                encoding="utf-8",
            )
            changed = write_runtime_credential_links(
                path,
                runtime_links={"coinbase-spot": "connection-coinbase"},
                owner_email="owner@example.com",
                credential_store_path="data/user_workspace.sqlite3",
                credential_master_key_env="MASTER_KEY",
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(changed, 1)
        row = saved["spot_exchanges"][0]
        self.assertEqual(row["label"], "coinbase-spot")
        self.assertEqual(row["credential_connection_id"], "connection-coinbase")
