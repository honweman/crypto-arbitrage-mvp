from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.web.users import (
    WebUserStore,
    normalize_assets,
    totp_code,
    verify_password,
    verify_totp,
)


class WebUserStoreTest(unittest.TestCase):
    def test_normalizes_assets(self) -> None:
        self.assertEqual(normalize_assets("acs, BTC acs"), ["ACS", "BTC"])

    def test_registers_user_and_authenticates_with_totp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            user = store.create_user(
                email="Trader@Example.com",
                password="strong-password",
                allowed_assets="acs,btc",
            )

            self.assertEqual(user.email, "trader@example.com")
            self.assertEqual(user.allowed_assets, ["ACS", "BTC"])
            self.assertTrue(verify_password("strong-password", user.password_hash))
            self.assertTrue(verify_totp(user.totp_secret, totp_code(user.totp_secret)))
            self.assertIsNotNone(
                store.authenticate(
                    email="trader@example.com",
                    password="strong-password",
                    totp=totp_code(user.totp_secret),
                )
            )
            self.assertIsNone(
                store.authenticate(
                    email="trader@example.com",
                    password="wrong-password",
                    totp=totp_code(user.totp_secret),
                )
            )

    def test_profile_preferred_asset_must_be_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(
                email="trader@example.com",
                password="strong-password",
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
            admin = store.create_user(email="admin@example.com", password="strong-password")
            member = store.admin_create_user(
                email="member@example.com",
                password="strong-password",
                allowed_assets=["ACS", "BTC"],
                preferred_asset="acs",
            )

            self.assertEqual(admin.role, "admin")
            self.assertEqual(member.role, "user")
            self.assertEqual(member.preferred_asset, "ACS")
            with self.assertRaisesRegex(ValueError, "preferred asset"):
                store.admin_create_user(
                    email="other@example.com",
                    password="strong-password",
                    allowed_assets=["ACS"],
                    preferred_asset="BTC",
                )
            with self.assertRaisesRegex(ValueError, "role must be"):
                store.admin_create_user(
                    email="other@example.com",
                    password="strong-password",
                    role="superuser",
                )

    def test_admin_set_role_protects_the_last_remaining_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            admin = store.create_user(email="admin@example.com", password="strong-password")

            with self.assertRaisesRegex(ValueError, "last remaining admin"):
                store.admin_set_role(email=admin.email, role="user")

            second_admin = store.admin_create_user(
                email="second-admin@example.com",
                password="strong-password",
                role="admin",
            )
            demoted = store.admin_set_role(email=second_admin.email, role="user")
            self.assertEqual(demoted.role, "user")
            # Now that only one admin remains, it is protected again.
            with self.assertRaisesRegex(ValueError, "last remaining admin"):
                store.admin_set_role(email=admin.email, role="user")

    def test_admin_set_allowed_assets_validates_preferred_asset_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(email="admin@example.com", password="strong-password")
            member = store.admin_create_user(
                email="member@example.com",
                password="strong-password",
                allowed_assets=["ACS"],
            )

            with self.assertRaisesRegex(ValueError, "preferred asset"):
                store.admin_set_allowed_assets(
                    email=member.email,
                    allowed_assets=["ACS"],
                    preferred_asset="BTC",
                )

            updated = store.admin_set_allowed_assets(
                email=member.email,
                allowed_assets=["ACS", "BTC"],
                preferred_asset="BTC",
            )
            self.assertEqual(updated.allowed_assets, ["ACS", "BTC"])
            self.assertEqual(updated.preferred_asset, "BTC")

    def test_admin_reset_password_replaces_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            member = store.create_user(email="member@example.com", password="strong-password")

            store.admin_reset_password(email=member.email, new_password="new-strong-password")

            self.assertIsNone(
                store.authenticate(
                    email=member.email,
                    password="strong-password",
                    totp=totp_code(member.totp_secret),
                )
            )
            self.assertIsNotNone(
                store.authenticate(
                    email=member.email,
                    password="new-strong-password",
                    totp=totp_code(member.totp_secret),
                )
            )

    def test_admin_delete_user_protects_the_last_remaining_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            admin = store.create_user(email="admin@example.com", password="strong-password")
            member = store.admin_create_user(
                email="member@example.com",
                password="strong-password",
            )

            with self.assertRaisesRegex(ValueError, "last remaining admin"):
                store.admin_delete_user(email=admin.email)

            store.admin_delete_user(email=member.email)
            self.assertIsNone(store.get_user(member.email))


if __name__ == "__main__":
    unittest.main()
