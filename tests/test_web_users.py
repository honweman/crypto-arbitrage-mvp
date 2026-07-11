from __future__ import annotations

import base64
import os
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.web.users import (
    WebUserStore,
    normalize_assets,
    normalize_username,
    totp_code,
    validate_password,
    verify_password,
    verify_totp,
)


class WebUserStoreTest(unittest.TestCase):
    def test_normalizes_assets(self) -> None:
        self.assertEqual(normalize_assets("acs, BTC acs"), ["ACS", "BTC"])

    def test_registers_user_and_authenticates_with_username_and_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            user = store.create_user(
                email="Trader@Example.com",
                username="Trader.One",
                password="Strong-pass-1!",
                allowed_assets="acs,btc",
            )

            self.assertEqual(user.email, "trader@example.com")
            self.assertEqual(user.username, "trader.one")
            self.assertEqual(user.allowed_assets, ["ACS", "BTC"])
            self.assertTrue(verify_password("Strong-pass-1!", user.password_hash))
            self.assertTrue(verify_totp(user.totp_secret, totp_code(user.totp_secret)))
            self.assertIsNotNone(
                store.authenticate(
                    username="trader.one",
                    password="Strong-pass-1!",
                )
            )
            self.assertIsNone(
                store.authenticate(
                    username="trader.one",
                    password="wrong-password",
                )
            )

    def test_password_policy_requires_letter_number_and_special_character(self) -> None:
        self.assertEqual(validate_password("Strong-pass-1!"), "Strong-pass-1!")
        for password in ("short1!", "12345678!", "Password!", "Password1"):
            with self.subTest(password=password):
                with self.assertRaises(ValueError):
                    validate_password(password)

    def test_totp_enable_login_enforcement_and_disable_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            user = store.create_user(
                email="secure@example.com",
                username="secure-user",
                password="Strong-pass-1!",
            )

            with self.assertRaisesRegex(ValueError, "current password"):
                store.set_totp_enabled(
                    email=user.email,
                    password="wrong-password",
                    code=totp_code(user.totp_secret),
                    enabled=True,
                )
            with self.assertRaisesRegex(ValueError, "code is invalid"):
                store.set_totp_enabled(
                    email=user.email,
                    password="Strong-pass-1!",
                    code="123",
                    enabled=True,
                )

            enabled = store.set_totp_enabled(
                email=user.email,
                password="Strong-pass-1!",
                code=totp_code(user.totp_secret),
                enabled=True,
            )
            self.assertTrue(enabled.totp_enabled)
            self.assertEqual(enabled.auth_version, user.auth_version + 1)
            self.assertIsNone(
                store.authenticate(
                    username=user.username,
                    password="Strong-pass-1!",
                )
            )
            self.assertIsNone(
                store.authenticate(
                    username=user.username,
                    password="Strong-pass-1!",
                    totp="00000000",
                )
            )
            self.assertIsNotNone(
                store.authenticate(
                    username=user.username,
                    password="Strong-pass-1!",
                    totp=totp_code(enabled.totp_secret),
                )
            )

            disabled = store.set_totp_enabled(
                email=user.email,
                password="Strong-pass-1!",
                code=totp_code(enabled.totp_secret),
                enabled=False,
            )
            password_auth = store.authenticate(
                username=user.username,
                password="Strong-pass-1!",
            )

        self.assertFalse(disabled.totp_enabled)
        self.assertNotEqual(disabled.totp_secret, enabled.totp_secret)
        self.assertEqual(disabled.auth_version, enabled.auth_version + 1)
        self.assertIsNotNone(password_auth)

    def test_totp_secret_migrates_to_encrypted_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.json"
            user = WebUserStore(path).create_user(
                email="secure@example.com",
                password="Strong-pass-1!",
            )
            self.assertIn(user.totp_secret, path.read_text(encoding="utf-8"))
            encoded_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
            with patch.dict(os.environ, {"TEST_TOTP_MASTER_KEY": encoded_key}):
                encrypted_store = WebUserStore(
                    path,
                    master_key_env="TEST_TOTP_MASTER_KEY",
                )
                migrated = encrypted_store.migrate_totp_secrets()
                stored = path.read_text(encoding="utf-8")
                loaded = encrypted_store.get_user(user.email)
                store_mode = path.stat().st_mode & 0o777

            raw_user = json.loads(stored)["users"][0]

        self.assertTrue(migrated)
        self.assertNotIn(user.totp_secret, stored)
        self.assertNotIn("totp_secret", raw_user)
        self.assertIn("totp_secret_nonce", raw_user)
        self.assertIn("totp_secret_ciphertext", raw_user)
        self.assertEqual(loaded.totp_secret, user.totp_secret)
        self.assertEqual(store_mode, 0o600)

    def test_username_is_unique_and_legacy_users_get_compatible_username(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.json"
            store = WebUserStore(path)
            user = store.create_user(
                email="first.user@example.com",
                username="first-user",
                password="Strong-pass-1!",
            )
            with self.assertRaisesRegex(ValueError, "username is already registered"):
                store.create_user(
                    email="other@example.com",
                    username="FIRST-USER",
                    password="Strong-pass-2!",
                )

            raw = json.loads(path.read_text(encoding="utf-8"))
            del raw["users"][0]["username"]
            path.write_text(json.dumps(raw), encoding="utf-8")

            migrated = store.get_user(user.email)
            self.assertEqual(migrated.username, "first.user")
            self.assertEqual(normalize_username("Trader_01"), "trader_01")

    def test_password_reset_increments_auth_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            user = store.create_user(
                email="member@example.com",
                password="Strong-pass-1!",
            )
            updated = store.reset_password(
                email=user.email,
                new_password="Strong-pass-2!",
            )
            self.assertEqual(updated.auth_version, user.auth_version + 1)
            self.assertIsNone(
                store.authenticate(username=user.username, password="Strong-pass-1!")
            )
            self.assertIsNotNone(
                store.authenticate(username=user.username, password="Strong-pass-2!")
            )

    def test_password_reset_disables_and_rotates_totp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            user = store.create_user(
                email="member@example.com",
                password="Strong-pass-1!",
            )
            enabled = store.set_totp_enabled(
                email=user.email,
                password="Strong-pass-1!",
                code=totp_code(user.totp_secret),
                enabled=True,
            )

            updated = store.reset_password(
                email=user.email,
                new_password="Strong-pass-2!",
            )
            reset_auth = store.authenticate(
                username=user.username,
                password="Strong-pass-2!",
            )

        self.assertFalse(updated.totp_enabled)
        self.assertNotEqual(updated.totp_secret, enabled.totp_secret)
        self.assertEqual(updated.auth_version, enabled.auth_version + 1)
        self.assertIsNotNone(reset_auth)

    def test_profile_preferred_asset_must_be_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(
                email="trader@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS"],
            )

            with self.assertRaisesRegex(ValueError, "not allowed"):
                store.update_profile(
                    email="trader@example.com",
                    preferred_asset="BTC",
                )

    def test_admin_create_user_defaults_to_user_role_and_validates_preferred_asset(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            admin = store.create_user(email="admin@example.com", password="Strong-pass-1!")
            member = store.admin_create_user(
                email="member@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS", "BTC"],
                preferred_asset="acs",
            )

            self.assertEqual(admin.role, "admin")
            self.assertEqual(member.role, "user")
            self.assertEqual(member.preferred_asset, "ACS")
            with self.assertRaisesRegex(ValueError, "preferred asset"):
                store.admin_create_user(
                    email="other@example.com",
                    password="Strong-pass-1!",
                    allowed_assets=["ACS"],
                    preferred_asset="BTC",
                )
            with self.assertRaisesRegex(ValueError, "role must be"):
                store.admin_create_user(
                    email="other@example.com",
                    password="Strong-pass-1!",
                    role="superuser",
                )

    def test_admin_update_user_role_protects_the_last_remaining_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            admin = store.create_user(email="admin@example.com", password="Strong-pass-1!")

            with self.assertRaisesRegex(ValueError, "last remaining admin"):
                store.admin_update_user(email=admin.email, role="user")

            second_admin = store.admin_create_user(
                email="second-admin@example.com",
                password="Strong-pass-1!",
                role="admin",
            )
            demoted = store.admin_update_user(email=second_admin.email, role="user")
            self.assertEqual(demoted.role, "user")
            # Now that only one admin remains, it is protected again.
            with self.assertRaisesRegex(ValueError, "last remaining admin"):
                store.admin_update_user(email=admin.email, role="user")

    def test_admin_update_user_validates_preferred_asset_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(email="admin@example.com", password="Strong-pass-1!")
            member = store.admin_create_user(
                email="member@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS"],
            )

            with self.assertRaisesRegex(ValueError, "preferred asset"):
                store.admin_update_user(
                    email=member.email,
                    allowed_assets=["ACS"],
                    allowed_assets_provided=True,
                    preferred_asset="BTC",
                    preferred_asset_provided=True,
                )

            updated = store.admin_update_user(
                email=member.email,
                allowed_assets=["ACS", "BTC"],
                allowed_assets_provided=True,
                preferred_asset="BTC",
                preferred_asset_provided=True,
            )
            self.assertEqual(updated.allowed_assets, ["ACS", "BTC"])
            self.assertEqual(updated.preferred_asset, "BTC")

    def test_admin_update_user_narrowing_assets_drops_stale_preferred_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(email="admin@example.com", password="Strong-pass-1!")
            member = store.admin_create_user(
                email="member@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS", "BTC"],
                preferred_asset="ACS",
            )

            # Narrowing the allowed list without touching preferred_asset must not
            # error just because the old preferred asset fell out of the new list.
            updated = store.admin_update_user(
                email=member.email,
                allowed_assets=["BTC"],
                allowed_assets_provided=True,
            )
            self.assertEqual(updated.allowed_assets, ["BTC"])
            self.assertEqual(updated.preferred_asset, "")

    def test_admin_update_user_is_atomic_on_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(email="admin@example.com", password="Strong-pass-1!")
            member = store.admin_create_user(
                email="member@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS", "BTC"],
                preferred_asset="ACS",
            )

            with self.assertRaisesRegex(ValueError, "preferred asset"):
                store.admin_update_user(
                    email=member.email,
                    role="admin",
                    allowed_assets=["BTC"],
                    allowed_assets_provided=True,
                    preferred_asset="ACS",
                    preferred_asset_provided=True,
                )

            persisted = store.get_user(member.email)
            self.assertEqual(persisted.role, "user")
            self.assertEqual(persisted.allowed_assets, ["ACS", "BTC"])
            self.assertEqual(persisted.preferred_asset, "ACS")

    def test_admin_update_user_rejects_no_op_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            member = store.create_user(email="member@example.com", password="Strong-pass-1!")

            with self.assertRaisesRegex(ValueError, "no changes"):
                store.admin_update_user(email=member.email)

    def test_admin_update_user_replaces_password_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            member = store.create_user(email="member@example.com", password="Strong-pass-1!")

            enabled = store.set_totp_enabled(
                email=member.email,
                password="Strong-pass-1!",
                code=totp_code(member.totp_secret),
                enabled=True,
            )
            updated = store.admin_update_user(
                email=member.email,
                new_password="Strong-pass-2!",
            )

            self.assertIsNone(
                store.authenticate(
                    email=member.email,
                    password="Strong-pass-1!",
                    totp=totp_code(member.totp_secret),
                )
            )
            self.assertIsNotNone(
                store.authenticate(
                    email=member.email,
                    password="Strong-pass-2!",
                )
            )
            self.assertFalse(updated.totp_enabled)
            self.assertNotEqual(updated.totp_secret, enabled.totp_secret)

    def test_admin_delete_user_protects_the_last_remaining_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            admin = store.create_user(email="admin@example.com", password="Strong-pass-1!")
            member = store.admin_create_user(
                email="member@example.com",
                password="Strong-pass-1!",
            )

            with self.assertRaisesRegex(ValueError, "last remaining admin"):
                store.admin_delete_user(email=admin.email)

            store.admin_delete_user(email=member.email)
            self.assertIsNone(store.get_user(member.email))


if __name__ == "__main__":
    unittest.main()
