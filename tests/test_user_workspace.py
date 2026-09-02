from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct

from arbitrage_bot.user_workspace import (
    CONNECTION_MAX_AGE_SECONDS,
    CredentialCipher,
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
    UserRiskProfile,
    UserWorkspaceStore,
    api_connection_egress_blockers,
    validate_exchange_credentials,
)
from arbitrage_bot.user_strategies import UserStrategy


MASTER_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii").rstrip("=")


class UserWorkspaceStoreTest(unittest.TestCase):
    def test_account_egress_is_encrypted_verified_and_unique_per_exchange(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_MASTER_KEY": MASTER_KEY},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            first = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "label": "Gate Main",
                        "exchange": "gateio",
                        "egress_mode": "proxy",
                        "egress_expected_ip": "203.0.113.10",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={
                    "api_key": "key-1",
                    "secret": "secret-1",
                    "proxy_url": "http://user:pass@10.0.0.10:8080",
                },
            )
            verified = store.update_api_connection_check(
                first.id,
                status="healthy",
                check={
                    "balances": [],
                    "open_order_count": 0,
                    "egress_observed_ip": "203.0.113.10",
                    "egress_checked_at": time.time(),
                    "egress_error": "",
                },
            )
            payload = store.public_payload(
                owner_email="trader@example.com",
                is_admin=False,
            )
            decrypted = store.decrypt_credentials(
                account_id=first.id,
                owner_email=first.owner_email,
            )

            self.assertEqual(verified.connection_status, "healthy")
            self.assertEqual(decrypted["proxy_url"], "http://user:pass@10.0.0.10:8080")
            self.assertNotIn(
                "http://user:pass@10.0.0.10:8080",
                str(payload["connections"][0]),
            )
            self.assertTrue(payload["connections"][0]["proxy_configured"])
            self.assertTrue(payload["connections"][0]["egress_ready"])
            with self.assertRaisesRegex(ValueError, "already assigned"):
                store.upsert_api_connection(
                    UserApiConnection.from_dict(
                        {
                            "owner_email": "trader@example.com",
                            "label": "Gate Second",
                            "exchange": "gateio",
                            "egress_mode": "source_ip",
                            "egress_source_ip": "10.0.0.11",
                            "egress_expected_ip": "203.0.113.10",
                        }
                    ),
                    credentials={"api_key": "key-2", "secret": "secret-2"},
                )
            other_owner = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "other@example.com",
                        "label": "Gate Main",
                        "exchange": "gateio",
                        "egress_mode": "source_ip",
                        "egress_source_ip": "10.0.0.11",
                        "egress_expected_ip": "203.0.113.10",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={"api_key": "key-3", "secret": "secret-3"},
            )
            self.assertEqual(other_owner.owner_email, "other@example.com")

    def test_multiple_same_exchange_accounts_require_verified_unique_egress(
        self,
    ) -> None:
        first = UserApiConnection.from_dict(
            {
                "owner_email": "one@example.com",
                "label": "Bybit One",
                "exchange": "bybit",
            }
        )
        second = UserApiConnection.from_dict(
            {
                "owner_email": "one@example.com",
                "label": "Bybit Two",
                "exchange": "bybit",
                "connection_status": "healthy",
                "connection_checked_at": time.time(),
            }
        )

        blockers = api_connection_egress_blockers(first, [first, second])

        self.assertIn("expected public IP is required", blockers)
        self.assertIn("egress public IP has not been verified", blockers)

    def test_staged_second_account_does_not_interrupt_existing_account(self) -> None:
        now = time.time()
        existing = UserApiConnection.from_dict(
            {
                "owner_email": "one@example.com",
                "label": "Bybit Main",
                "exchange": "bybit",
                "connection_status": "healthy",
                "connection_checked_at": now,
            }
        )
        staged = UserApiConnection.from_dict(
            {
                "owner_email": "one@example.com",
                "label": "Bybit Second",
                "exchange": "bybit",
                "egress_mode": "source_ip",
                "egress_source_ip": "172.19.60.74",
                "egress_expected_ip": "8.222.193.180",
            }
        )

        existing_blockers = api_connection_egress_blockers(
            existing,
            [existing, staged],
            now=now,
        )
        staged_blockers = api_connection_egress_blockers(
            staged,
            [existing, staged],
            now=now,
        )

        self.assertEqual(existing_blockers, [])
        self.assertIn("egress public IP has not been verified", staged_blockers)

    def test_second_account_activation_waits_for_existing_route_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_MASTER_KEY": MASTER_KEY},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            first = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "one@example.com",
                        "label": "Bybit Main",
                        "exchange": "bybit",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={"api_key": "key-1", "secret": "secret-1"},
            )
            first = store.update_api_connection_check(first.id, status="healthy")
            second = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "one@example.com",
                        "label": "Bybit Second",
                        "exchange": "bybit",
                        "egress_mode": "source_ip",
                        "egress_source_ip": "172.19.60.74",
                        "egress_expected_ip": "8.222.193.180",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={"api_key": "key-2", "secret": "secret-2"},
            )

            blocked_second = store.update_api_connection_check(
                second.id,
                status="healthy",
                check={
                    "egress_observed_ip": "8.222.193.180",
                    "egress_checked_at": time.time(),
                    "egress_error": "",
                },
            )

            self.assertEqual(blocked_second.connection_status, "error")
            self.assertIn(
                "account Bybit Main",
                blocked_second.connection_error,
            )
            self.assertEqual(
                store.get_api_connection(first.id).connection_status,
                "healthy",
            )

    def test_different_owners_do_not_share_exchange_egress_checks(self) -> None:
        now = time.time()
        first = UserApiConnection.from_dict(
            {
                "owner_email": "one@example.com",
                "label": "Bybit One",
                "exchange": "bybit",
                "connection_status": "healthy",
                "connection_checked_at": now,
                "egress_mode": "source_ip",
                "egress_source_ip": "172.19.60.74",
                "egress_expected_ip": "8.222.193.180",
                "egress_observed_ip": "8.222.193.180",
                "egress_checked_at": now,
            }
        )
        second = UserApiConnection.from_dict(
            {
                "owner_email": "two@example.com",
                "label": "Bybit Two",
                "exchange": "bybit",
                "connection_status": "healthy",
                "connection_checked_at": now,
                "egress_mode": "source_ip",
                "egress_source_ip": "172.19.60.74",
                "egress_expected_ip": "8.222.193.180",
                "egress_observed_ip": "8.222.193.180",
                "egress_checked_at": now,
            }
        )

        self.assertEqual(
            api_connection_egress_blockers(first, [first, second], now=now),
            [],
        )
        self.assertEqual(
            api_connection_egress_blockers(second, [first, second], now=now),
            [],
        )

    def test_legacy_api_connection_is_normalized_and_keeps_large_balance_snapshot(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_MASTER_KEY": MASTER_KEY},
                clear=False,
            ),
        ):
            path = Path(tmp) / "workspace.sqlite3"
            store = UserWorkspaceStore(path, master_key_env="TEST_MASTER_KEY")
            connection = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "label": "Binance Main",
                        "exchange": "binance",
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                        "balance_snapshot": [
                            {
                                "currency": f"ASSET{index}",
                                "total": index + 1,
                                **(
                                    {
                                        "valuation_price": 600.0,
                                        "valuation_quote": "USDT",
                                        "valuation_symbol": "ASSET0/USDT",
                                        "valuation_at": 123.0,
                                    }
                                    if index == 0
                                    else {}
                                ),
                            }
                            for index in range(150)
                        ],
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            with sqlite3.connect(path) as database:
                raw = json.loads(
                    database.execute(
                        "SELECT payload FROM user_api_connections WHERE id = ?",
                        (connection.id,),
                    ).fetchone()[0]
                )
                raw.pop("market_types")
                raw.pop("market_scope")
                database.execute(
                    "UPDATE user_api_connections SET payload = ? WHERE id = ?",
                    (json.dumps(raw), connection.id),
                )
                database.commit()

            restarted = UserWorkspaceStore(path, master_key_env="TEST_MASTER_KEY")
            normalized = restarted.get_api_connection(connection.id)
            with sqlite3.connect(path) as database:
                stored = json.loads(
                    database.execute(
                        "SELECT payload FROM user_api_connections WHERE id = ?",
                        (connection.id,),
                    ).fetchone()[0]
                )

        self.assertEqual(normalized.market_types, ("spot", "swap"))
        self.assertEqual(len(normalized.balance_snapshot), 150)
        self.assertEqual(normalized.balance_snapshot[0]["valuation_price"], 600.0)
        self.assertEqual(normalized.balance_snapshot[0]["valuation_quote"], "USDT")
        self.assertEqual(stored["market_types"], ["spot", "swap"])
        self.assertEqual(stored["market_scope"], "unified")

    def test_global_api_connection_is_independent_of_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"TEST_MASTER_KEY": MASTER_KEY},
                clear=False,
            ):
                store = UserWorkspaceStore(
                    Path(tmp) / "workspace.sqlite3",
                    master_key_env="TEST_MASTER_KEY",
                )
                api_connection = store.upsert_api_connection(
                    UserApiConnection.from_dict(
                        {
                            "owner_email": "trader@example.com",
                            "label": "Coinbase Global",
                            "exchange": "coinbase",
                            "market_type": "spot",
                            "allow_all_markets": True,
                            "withdrawal_disabled_confirmed": True,
                            "trade_permission_confirmed": True,
                        }
                    ),
                    credentials={
                        "api_key": "test-api-key",
                        "secret": "test-api-secret",
                    },
                )
                payload = store.public_payload(
                    owner_email="trader@example.com",
                    is_admin=False,
                )

                self.assertEqual(payload["projects"], [])
                self.assertEqual(payload["accounts"], [])
                self.assertEqual(payload["connections"][0]["id"], api_connection.id)
                self.assertTrue(api_connection.allow_all_markets)
                self.assertEqual(
                    payload["connections"][0]["market_scope"],
                    "all_supported_markets",
                )
                self.assertTrue(payload["connections"][0]["credentials_configured"])
                self.assertEqual(
                    store.delete_connection(
                        api_connection.id,
                        owner_email="trader@example.com",
                    ),
                    0,
                )
                self.assertEqual(
                    store.list_api_connections(
                        owner_email="trader@example.com",
                        is_admin=False,
                    ),
                    [],
                )

    def test_wallet_challenge_verifies_once_and_persists_read_only_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            signer = Account.create()
            challenge = store.create_wallet_challenge(
                owner_email="trader@example.com",
                address=signer.address,
                chain_id=137,
                wallet_type="metamask",
                domain="daydayuptrade.com",
            )
            signature = Account.sign_message(
                encode_defunct(text=challenge["message"]),
                signer.key,
            ).signature.hex()

            wallet = store.verify_wallet_challenge(
                owner_email="trader@example.com",
                challenge_id=challenge["challenge_id"],
                signature=signature,
                label="Trading Wallet",
            )
            payload = store.public_payload(
                owner_email="trader@example.com",
                is_admin=False,
            )

            self.assertEqual(wallet.address, signer.address)
            self.assertEqual(wallet.chain_id, 137)
            self.assertFalse(wallet.to_dict()["trading_authorized"])
            self.assertEqual(payload["summary"]["wallet_count"], 1)
            self.assertEqual(payload["wallets"][0]["label"], "Trading Wallet")
            self.assertEqual(
                {row["id"] for row in payload["dex_venue_catalog"]},
                {"hyperliquid", "polymarket", "dydx", "aster"},
            )
            exchange_catalog = {row["id"]: row for row in payload["exchange_catalog"]}
            self.assertEqual(
                exchange_catalog["hyperliquid"]["market_types"],
                ["spot", "swap"],
            )
            self.assertEqual(
                exchange_catalog["dydx"]["required_credentials"],
                ["api_key", "secret"],
            )
            self.assertEqual(
                exchange_catalog["aster"]["default_variant"],
                "v3",
            )
            for exchange_id in ("gateio", "htx", "mexc"):
                self.assertEqual(
                    exchange_catalog[exchange_id]["market_types"],
                    ["spot", "swap"],
                )
                self.assertEqual(
                    exchange_catalog[exchange_id]["required_credentials"],
                    ["api_key", "secret"],
                )
            with self.assertRaisesRegex(ValueError, "already been used"):
                store.verify_wallet_challenge(
                    owner_email="trader@example.com",
                    challenge_id=challenge["challenge_id"],
                    signature=signature,
                )

            store.delete_wallet(wallet.id, owner_email="trader@example.com")
            self.assertEqual(
                store.list_wallets(
                    owner_email="trader@example.com",
                    is_admin=False,
                ),
                [],
            )

    def test_venue_connection_persists_and_is_removed_with_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            store = UserWorkspaceStore(path, master_key_env=None)
            signer = Account.create()
            challenge = store.create_wallet_challenge(
                owner_email="trader@example.com",
                address=signer.address,
                chain_id=137,
                wallet_type="metamask",
                domain="daydayuptrade.com",
            )
            signature = Account.sign_message(
                encode_defunct(text=challenge["message"]),
                signer.key,
            ).signature.hex()
            wallet = store.verify_wallet_challenge(
                owner_email="trader@example.com",
                challenge_id=challenge["challenge_id"],
                signature=signature,
            )
            link = store.upsert_venue_connection(
                owner_email="trader@example.com",
                venue="polymarket",
                wallet=wallet,
                check={
                    "status": "healthy",
                    "checked_at": time.time(),
                    "latency_ms": 12.5,
                    "detail": {"position_count": 3, "ignored": ["secret"]},
                },
            )

            restarted = UserWorkspaceStore(path, master_key_env=None)
            payload = restarted.public_payload(
                owner_email="trader@example.com",
                is_admin=False,
            )

            self.assertTrue(link.to_dict()["read_only_verified"])
            self.assertFalse(link.to_dict()["trading_authorized"])
            self.assertEqual(payload["summary"]["venue_connection_count"], 1)
            self.assertEqual(payload["summary"]["healthy_venue_connection_count"], 1)
            self.assertEqual(
                payload["venue_connections"][0]["detail"], {"position_count": 3}
            )
            restarted.delete_wallet(wallet.id, owner_email="trader@example.com")
            self.assertEqual(
                restarted.list_venue_connections(
                    owner_email="trader@example.com",
                    is_admin=False,
                ),
                [],
            )
            self.assertIsNone(
                restarted.record_venue_connection_check(
                    link.id,
                    {"status": "healthy", "checked_at": time.time()},
                )
            )

    def test_venue_connection_staleness_is_computed_when_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            store.upsert_venue_connection(
                owner_email="trader@example.com",
                venue="dydx",
                wallet=None,
                check={
                    "status": "healthy",
                    "checked_at": time.time() - 601.0,
                    "latency_ms": 8.0,
                    "detail": {"market_count": 200},
                },
            )

            payload = store.public_payload(
                owner_email="trader@example.com",
                is_admin=False,
            )
            link = payload["venue_connections"][0]

            self.assertTrue(link["stale"])
            self.assertFalse(link["read_only_verified"])
            self.assertEqual(payload["summary"]["healthy_venue_connection_count"], 0)
            self.assertEqual(payload["summary"]["stale_venue_connection_count"], 1)
            self.assertEqual(payload["summary"]["error_venue_connection_count"], 0)

    def test_decentralized_exchange_credentials_are_validated(self) -> None:
        signer = Account.create()
        owner = Account.create()
        validate_exchange_credentials(
            "hyperliquid",
            {"api_key": owner.address, "secret": signer.key.hex()},
        )
        validate_exchange_credentials(
            "dydx",
            {
                "api_key": "dydx1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqe36p3",
                "secret": "one two three four five six seven eight nine ten eleven twelve",
            },
        )
        with self.assertRaisesRegex(ValueError, "valid EVM address"):
            validate_exchange_credentials(
                "aster",
                {"api_key": "not-an-address", "secret": signer.key.hex()},
            )
        with self.assertRaisesRegex(ValueError, "32-byte EVM private key"):
            validate_exchange_credentials(
                "hyperliquid",
                {"api_key": signer.address, "secret": "not-a-key"},
            )
        with self.assertRaisesRegex(ValueError, "dedicated agent/signer key"):
            validate_exchange_credentials(
                "hyperliquid",
                {"api_key": signer.address, "secret": signer.key.hex()},
            )
        with self.assertRaisesRegex(ValueError, "dYdX Chain address"):
            validate_exchange_credentials(
                "dydx",
                {
                    "api_key": signer.address,
                    "secret": "one two three four five six seven eight nine ten eleven twelve",
                },
            )

    def test_wallet_challenge_rejects_another_signer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            expected = Account.create()
            attacker = Account.create()
            challenge = store.create_wallet_challenge(
                owner_email="trader@example.com",
                address=expected.address,
                chain_id=1,
                wallet_type="injected",
                domain="daydayuptrade.com",
            )
            signature = Account.sign_message(
                encode_defunct(text=challenge["message"]),
                attacker.key,
            ).signature.hex()

            with self.assertRaisesRegex(ValueError, "does not match"):
                store.verify_wallet_challenge(
                    owner_email="trader@example.com",
                    challenge_id=challenge["challenge_id"],
                    signature=signature,
                )

    def test_user_risk_profile_is_owner_scoped_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            saved = store.upsert_risk_profile(
                UserRiskProfile.from_dict(
                    {
                        "owner_email": "trader@example.com",
                        "trading_enabled": False,
                        "max_order_quote": 50.0,
                        "max_cycle_quote": 75.0,
                        "max_total_exposure_quote": 250.0,
                        "max_daily_loss_quote": 20.0,
                        "max_open_orders": 12,
                        "max_active_strategies": 3,
                    }
                )
            )
            other = store.risk_profile("other@example.com")
            payload = store.public_payload(
                owner_email="trader@example.com",
                is_admin=False,
            )

        self.assertFalse(saved.trading_enabled)
        self.assertFalse(saved.to_dict()["live_submit_allowed"])
        self.assertEqual(saved.max_order_quote, 50.0)
        self.assertEqual(saved.max_cycle_quote, 75.0)
        self.assertEqual(payload["risk_profile"]["max_open_orders"], 12)
        self.assertTrue(other.trading_enabled)
        self.assertTrue(other.to_dict()["live_submit_allowed"])
        self.assertEqual(other.max_total_exposure_quote, 0.0)
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            UserRiskProfile.from_dict(
                {
                    "owner_email": "trader@example.com",
                    "max_daily_loss_quote": -1,
                }
            )

    def test_user_risk_cycle_limit_cannot_be_lower_than_order_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_cycle_quote"):
            UserRiskProfile.from_dict(
                {
                    "owner_email": "trader@example.com",
                    "max_order_quote": 50.0,
                    "max_cycle_quote": 25.0,
                }
            )

    def test_strategy_defaults_are_owner_scoped_reassigned_and_purged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            saved = store.upsert_strategy_default(
                "trader@example.com",
                "auto_buy_sell",
                {
                    "exchange": "workspace:coinbase-main:spot",
                    "symbol": "ACS/USDC",
                    "side": "buy",
                    "total_quote": 25.0,
                },
            )

            self.assertEqual(saved["total_quote"], 25.0)
            self.assertEqual(
                store.strategy_default(
                    "trader@example.com",
                    "auto_buy_sell",
                )["symbol"],
                "ACS/USDC",
            )
            self.assertEqual(
                store.strategy_default("other@example.com", "auto_buy_sell"),
                {},
            )

            moved = store.reassign_owner(
                "trader@example.com",
                "renamed@example.com",
            )
            self.assertEqual(moved["user_strategy_defaults"], 1)
            self.assertEqual(
                store.strategy_default("trader@example.com", "auto_buy_sell"),
                {},
            )
            self.assertEqual(
                store.strategy_default("renamed@example.com", "auto_buy_sell")[
                    "exchange"
                ],
                "workspace:coinbase-main:spot",
            )

            removed = store.purge_owner("renamed@example.com")
            self.assertEqual(removed["user_strategy_defaults"], 1)
            self.assertEqual(
                store.strategy_default("renamed@example.com", "auto_buy_sell"),
                {},
            )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                store.upsert_strategy_default(
                    "renamed@example.com",
                    "platform_risk",
                    {},
                )

    def test_project_readiness_guides_each_onboarding_step(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
            ),
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
            self.assertEqual(account_row["readiness"]["completed_steps"], 6)

            account = store.update_account_connection(
                account.id,
                status="healthy",
                check={
                    "balances": [
                        {
                            "currency": "ACS",
                            "free": 110_000_000,
                            "used": 0,
                            "total": 110_000_000,
                            "wallet": "funding",
                            "tradable": False,
                        }
                    ]
                },
            )
            payload = store.public_payload(
                owner_email=project.owner_email,
                is_admin=False,
            )
            account_row = payload["accounts"][0]
            self.assertEqual(
                account_row["readiness"]["next_action"]["code"],
                "complete",
            )
            self.assertTrue(account.enabled)
            self.assertEqual(account.balance_snapshot[0]["wallet"], "funding")
            self.assertFalse(account.balance_snapshot[0]["tradable"])
            self.assertTrue(account_row["enabled"])
            self.assertEqual(account_row["readiness"]["completed_steps"], 8)
            self.assertGreater(
                account_row["readiness"]["connection_remaining_seconds"],
                86_000,
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
                        "live_enabled": True,
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
            with patch.object(
                store, "_connect", side_effect=original_connect
            ) as connect:
                payload = store.public_payload(
                    owner_email=project.owner_email,
                    is_admin=False,
                )
            self.assertLessEqual(connect.call_count, 6)

        self.assertTrue(payload["projects"][0]["readiness"]["ready"])
        self.assertEqual(payload["summary"]["ready_project_count"], 1)
        self.assertEqual(payload["summary"]["ready_account_count"], 1)
        self.assertEqual(payload["summary"]["setup_progress_pct"], 100.0)
        self.assertEqual(payload["risk_capacity"]["active_strategies"], 1)
        self.assertEqual(payload["risk_capacity"]["reserved_exposure_quote"], 50.0)
        self.assertEqual(payload["risk_capacity"]["reserved_open_orders"], 4)

    def test_account_boolean_fields_are_strict(self) -> None:
        base = {
            "owner_email": "trader@example.com",
            "project_id": "project-acs",
            "exchange": "coinbase",
        }
        with self.assertRaisesRegex(
            ValueError, "account enabled must be true or false"
        ):
            UserExchangeAccount.from_dict({**base, "enabled": "false"})
        with self.assertRaisesRegex(
            ValueError,
            "withdrawal-disabled confirmation must be true or false",
        ):
            UserExchangeAccount.from_dict(
                {**base, "withdrawal_disabled_confirmed": "true"}
            )

    def test_encrypts_credentials_and_public_payload_never_returns_values(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
            ),
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

    def test_account_credentials_require_master_key_and_no_withdrawal_permission(
        self,
    ) -> None:
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
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
            ),
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

    def test_upbit_korea_is_default_and_legacy_global_is_normalized(self) -> None:
        current = UserExchangeAccount.from_dict(
            {
                "owner_email": "trader@example.com",
                "project_id": "project-upbit",
                "exchange": "upbit",
            }
        )
        legacy = UserExchangeAccount.from_dict(
            {
                "owner_email": "trader@example.com",
                "project_id": "project-upbit",
                "exchange": "upbit",
                "api_variant": "global",
            }
        )

        self.assertEqual(current.api_variant, "korea")
        self.assertEqual(legacy.api_variant, "korea")

    def test_disabling_project_disables_child_accounts_without_deleting_credentials(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
            ),
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

    def test_connection_error_disables_account_and_keeps_credentials_encrypted(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
            ),
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
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_WORKSPACE_MASTER_KEY": MASTER_KEY},
            ),
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
