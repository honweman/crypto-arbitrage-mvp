from __future__ import annotations

import unittest

from arbitrage_bot.web.verification import (
    EmailVerificationManager,
    VerificationRateLimited,
)


class EmailVerificationManagerTest(unittest.TestCase):
    def test_code_is_single_use_and_scoped_to_purpose(self) -> None:
        manager = EmailVerificationManager(resend_seconds=10)
        code = manager.issue(
            email="Trader@Example.com",
            purpose="register",
            now=100.0,
        )

        self.assertEqual(len(code), 6)
        self.assertFalse(
            manager.verify(
                email="trader@example.com",
                purpose="password_reset",
                code=code,
                now=101.0,
            )
        )
        self.assertTrue(
            manager.verify(
                email="trader@example.com",
                purpose="register",
                code=code,
                now=101.0,
            )
        )
        self.assertFalse(
            manager.verify(
                email="trader@example.com",
                purpose="register",
                code=code,
                now=102.0,
            )
        )

    def test_expired_and_repeatedly_incorrect_codes_are_rejected(self) -> None:
        manager = EmailVerificationManager(
            ttl_seconds=60,
            resend_seconds=10,
            max_attempts=2,
        )
        code = manager.issue(
            email="trader@example.com",
            purpose="register",
            now=100.0,
        )
        self.assertFalse(
            manager.verify(
                email="trader@example.com",
                purpose="register",
                code="000000" if code != "000000" else "000001",
                now=101.0,
            )
        )
        self.assertFalse(
            manager.verify(
                email="trader@example.com",
                purpose="register",
                code="999999" if code != "999999" else "999998",
                now=102.0,
            )
        )
        self.assertFalse(
            manager.verify(
                email="trader@example.com",
                purpose="register",
                code=code,
                now=103.0,
            )
        )

        expired = manager.issue(
            email="expired@example.com",
            purpose="password_reset",
            now=200.0,
        )
        self.assertFalse(
            manager.verify(
                email="expired@example.com",
                purpose="password_reset",
                code=expired,
                now=261.0,
            )
        )

    def test_resend_and_hourly_limits(self) -> None:
        manager = EmailVerificationManager(
            resend_seconds=10,
            max_sends_per_hour=2,
        )
        manager.issue(email="trader@example.com", purpose="register", now=100.0)
        with self.assertRaises(VerificationRateLimited):
            manager.issue(email="trader@example.com", purpose="register", now=105.0)
        manager.issue(email="trader@example.com", purpose="register", now=111.0)
        with self.assertRaises(VerificationRateLimited):
            manager.issue(email="trader@example.com", purpose="register", now=122.0)


if __name__ == "__main__":
    unittest.main()
