from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from arbitrage_bot.config import ExchangeConfig
from arbitrage_bot.exchange_rate_limit import (
    AsyncRequestPacer,
    exchange_request_pacing_policy,
    is_exchange_rate_limit_error,
)
from arbitrage_bot.exchanges import ExchangeManager


class ExchangeRateLimitPolicyTest(unittest.TestCase):
    def test_gate_spot_create_uses_restricted_fill_ratio_limit(self) -> None:
        cfg = ExchangeConfig(id="gateio", label="gate-main", market_type="spot")

        policy = exchange_request_pacing_policy(cfg, operation="create_order")

        assert policy is not None
        self.assertEqual(policy.interval_seconds, 3.5)
        self.assertEqual(policy.cooldown_seconds, 10.5)

    def test_policy_does_not_slow_other_gate_operations_or_markets(self) -> None:
        spot = ExchangeConfig(id="gateio", label="gate-main", market_type="spot")
        swap = ExchangeConfig(id="gateio", label="gate-perp", market_type="swap")

        self.assertIsNone(
            exchange_request_pacing_policy(spot, operation="cancel_order")
        )
        self.assertIsNone(
            exchange_request_pacing_policy(swap, operation="create_order")
        )

    def test_gate_rate_limit_error_is_recognized_as_confirmed_rejection(self) -> None:
        class RateLimitExceeded(Exception):
            pass

        error = RateLimitExceeded(
            'gate {"label":"TOO_MANY_REQUESTS",'
            '"message":"Request Rate Limit Exceeded (012)3/10"}'
        )

        self.assertTrue(is_exchange_rate_limit_error(error))
        self.assertFalse(is_exchange_rate_limit_error(RuntimeError("network timeout")))


class AsyncRequestPacerTest(unittest.IsolatedAsyncioTestCase):
    async def test_wait_spaces_requests_and_defer_extends_cooldown(self) -> None:
        now = 100.0
        sleeps: list[float] = []

        def clock() -> float:
            return now

        async def sleep(seconds: float) -> None:
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        pacer = AsyncRequestPacer(1.1, clock=clock, sleeper=sleep)

        self.assertEqual(await pacer.wait(), 0.0)
        self.assertAlmostEqual(await pacer.wait(), 1.1)
        await pacer.defer(10.5)
        self.assertAlmostEqual(await pacer.wait(), 10.5)
        self.assertEqual(len(sleeps), 2)
        self.assertAlmostEqual(sleeps[0], 1.1)
        self.assertAlmostEqual(sleeps[1], 10.5)

    async def test_direct_gate_order_uses_pacer_and_rate_limit_cooldown(self) -> None:
        class RateLimitExceeded(Exception):
            pass

        class FakeClient:
            async def create_order(self, *args: object) -> dict[str, object]:
                raise RateLimitExceeded("Request Rate Limit Exceeded")

        cfg = ExchangeConfig(id="gateio", label="gate-main", market_type="spot")
        manager = ExchangeManager(order_journal_path="")
        manager._clients[cfg.key] = FakeClient()

        with (
            patch(
                "arbitrage_bot.exchanges.pace_exchange_request",
                new=AsyncMock(return_value=0.0),
            ) as pace,
            patch(
                "arbitrage_bot.exchanges.defer_exchange_request",
                new=AsyncMock(),
            ) as defer,
            self.assertRaises(RateLimitExceeded),
        ):
            await manager.create_prepared_limit_order(
                cfg,
                symbol="ACS/USDT",
                side="buy",
                prepared={"amount": 10.0, "price": 0.1, "errors": []},
            )

        pace.assert_awaited_once_with(cfg, operation="create_order")
        defer.assert_awaited_once_with(cfg, operation="create_order")
