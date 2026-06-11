from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional

from arbitrage_bot.auto_buy_sell_task import (
    AutoBuySellTaskService,
    AutoBuySellTaskStore,
    validate_task_config,
)
from arbitrage_bot.config import (
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PnlStoreConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    TradeLogConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


class FakeTaskManager:
    def __init__(self) -> None:
        self.created = 0
        self.canceled = 0
        self.bid_price = 0.00014
        self.ask_price = 0.00016
        self.open_order_ids: list[str] = []
        self.closed_orders: list[dict[str, object]] = []
        self.trades: list[dict[str, object]] = []

    async def fetch_order_book(
        self,
        *_: object,
        **__: object,
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=self.bid_price, amount=100_000)],
            asks=[BookLevel(price=self.ask_price, amount=100_000)],
        )

    async def fetch_open_orders(
        self,
        *_: object,
        **__: object,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": order_id,
                "symbol": "ACS/USDT",
                "status": "open",
                "filled": 0.0,
                "remaining": 5.0,
                "cost": 0.0,
            }
            for order_id in self.open_order_ids
        ]

    async def fetch_closed_orders(
        self,
        *_: object,
        **__: object,
    ) -> list[dict[str, object]]:
        return self.closed_orders

    async def fetch_my_trades(
        self,
        *_: object,
        **__: object,
    ) -> list[dict[str, object]]:
        return self.trades

    async def prepare_limit_order(
        self,
        *_: object,
        **__: object,
    ) -> dict[str, object]:
        return {
            "exchange": "bybit-spot",
            "symbol": "ACS/USDT",
            "side": "buy",
            "status": "ok",
            "requested_amount": 5.0,
            "requested_price": 0.00016,
            "amount": 5.0,
            "price": 0.00016,
            "cost": 0.0008,
            "limits": {},
            "precision": {},
            "errors": [],
            "warnings": [],
        }

    async def create_limit_order(
        self,
        *_: object,
        **__: object,
    ) -> dict[str, object]:
        self.created += 1
        order_id = f"order-{self.created}"
        self.open_order_ids.append(order_id)
        return {"id": order_id}

    async def cancel_order(
        self,
        *_: object,
        order_id: str,
        **__: object,
    ) -> dict[str, object]:
        self.canceled += 1
        self.open_order_ids = [
            item for item in self.open_order_ids if item != order_id
        ]
        return {"id": order_id, "status": "canceled"}


