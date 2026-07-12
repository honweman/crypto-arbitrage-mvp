from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from arbitrage_bot.config import (
    CrossExchangeRebalanceConfig,
    MarketMakerConfig,
    load_config,
)
from arbitrage_bot.web.coordination import (
    market_maker_coordination_status,
    rebalance_coordination_hold_required,
)
from arbitrage_bot.web.loops import _market_maker_instance_task_loop
from arbitrage_bot.web.state import MonitorState
from arbitrage_bot.web_config import (
    cross_exchange_rebalance_config_from_payload,
    market_maker_configs_for_runtime,
)


def make_config():
    cfg = load_config("config.acs.example.json")
    market_makers = [
        MarketMakerConfig(
            enabled=True,
            live_enabled=True,
            exchange="coinbase-spot",
            symbol="ACS/USDC",
        ),
        MarketMakerConfig(
            enabled=True,
            live_enabled=True,
            exchange="upbit-spot",
            symbol="BTC/USDT",
        ),
    ]
    rebalance = CrossExchangeRebalanceConfig(
        enabled=True,
        live_enabled=True,
        buy_exchange="bithumb-spot",
        buy_symbol="ACS/KRW",
        sell_exchange="coinbase-spot",
        sell_symbol="ACS/USDC",
        total_quote_common=100.0,
        quote_per_cycle_common=10.0,
    )
    return replace(
        cfg,
        market_maker=market_makers[0],
        market_makers=market_makers,
        cross_exchange_rebalance=rebalance,
    )


class CoordinationStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_hold_is_scoped_and_released(self) -> None:
        state = MonitorState(make_config(), 1.0)
        hold = await state.acquire_coordination_hold(
            "cross_exchange_rebalance",
            [
                ("bithumb-spot", "ACS/KRW"),
                ("coinbase-spot", "ACS/USDC"),
            ],
            reason="test handoff",
            ttl_seconds=30.0,
        )

        self.assertEqual(len(hold["resources"]), 2)
        blocker = await state.coordination_hold_for(
            "coinbase-spot",
            "ACS/USDC",
            requester="market_maker:coinbase",
        )
        unrelated = await state.coordination_hold_for(
            "upbit-spot",
            "BTC/USDT",
            requester="market_maker:upbit",
        )
        owner_view = await state.coordination_hold_for(
            "coinbase-spot",
            "ACS/USDC",
            requester="cross_exchange_rebalance",
        )

        self.assertEqual(blocker["owner"], "cross_exchange_rebalance")
        self.assertIsNone(unrelated)
        self.assertIsNone(owner_view)
        self.assertTrue(
            await state.release_coordination_hold("cross_exchange_rebalance")
        )
        self.assertEqual(await state.coordination_holds(), [])

    async def test_mm_acknowledges_only_after_exchange_confirms_cancellation(
        self,
    ) -> None:
        cfg = make_config()
        maker = market_maker_configs_for_runtime(cfg)[0]
        state = MonitorState(cfg, 1.0)
        await state.acquire_coordination_hold(
            "cross_exchange_rebalance",
            [(maker.exchange, maker.symbol)],
            reason="test handoff",
            ttl_seconds=30.0,
        )

        class FakeManager:
            def __init__(self) -> None:
                self.orders = {
                    "mm-1": {"id": "mm-1", "side": "buy"},
                    "mm-2": {"id": "mm-2", "side": "sell"},
                }
                self.cancel_calls = 0

            async def fetch_open_orders(self, *_: object, **__: object):
                return list(self.orders.values())

            async def cancel_orders(
                self,
                *_: object,
                order_ids: list[str],
                **__: object,
            ):
                self.cancel_calls += 1
                canceled = [self.orders.pop(order_id) for order_id in order_ids]
                return canceled

            async def close(self) -> None:
                return None

        manager = FakeManager()
        with (
            patch("arbitrage_bot.web.loops.ExchangeManager", return_value=manager),
            patch("arbitrage_bot.web.loops.write_trade_event"),
            patch("arbitrage_bot.web.loops.write_strategy_timeline_from_payload"),
        ):
            task = asyncio.create_task(
                _market_maker_instance_task_loop(cfg, state, maker.id)
            )
            try:
                for _ in range(50):
                    runtime = await state.market_maker_runtime()
                    instance = next(
                        (
                            item
                            for item in runtime.get("instances", [])
                            if item.get("id") == maker.id
                        ),
                        {},
                    )
                    if instance.get("status") == "coordinating":
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("MM did not acknowledge coordination")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(manager.cancel_calls, 1)
        self.assertEqual(instance["open_order_count"], 0)
        self.assertEqual(
            instance["coordination_hold"]["owner"],
            "cross_exchange_rebalance",
        )

    async def test_mm_keeps_hold_when_cancellation_cannot_be_confirmed(self) -> None:
        cfg = make_config()
        maker = market_maker_configs_for_runtime(cfg)[0]
        state = MonitorState(cfg, 1.0)
        await state.acquire_coordination_hold(
            "cross_exchange_rebalance",
            [(maker.exchange, maker.symbol)],
            reason="test handoff",
            ttl_seconds=30.0,
        )

        class FailingCancelManager:
            async def fetch_open_orders(self, *_: object, **__: object):
                return [
                    {"id": "mm-1", "side": "buy"},
                    {"id": "mm-2", "side": "sell"},
                ]

            async def cancel_orders(self, *_: object, **__: object):
                raise RuntimeError("batch cancel unavailable")

            async def cancel_order(self, *_: object, **__: object):
                raise RuntimeError("cancel rejected")

            async def close(self) -> None:
                return None

        manager = FailingCancelManager()
        with (
            patch("arbitrage_bot.web.loops.ExchangeManager", return_value=manager),
            patch("arbitrage_bot.web.loops.write_trade_event"),
            patch("arbitrage_bot.web.loops.write_strategy_timeline_from_payload"),
        ):
            task = asyncio.create_task(
                _market_maker_instance_task_loop(cfg, state, maker.id)
            )
            try:
                for _ in range(50):
                    runtime = await state.market_maker_runtime()
                    instance = next(
                        (
                            item
                            for item in runtime.get("instances", [])
                            if item.get("id") == maker.id
                        ),
                        {},
                    )
                    if instance.get("status") == "coordination_cancel_retry":
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("MM did not enter coordination cancel retry")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(instance["open_order_count"], 2)
        self.assertEqual(instance["mode"], "paused")
        self.assertEqual(
            instance["coordination_hold"]["owner"],
            "cross_exchange_rebalance",
        )


