from __future__ import annotations

import os
import unittest
from unittest.mock import patch


from arbitrage_bot.config import (
    WebSecurityConfig,
)
from arbitrage_bot.web import (
    APP_JS,
    HTML as INDEX_HTML,
    LoginRateLimiter,
    _cookie_secret,
)
from tests.web_test_support import make_config


HTML = f"{INDEX_HTML}\n{APP_JS}"


class LoginRateLimiterTest(unittest.TestCase):
    def test_locks_out_after_max_failures_and_recovers(self) -> None:
        limiter = LoginRateLimiter(
            max_failures=3,
            window_seconds=100.0,
            lockout_seconds=60.0,
        )
        key = "203.0.113.7"

        self.assertEqual(limiter.retry_after(key, now=0.0), 0.0)
        self.assertEqual(limiter.register_failure(key, now=1.0), 0.0)
        self.assertEqual(limiter.register_failure(key, now=2.0), 0.0)
        # Third failure crosses the threshold and triggers the lockout.
        self.assertEqual(limiter.register_failure(key, now=3.0), 60.0)
        self.assertEqual(limiter.retry_after(key, now=3.0), 60.0)
        self.assertAlmostEqual(limiter.retry_after(key, now=33.0), 30.0)
        # After the lockout window expires the client may try again.
        self.assertEqual(limiter.retry_after(key, now=64.0), 0.0)

    def test_success_clears_failure_history(self) -> None:
        limiter = LoginRateLimiter(max_failures=3, window_seconds=100.0)
        key = "203.0.113.8"
        limiter.register_failure(key, now=1.0)
        limiter.register_failure(key, now=2.0)
        limiter.register_success(key)
        # History reset, so two more failures must not lock the client out.
        self.assertEqual(limiter.register_failure(key, now=3.0), 0.0)
        self.assertEqual(limiter.register_failure(key, now=4.0), 0.0)
        self.assertEqual(limiter.retry_after(key, now=4.0), 0.0)

    def test_old_failures_outside_window_are_forgotten(self) -> None:
        limiter = LoginRateLimiter(max_failures=3, window_seconds=100.0)
        key = "203.0.113.9"
        limiter.register_failure(key, now=1.0)
        limiter.register_failure(key, now=2.0)
        # This failure is far outside the window; the earlier two have aged out.
        self.assertEqual(limiter.register_failure(key, now=500.0), 0.0)
        self.assertEqual(limiter.retry_after(key, now=500.0), 0.0)

    def test_clients_are_tracked_independently(self) -> None:
        limiter = LoginRateLimiter(
            max_failures=2, window_seconds=100.0, lockout_seconds=60.0
        )
        limiter.register_failure("10.0.0.1", now=1.0)
        self.assertEqual(limiter.register_failure("10.0.0.1", now=2.0), 60.0)
        # A different client IP is unaffected by the first one's lockout.
        self.assertEqual(limiter.retry_after("10.0.0.2", now=2.0), 0.0)
        self.assertEqual(limiter.register_failure("10.0.0.2", now=2.0), 0.0)


class CookieSecretTest(unittest.TestCase):
    def test_unconfigured_secret_is_random_not_a_known_constant(self) -> None:
        cfg = make_config(
            web_security=WebSecurityConfig(
                password_env=None,
                cookie_secret_env=None,
            )
        )
        secret = _cookie_secret(cfg)
        self.assertTrue(secret)
        self.assertNotEqual(secret, "crypto-arbitrage-dev")
        # Stable within the process so existing sessions stay valid.
        self.assertEqual(secret, _cookie_secret(cfg))

    def test_explicit_cookie_secret_env_takes_precedence(self) -> None:
        cfg = make_config(
            web_security=WebSecurityConfig(
                password_env="WEB_PW_TEST",
                cookie_secret_env="COOKIE_SECRET_TEST",
            )
        )
        with patch.dict(
            os.environ,
            {"COOKIE_SECRET_TEST": "configured-secret", "WEB_PW_TEST": "pw"},
        ):
            self.assertEqual(_cookie_secret(cfg), "configured-secret")