class AutoBuySellTaskTest(unittest.IsolatedAsyncioTestCase):
    async def test_task_store_round_trips_and_pause_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            task = await service.create_task(self._slow_cfg())

            paused = await service.set_paused(task["id"], True)
            resumed = await service.set_paused(task["id"], False)
            loaded = AutoBuySellTaskStore(Path(tmp) / "tasks.json").load()

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, task["id"])
        self.assertEqual(loaded[0].status, "running")

    async def test_task_progress_uses_fills_not_submitted_amount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            task = await service.create_task(self._slow_cfg())
            manager = FakeTaskManager()
            cfg = self._cfg(tmp)

            first = await service.run_due_tasks(cfg, manager)
            first_task = first["tasks"][0]
            self.assertEqual(manager.created, 1)
            self.assertEqual(first_task["filled_base"], 0.0)
            self.assertEqual(first_task["open_order_ids"], ["order-1"])

            manager.open_order_ids = []
            manager.trades = [
                {
                    "id": "trade-1",
                    "order": "order-1",
                    "amount": 2.0,
                    "cost": 0.0003,
                    "timestamp": 1_000_000,
                }
            ]
            service._tasks[0].order_created_at["order-1"] = time.time() - 2.0
            service._tasks[0].next_run_at = 0.0
            second = await service.run_due_tasks(cfg, manager)
            second_task = second["tasks"][0]

            manager.open_order_ids = []
            manager.trades = []
            service._tasks[0].order_created_at["order-2"] = time.time() - 2.0
            service._tasks[0].next_run_at = 0.0
            third = await service.run_due_tasks(cfg, manager)
            third_task = third["tasks"][0]

        self.assertEqual(task["status"], "running")
        self.assertEqual(manager.created, 3)
        self.assertEqual(second_task["filled_base"], 2.0)
        self.assertAlmostEqual(second_task["last_plan"]["submitted_base"], 2.0)
        self.assertEqual(second_task["open_order_ids"], ["order-2"])
        self.assertEqual(third_task["filled_base"], 2.0)

    async def test_task_waits_for_start_price_then_remains_triggered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            slow_cfg = self._slow_cfg(
                total_base=15.0,
                slice_base_min=5.0,
                start_price=0.00015,
                stop_price=0.00013,
                side="sell",
            )
            await service.create_task(slow_cfg)
            manager = FakeTaskManager()
            manager.bid_price = 0.00014
            manager.ask_price = 0.00016
            cfg = self._cfg(tmp, slow_execution=slow_cfg)

            waiting = await service.run_due_tasks(cfg, manager)
            waiting_task = waiting["tasks"][0]

            manager.bid_price = 0.00015
            manager.ask_price = 0.00016
            service._tasks[0].next_run_at = 0.0
            triggered = await service.run_due_tasks(cfg, manager)
            triggered_task = triggered["tasks"][0]

            manager.open_order_ids = []
            manager.bid_price = 0.00014
            manager.ask_price = 0.00016
            service._tasks[0].next_run_at = 0.0
            service._tasks[0].order_created_at["order-1"] = time.time() - 2.0
            continued = await service.run_due_tasks(cfg, manager)
            continued_task = continued["tasks"][0]

        self.assertEqual(waiting_task["status"], "waiting_for_start_price")
        self.assertEqual(manager.created, 2)
        self.assertFalse(waiting_task["start_price_triggered"])
        self.assertTrue(triggered_task["start_price_triggered"])
        self.assertEqual(triggered_task["open_order_ids"], ["order-1"])
        self.assertTrue(continued_task["start_price_triggered"])
        self.assertEqual(continued_task["open_order_ids"], ["order-2"])

    async def test_stale_order_cancel_respects_next_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            await service.create_task(
                self._slow_cfg(interval_seconds=10.0, order_ttl_seconds=0.01)
            )
            manager = FakeTaskManager()
            cfg = self._cfg(
                tmp,
                slow_execution=self._slow_cfg(
                    interval_seconds=10.0,
                    order_ttl_seconds=0.01,
                ),
            )

            first = await service.run_due_tasks(cfg, manager)
            self.assertEqual(first["tasks"][0]["open_order_ids"], ["order-1"])
            self.assertEqual(manager.created, 1)

            service._tasks[0].order_created_at["order-1"] = 0.0
            service._tasks[0].last_error = "previous error"
            service._tasks[0].next_run_at = 0.0
            before_cancel = time.time()
            second = await service.run_due_tasks(cfg, manager)
            second_task = second["tasks"][0]

            service._tasks[0].next_run_at = time.time() + 100.0
            third = await service.run_due_tasks(cfg, manager)

        self.assertEqual(manager.canceled, 1)
        self.assertEqual(manager.created, 1)
        self.assertEqual(second_task["status"], "running")
        self.assertEqual(second_task["last_status"], "canceled_stale_orders")
        self.assertIsNone(second_task["last_error"])
        self.assertGreaterEqual(second_task["next_run_at"], before_cancel + 9.0)
        self.assertEqual(third["tasks"][0]["placed_count"], 1)

    async def test_filled_order_respects_next_interval_before_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            await service.create_task(
                self._slow_cfg(interval_seconds=10.0, order_ttl_seconds=2.0)
            )
            manager = FakeTaskManager()
            cfg = self._cfg(
                tmp,
                slow_execution=self._slow_cfg(
                    interval_seconds=10.0,
                    order_ttl_seconds=2.0,
                ),
            )

            first = await service.run_due_tasks(cfg, manager)
            first_order_at = service._tasks[0].order_created_at["order-1"]
            self.assertEqual(first["tasks"][0]["open_order_ids"], ["order-1"])
            self.assertEqual(manager.created, 1)

            manager.open_order_ids = []
            service._tasks[0].next_run_at = 0.0
            second = await service.run_due_tasks(cfg, manager)
            second_task = second["tasks"][0]

            service._tasks[0].order_created_at["order-1"] = time.time() - 20.0
            service._tasks[0].next_run_at = 0.0
            third = await service.run_due_tasks(cfg, manager)

        self.assertEqual(manager.created, 2)
        self.assertEqual(second_task["status"], "waiting_for_interval")
        self.assertEqual(second_task["last_status"], "waiting_for_interval")
        self.assertGreaterEqual(second_task["next_run_at"], first_order_at + 9.0)
        self.assertEqual(third["tasks"][0]["open_order_ids"], ["order-2"])

    async def test_quote_target_progress_completes_from_filled_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            task = await service.create_task(
                self._slow_cfg(total_base=0.0, total_quote=0.001)
            )
            manager = FakeTaskManager()
            cfg = self._cfg(tmp)

            first = await service.run_due_tasks(cfg, manager)
            first_task = first["tasks"][0]
            self.assertEqual(manager.created, 1)
            self.assertEqual(first_task["progress_mode"], "quote")
            self.assertEqual(first_task["filled_quote"], 0.0)

            manager.open_order_ids = []
            manager.trades = [
                {
                    "id": "trade-1",
                    "order": "order-1",
                    "amount": 5.0,
                    "cost": 0.001,
                    "timestamp": 1_000_000,
                }
            ]
            service._tasks[0].next_run_at = 0.0
            second = await service.run_due_tasks(cfg, manager)
            second_task = second["tasks"][0]

        self.assertEqual(task["status"], "running")
        self.assertEqual(manager.created, 1)
        self.assertEqual(second_task["status"], "complete")
        self.assertEqual(second_task["progress_mode"], "quote")
        self.assertAlmostEqual(second_task["filled_quote"], 0.001)
        self.assertAlmostEqual(second_task["remaining_quote"], 0.0)
        self.assertAlmostEqual(second_task["progress_pct"], 100.0)

    async def test_refresh_accumulates_new_trades_beyond_recent_order_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            await service.create_task(
                self._slow_cfg(total_base=0.0, total_quote=20.0)
            )
            manager = FakeTaskManager()
            cfg = self._cfg(
                tmp,
                slow_execution=self._slow_cfg(total_base=0.0, total_quote=20.0),
            )
            service._tasks[0].placed_order_ids = ["old-order", "new-order"]
            service._tasks[0].known_trade_ids = ["old-trade"]
            service._tasks[0].filled_base = 100.0
            service._tasks[0].filled_quote = 10.0
            manager.trades = [
                {
                    "id": "old-trade",
                    "order": "old-order",
                    "amount": 10.0,
                    "cost": 1.0,
                    "timestamp": 1_000_000,
                },
                {
                    "id": "new-trade",
                    "order": "new-order",
                    "amount": 2.0,
                    "price": 1.5,
                    "timestamp": 2_000_000,
                },
            ]

            await service._refresh_task_activity(service._tasks[0], cfg, manager)
            first_refresh = service._tasks[0].to_dict()
            await service._refresh_task_activity(service._tasks[0], cfg, manager)
            second_refresh = service._tasks[0].to_dict()

        self.assertAlmostEqual(first_refresh["filled_base"], 102.0)
        self.assertAlmostEqual(first_refresh["filled_quote"], 13.0)
        self.assertIn("new-trade", first_refresh["known_trade_ids"])
        self.assertAlmostEqual(second_refresh["filled_base"], 102.0)
        self.assertAlmostEqual(second_refresh["filled_quote"], 13.0)

    def test_validate_task_config_requires_one_slice_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "configure exactly one"):
            validate_task_config(self._slow_cfg(slice_base=1.0, slice_base_min=1.0))

    def _slow_cfg(
        self,
        *,
        total_base: float = 10.0,
        total_quote: float = 0.0,
        slice_base: float = 0.0,
        slice_base_min: float = 5.0,
        interval_seconds: float = 1.0,
        order_ttl_seconds: float = 0.0,
        start_price: float = 0.0,
        stop_price: float = 0.0,
        side: str = "buy",
    ) -> SlowExecutionConfig:
        return SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side=side,
            total_base=total_base,
            total_quote=total_quote,
            slice_base=slice_base,
            slice_base_min=slice_base_min,
            slice_base_max=5.0,
            interval_seconds=interval_seconds,
            order_ttl_seconds=order_ttl_seconds,
            start_price=start_price,
            stop_price=stop_price,
            min_order_quote=0.0,
        )

    def _cfg(
        self,
        tmp: str,
        *,
        risk: Optional[RiskConfig] = None,
        slow_execution: Optional[SlowExecutionConfig] = None,
    ) -> BotConfig:
        return BotConfig(
            poll_seconds=1.0,
            order_book_depth=20,
            notional_quote=200.0,
            min_profit_quote=0.1,
            min_profit_bps=1.0,
            min_basis_bps=15.0,
            common_quote_currency="USD",
            quote_rates={"USD": 1.0},
            quote_rate_sources=[],
            onchain_monitor=OnchainMonitorConfig(),
            market_maker=MarketMakerConfig(),
            slow_execution=slow_execution or self._slow_cfg(),
            portfolio=PortfolioConfig(),
            spot_symbols=[],
            spot_markets=[],
            cash_and_carry_pairs=[],
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            derivative_exchanges=[],
            risk=risk or RiskConfig(allow_live_trading=True, require_post_only=False),
            trade_log=TradeLogConfig(enabled=False, path=f"{tmp}/events.jsonl"),
            pnl_store=PnlStoreConfig(enabled=False),
        )


if __name__ == "__main__":
    unittest.main()
