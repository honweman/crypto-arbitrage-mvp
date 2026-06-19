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


if __name__ == "__main__":
    unittest.main()