class CoordinationStatusTest(unittest.TestCase):
    def test_matching_mm_must_acknowledge_and_clear_orders(self) -> None:
        cfg = make_config()
        maker = market_maker_configs_for_runtime(cfg)[0]
        waiting = market_maker_coordination_status(
            cfg,
            {
                "instances": [
                    {
                        "id": maker.id,
                        "status": "placed",
                        "open_order_count": 4,
                        "coordination_hold": None,
                    }
                ]
            },
            owner="cross_exchange_rebalance",
        )
        ready = market_maker_coordination_status(
            cfg,
            {
                "instances": [
                    {
                        "id": maker.id,
                        "status": "coordinating",
                        "open_order_count": 0,
                        "open_order_sync_error": None,
                        "coordination_hold": {
                            "owner": "cross_exchange_rebalance"
                        },
                    }
                ]
            },
            owner="cross_exchange_rebalance",
        )

        self.assertFalse(waiting["ready"])
        self.assertEqual(waiting["affected_instance_count"], 1)
        self.assertTrue(any("acknowledgement" in item for item in waiting["reasons"]))
        self.assertTrue(any("4 open" in item for item in waiting["reasons"]))
        self.assertTrue(ready["ready"])

    def test_safety_hold_only_persists_for_unresolved_execution(self) -> None:
        self.assertFalse(
            rebalance_coordination_hold_required({"status": "progress"})
        )
        self.assertTrue(
            rebalance_coordination_hold_required({"status": "blocked_by_conflict"})
        )
        self.assertTrue(
            rebalance_coordination_hold_required(
                {
                    "status": "execution_error",
                    "execution": {"remaining_open_order_ids": ["order-1"]},
                }
            )
        )

    def test_coordination_config_validates_timeout(self) -> None:
        configured = cross_exchange_rebalance_config_from_payload(
            {
                "coordinate_market_maker": True,
                "coordination_timeout_seconds": 45,
            }
        )
        self.assertTrue(configured.coordinate_market_maker)
        self.assertEqual(configured.coordination_timeout_seconds, 45.0)
        with self.assertRaisesRegex(ValueError, "between 1 and 300"):
            cross_exchange_rebalance_config_from_payload(
                {"coordination_timeout_seconds": 0.5}
            )


if __name__ == "__main__":
    unittest.main()
