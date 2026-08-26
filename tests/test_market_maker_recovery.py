from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from arbitrage_bot.config import ExchangeConfig, MarketMakerConfig, RiskConfig
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.web.background.market_maker import (
    _MarketMakerAutoRecovery,
    _market_maker_instance_task_loop,
    _market_maker_recovery_reason,
)

from tests.web_test_support import make_config


class MarketMakerAutoRecoveryTest(unittest.TestCase):
    def test_recovery_waits_for_delay_and_reschedules_failed_check(self) -> None:
        recovery = _MarketMakerAutoRecovery(delay_seconds=600.0)

        recovery.schedule("blocked_by_risk", "stale order book", now=100.0)

        self.assertTrue(recovery.waiting(now=699.0))
        self.assertFalse(recovery.begin_if_due(now=699.0))
        self.assertTrue(recovery.begin_if_due(now=700.0))
        self.assertEqual(recovery.status, "checking")
        self.assertEqual(recovery.attempt_count, 1)

        recovery.schedule("blocked_by_risk", "still stale", now=701.0)

        self.assertEqual(recovery.failure_started_at, 100.0)
        self.assertEqual(recovery.next_check_at, 1301.0)
        self.assertEqual(recovery.to_dict(now=801.0)["next_check_in_seconds"], 500.0)

    def test_recovery_can_be_marked_recovered_or_cleared_by_manual_gate(self) -> None:
        recovery = _MarketMakerAutoRecovery(delay_seconds=600.0)
        recovery.schedule("error", "temporary exchange timeout", now=10.0)
        self.assertTrue(recovery.begin_if_due(now=610.0))

        recovery.mark_recovered(now=611.0)

        self.assertEqual(recovery.status, "recovered")
        self.assertEqual(recovery.recovered_at, 611.0)
        self.assertIsNone(recovery.next_check_at)

        recovery.clear()

        self.assertEqual(recovery.status, "inactive")
        self.assertEqual(recovery.attempt_count, 0)
        self.assertIsNone(recovery.reason)

    def test_recovery_reason_prefers_specific_risk_reason(self) -> None:
        self.assertEqual(
            _market_maker_recovery_reason(
                {
                    "status": "blocked_by_risk",
                    "risk": {"reasons": ["order book gap exceeds 100 bps"]},
                }
            ),
            "order book gap exceeds 100 bps",
        )


class MarketMakerAutoRecoveryLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_cycle_rechecks_and_recovers_after_cooldown(self) -> None:
        maker = MarketMakerConfig(
            id="coinbase-acs-mm",
            enabled=True,
            live_enabled=True,
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            levels=1,
            poll_seconds=1.0,
        )
        cfg = make_config(
            market_maker=maker,
            market_makers=[maker],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                )
            ],
            risk=RiskConfig(
                enabled=True,
                trading_enabled=True,
                allow_live_trading=True,
                allow_market_maker=True,
                strategy_enabled={"market_maker": True},
                account_enabled={"coinbase-spot": True},
            ),
        )

        class FakeState:
            def __init__(self) -> None:
                self.runtimes: list[dict[str, object]] = []

            async def runtime_config(self, *_: object):
                return cfg

            async def strategy_pauses(self):
                return {}

            async def is_running(self) -> bool:
                return True

            async def coordination_hold_for(self, *_: object, **__: object):
                return None

            async def portfolio_payload(self):
                return {}

            async def set_market_maker_instance_runtime(
                self,
                _instance_id: str,
                runtime: dict[str, object],
            ) -> None:
                self.runtimes.append(runtime)

        class FakeManager:
            async def recover_pending_order_intents(self, *_: object, **__: object):
                return {"status": "ok", "unresolved_count": 0}

            def order_reliability_summary(self):
                return {"enabled": True, "pending_count": 0}

            async def close(self) -> None:
                return None

        state = FakeState()
        manager = FakeManager()
        order_book = OrderBookSnapshot(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            bids=[BookLevel(price=0.20, amount=1000.0)],
            asks=[BookLevel(price=0.21, amount=1000.0)],
        )
        blocked_payload = {
            "status": "blocked_by_risk",
            "risk": {"approved": False, "reasons": ["temporary stale book"]},
            "market_data": {},
            "plan": {"mid_price": 0.205, "orders": []},
        }
        placed_payload = {
            "status": "placed",
            "risk": {"approved": True, "reasons": []},
            "market_data": {},
            "plan": {"mid_price": 0.205, "orders": []},
            "active_plan": {"mid_price": 0.205, "orders": []},
            "execution": {
                "placed_count": 0,
                "canceled_count": 0,
                "placed_order_ids": [],
                "active_order_ids": [],
            },
        }
        open_order_snapshot = {
            "source": "exchange",
            "order_ids": [],
            "open_orders": [],
            "open_order_count": 0,
            "error": None,
        }

        with (
            patch(
                "arbitrage_bot.web.background.market_maker.ExchangeManager",
                return_value=manager,
            ),
            patch(
                "arbitrage_bot.web.background.market_maker."
                "_cached_market_maker_order_book",
                new=AsyncMock(return_value=(order_book, {})),
            ),
            patch(
                "arbitrage_bot.web.background.market_maker."
                "_market_maker_open_order_snapshot",
                new=AsyncMock(return_value=open_order_snapshot),
            ),
            patch(
                "arbitrage_bot.web.background.market_maker."
                "run_market_maker_cycle",
                new=AsyncMock(side_effect=[blocked_payload, placed_payload]),
            ) as run_cycle,
            patch("arbitrage_bot.web.background.market_maker.write_trade_event"),
            patch(
                "arbitrage_bot.web.background.market_maker."
                "write_strategy_timeline_from_payload"
            ),
        ):
            task = asyncio.create_task(
                _market_maker_instance_task_loop(
                    cfg,
                    state,  # type: ignore[arg-type]
                    maker.id,
                    auto_recovery_seconds=0.0,
                )
            )
            try:
                for _ in range(150):
                    if any(
                        runtime.get("auto_recovery", {}).get("status") == "recovered"
                        for runtime in state.runtimes
                    ):
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail(f"MM did not recover: {state.runtimes[-3:]}")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(run_cycle.await_count, 2)
        blocked_runtime = next(
            runtime
            for runtime in state.runtimes
            if runtime.get("status") == "blocked_by_risk"
        )
        self.assertEqual(blocked_runtime["auto_recovery"]["status"], "waiting")
        self.assertEqual(blocked_runtime["auto_recovery"]["delay_seconds"], 0.0)
