from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import (
    CrossExchangeRebalanceConfig,
    MarketMakerConfig,
    StrategyTimelineConfig,
    load_config,
)
from arbitrage_bot.cross_exchange_rebalancer import (
    new_rebalance_runtime,
    save_rebalance_runtime,
)
from arbitrage_bot.strategy_timeline import write_strategy_timeline_from_payload
from arbitrage_bot.web.coordination import (
    coordination_blocked_sides,
    market_maker_coordination_status,
    market_maker_resources_coordination_status,
    rebalance_coordination_hold_required,
)
from arbitrage_bot.web.loops import (
    RebalanceMarketDataTimeout,
    _fetch_rebalance_books,
    _market_maker_instance_task_loop,
    _auto_buy_sell_coordination_required,
    _refresh_rebalance_runtime_from_state,
    _sleep_for_rebalance_config_change,
    cross_exchange_rebalance_task_loop,
)
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
    async def test_rebalance_loop_uses_newer_runtime_published_by_api(self) -> None:
        cfg = make_config()
        stale = new_rebalance_runtime(
            cfg.cross_exchange_rebalance,
            common_quote_currency=cfg.common_quote_currency,
        )
        stale.update(
            {
                "status": "halted",
                "halted": True,
                "halt_reason": "hedge_required",
                "updated_at": 10.0,
            }
        )
        reset = new_rebalance_runtime(
            cfg.cross_exchange_rebalance,
            common_quote_currency=cfg.common_quote_currency,
        )
        reset.update({"status": "starting", "updated_at": 20.0})

        class FakeState:
            async def cross_exchange_rebalance_runtime(self):
                return reset

        refreshed = await _refresh_rebalance_runtime_from_state(
            FakeState(),  # type: ignore[arg-type]
            stale,
            config_fingerprint=stale["config_fingerprint"],
        )

        self.assertFalse(refreshed["halted"])
        self.assertEqual(refreshed["status"], "starting")

    async def test_rebalance_interval_wait_wakes_for_a_config_change(self) -> None:
        cfg = make_config()
        changed_cfg = replace(
            cfg,
            cross_exchange_rebalance=replace(
                cfg.cross_exchange_rebalance,
                enabled=False,
                live_enabled=False,
            ),
        )

        class ChangedState:
            async def runtime_config(self, *_: object):
                return changed_cfg

        started = time.monotonic()
        await _sleep_for_rebalance_config_change(
            cfg,
            ChangedState(),  # type: ignore[arg-type]
            cfg.cross_exchange_rebalance,
            0.01,
        )

        self.assertLess(time.monotonic() - started, 0.2)

    async def test_rebalance_book_refresh_has_an_outer_timeout(self) -> None:
        cfg = make_config()

        class HangingManager:
            async def fetch_order_book(self, *_: object, **__: object):
                await asyncio.Event().wait()

        with self.assertRaisesRegex(
            RebalanceMarketDataTimeout,
            "order book refresh exceeded 0.1s",
        ):
            await _fetch_rebalance_books(
                cfg,
                HangingManager(),  # type: ignore[arg-type]
                timeout_seconds=0.1,
            )

    async def test_rebalance_loop_records_timeout_and_keeps_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config()
            cfg = replace(
                cfg,
                cross_exchange_rebalance=replace(
                    cfg.cross_exchange_rebalance,
                    interval_seconds=1.0,
                    runtime_path=str(Path(tmp) / "rebalance.json"),
                ),
            )

            class FakeState:
                def __init__(self) -> None:
                    self.runtimes: list[dict[str, object]] = []

                async def runtime_config(self, *_: object):
                    return cfg

                async def set_cross_exchange_rebalance_runtime(
                    self, runtime: dict[str, object]
                ) -> None:
                    self.runtimes.append(runtime)

                async def strategy_pauses(self):
                    return {}

                async def is_running(self) -> bool:
                    return True

                async def quote_rates(self):
                    return cfg.quote_rates

                async def release_coordination_hold(self, *_: object) -> bool:
                    return True

            class FakeManager:
                async def close(self) -> None:
                    return None

            state = FakeState()
            timeout = RebalanceMarketDataTimeout("test market data timeout")
            with (
                patch(
                    "arbitrage_bot.web.loops.ExchangeManager",
                    return_value=FakeManager(),
                ),
                patch(
                    "arbitrage_bot.web.loops._fetch_rebalance_books",
                    side_effect=timeout,
                ) as fetch_books,
                patch("arbitrage_bot.web.loops.write_trade_event"),
                patch("arbitrage_bot.web.loops.write_strategy_timeline_from_payload"),
            ):
                task = asyncio.create_task(
                    cross_exchange_rebalance_task_loop(cfg, state)  # type: ignore[arg-type]
                )
                try:
                    for _ in range(100):
                        if any(
                            runtime.get("status") == "waiting_for_market_data"
                            for runtime in state.runtimes
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("rebalance loop did not persist the timeout state")
                    self.assertFalse(task.done())
                    self.assertGreaterEqual(fetch_books.call_count, 1)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

    async def test_rebalance_loop_persists_disabled_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config()
            cfg = replace(
                cfg,
                cross_exchange_rebalance=replace(
                    cfg.cross_exchange_rebalance,
                    enabled=False,
                    live_enabled=False,
                    runtime_path=str(Path(tmp) / "rebalance.json"),
                ),
            )

            class FakeState:
                def __init__(self) -> None:
                    self.runtimes: list[dict[str, object]] = []

                async def runtime_config(self, *_: object):
                    return cfg

                async def set_cross_exchange_rebalance_runtime(
                    self, runtime: dict[str, object]
                ) -> None:
                    self.runtimes.append(runtime)

                async def strategy_pauses(self):
                    return {}

                async def is_running(self) -> bool:
                    return True

                async def acquire_coordination_hold(self, *_: object, **__: object):
                    return {"owner": "cross_exchange_rebalance"}

                async def release_coordination_hold(self, *_: object) -> bool:
                    return True

            class FakeManager:
                async def close(self) -> None:
                    return None

            state = FakeState()
            with (
                patch(
                    "arbitrage_bot.web.loops.ExchangeManager",
                    return_value=FakeManager(),
                ),
                patch("arbitrage_bot.web.loops.write_trade_event"),
                patch("arbitrage_bot.web.loops.write_strategy_timeline_from_payload"),
            ):
                task = asyncio.create_task(
                    cross_exchange_rebalance_task_loop(cfg, state)  # type: ignore[arg-type]
                )
                try:
                    for _ in range(100):
                        if state.runtimes:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("rebalance loop did not persist disabled state")
                    persisted = json.loads(
                        Path(cfg.cross_exchange_rebalance.runtime_path).read_text()
                    )
                    self.assertEqual(persisted["status"], "disabled")
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

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

    async def test_directional_hold_records_only_the_conflicting_side(self) -> None:
        state = MonitorState(make_config(), 1.0)
        hold = await state.acquire_coordination_hold(
            "auto_buy_sell:auto-1",
            [("coinbase-spot", "ACS/USDC", "sell")],
            reason="Auto Buy withdraws MM asks",
            ttl_seconds=30.0,
        )

        self.assertEqual(
            coordination_blocked_sides(
                hold,
                "coinbase-spot",
                "ACS/USDC",
            ),
            {"sell"},
        )
        self.assertEqual(hold["resources"][0]["side"], "sell")

    async def test_directional_hold_retains_non_conflicting_mm_side(self) -> None:
        cfg = make_config()
        maker = market_maker_configs_for_runtime(cfg)[0]
        state = MonitorState(cfg, 1.0)
        owner = "auto_buy_sell:auto-1"
        await state.acquire_coordination_hold(
            owner,
            [(maker.exchange, maker.symbol, "sell")],
            reason="Auto Buy withdraws MM asks",
            ttl_seconds=30.0,
        )

        class FakeManager:
            def __init__(self) -> None:
                self.orders = {
                    "mm-buy": {"id": "mm-buy", "side": "buy"},
                    "mm-sell": {"id": "mm-sell", "side": "sell"},
                }
                self.canceled_ids: list[str] = []

            async def fetch_open_orders(self, *_: object, **__: object):
                return list(self.orders.values())

            async def cancel_orders(
                self,
                *_: object,
                order_ids: list[str],
                **__: object,
            ):
                self.canceled_ids.extend(order_ids)
                return [self.orders.pop(order_id) for order_id in order_ids]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ):
                self.canceled_ids.append(order_id)
                return self.orders.pop(order_id)

            async def close(self) -> None:
                return None

        manager = FakeManager()
        with (
            patch("arbitrage_bot.web.loops.ExchangeManager", return_value=manager),
            patch("arbitrage_bot.web.loops.write_trade_event"),
            patch("arbitrage_bot.web.loops.write_strategy_timeline_from_payload"),
        ):
            loop_task = asyncio.create_task(
                _market_maker_instance_task_loop(cfg, state, maker.id)
            )
            try:
                for _ in range(100):
                    runtime = await state.market_maker_runtime()
                    instance = next(
                        (
                            item
                            for item in runtime.get("instances", [])
                            if item.get("id") == maker.id
                        ),
                        {},
                    )
                    if (
                        instance.get("status") == "coordinating"
                        and instance.get(
                            "coordination_conflicting_open_order_count"
                        )
                        == 0
                    ):
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail(
                        "directional MM coordination was not acknowledged: "
                        f"{instance}"
                    )
            finally:
                loop_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop_task

        self.assertEqual(manager.canceled_ids, ["mm-sell"])
        self.assertEqual(instance["open_order_ids"], ["mm-buy"])
        self.assertEqual(instance["coordination_retained_open_order_count"], 1)
        status = market_maker_resources_coordination_status(
            cfg,
            {"instances": [instance]},
            resources=[(maker.exchange, maker.symbol, "sell")],
            owner=owner,
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["instances"][0]["open_order_count"], 1)
        self.assertEqual(
            status["instances"][0]["conflicting_open_order_count"],
            0,
        )

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

    async def test_rebalance_loop_recovers_legacy_residual_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = str(Path(tmp) / "rebalance.json")
            timeline_path = str(Path(tmp) / "strategy_timeline.jsonl")
            cfg = make_config()
            cfg = replace(
                cfg,
                cross_exchange_rebalance=replace(
                    cfg.cross_exchange_rebalance,
                    interval_seconds=1.0,
                    runtime_path=runtime_path,
                ),
                strategy_timeline=StrategyTimelineConfig(
                    enabled=True,
                    path=timeline_path,
                ),
            )
            runtime = new_rebalance_runtime(
                cfg.cross_exchange_rebalance,
                common_quote_currency=cfg.common_quote_currency,
            )
            runtime.update(
                {
                    "status": "halted",
                    "halted": True,
                    "halt_reason": "hedge_required",
                }
            )
            save_rebalance_runtime(runtime_path, runtime)
            write_strategy_timeline_from_payload(
                cfg.strategy_timeline,
                {
                    "type": "cross_exchange_rebalance_execution",
                    "strategy": "cross_exchange_rebalance",
                    "mode": "live",
                    "status": "hedge_required",
                    "execution": {"fill_status": {"imbalance_base": -42.0}},
                },
            )

            class FakeState:
                def __init__(self) -> None:
                    self.runtimes: list[dict[str, object]] = []

                async def runtime_config(self, *_: object):
                    return cfg

                async def set_cross_exchange_rebalance_runtime(
                    self, value: dict[str, object]
                ) -> None:
                    self.runtimes.append(value)

                async def strategy_pauses(self):
                    return {}

                async def is_running(self) -> bool:
                    return True

                async def acquire_coordination_hold(self, *_: object, **__: object):
                    return {"owner": "cross_exchange_rebalance"}

                async def release_coordination_hold(self, *_: object) -> bool:
                    return True

            class FakeManager:
                async def close(self) -> None:
                    return None

            state = FakeState()
            with (
                patch(
                    "arbitrage_bot.web.loops.ExchangeManager",
                    return_value=FakeManager(),
                ),
                patch("arbitrage_bot.web.loops.write_trade_event"),
                patch("arbitrage_bot.web.loops.write_strategy_timeline_from_payload"),
            ):
                task = asyncio.create_task(
                    cross_exchange_rebalance_task_loop(cfg, state)  # type: ignore[arg-type]
                )
                try:
                    for _ in range(100):
                        if any(
                            isinstance(item.get("residual_exposure"), dict)
                            for item in state.runtimes
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("rebalance loop did not recover legacy residual")
                    recovered = next(
                        item
                        for item in reversed(state.runtimes)
                        if isinstance(item.get("residual_exposure"), dict)
                    )
                    residual = recovered["residual_exposure"]
                    self.assertEqual(residual["asset"], "ACS")
                    self.assertEqual(residual["side"], "buy")
                    self.assertEqual(residual["quantity_base"], 42.0)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

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
    def test_auto_buy_sell_coordination_requires_explicit_opt_in(self) -> None:
        blocked_guard = {
            "blocked": True,
            "reasons": ["self-trade guard: market maker is live"],
        }
        task = {
            "id": "auto-1",
            "status": "blocked_by_risk",
            "config": {
                "block_conflicting_market_maker": True,
                "coordinate_market_maker": False,
            },
            "last_risk": {"self_trade_guard": blocked_guard},
        }
        self.assertFalse(
            _auto_buy_sell_coordination_required(
                task,
                already_coordinating=False,
            )
        )
        task["config"]["coordinate_market_maker"] = True
        self.assertTrue(
            _auto_buy_sell_coordination_required(
                task,
                already_coordinating=False,
            )
        )
        task["last_risk"] = {
            "self_trade_guard": {"blocked": False},
            "reasons": ["maximum exposure exceeded"],
        }
        self.assertFalse(
            _auto_buy_sell_coordination_required(
                task,
                already_coordinating=True,
            )
        )

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
                        "coordination_hold": {"owner": "cross_exchange_rebalance"},
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
        self.assertFalse(rebalance_coordination_hold_required({"status": "progress"}))
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
