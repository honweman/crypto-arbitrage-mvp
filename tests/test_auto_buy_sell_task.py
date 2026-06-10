from __future__ import annotations

import tempfile
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
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
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
            service._tasks[0].next_run_at = 0.0
            second = await service.run_due_tasks(cfg, manager)
            second_task = second["tasks"][0]

            manager.open_order_ids = []
            manager.trades = []
            service._tasks[0].next_run_at = 0.0
            third = await service.run_due_tasks(cfg, manager)
            third_task = third["tasks"][0]

        self.assertEqual(task["status"], "running")
        self.assertEqual(manager.created, 3)
        self.assertEqual(second_task["filled_base"], 2.0)
        self.assertAlmostEqual(second_task["last_plan"]["submitted_base"], 2.0)
        self.assertEqual(second_task["open_order_ids"], ["order-2"])
        self.assertEqual(third_task["filled_base"], 2.0)

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
    ) -> SlowExecutionConfig:
        return SlowExecutionConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="buy",
            total_base=total_base,
            total_quote=total_quote,
            slice_base=slice_base,
            slice_base_min=slice_base_min,
            slice_base_max=5.0,
            interval_seconds=1.0,
            order_ttl_seconds=0.0,
            min_order_quote=0.0,
        )

    def _cfg(self, tmp: str, *, risk: Optional[RiskConfig] = None) -> BotConfig:
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
            slow_execution=self._slow_cfg(),
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
