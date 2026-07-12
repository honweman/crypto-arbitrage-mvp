from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.config import ExchangeConfig
from arbitrage_bot.exchanges import ExchangeManager, normalize_client_order_id
from arbitrage_bot.order_reliability import OrderIntentStore


class OrderIntentStoreTest(unittest.TestCase):
    def test_client_order_ids_are_deterministic_and_exchange_safe(self) -> None:
        long_id = "crypto-arb-mm-coinbase-spot-acs-usdc-1781065199786-20"

        normalized = normalize_client_order_id(long_id)

        self.assertEqual(len(normalized), 36)
        self.assertEqual(normalized, normalize_client_order_id(long_id))
        self.assertNotEqual(normalized, normalize_client_order_id(f"{long_id}-other"))
        self.assertEqual(normalize_client_order_id("short-id"), "short-id")

    def test_reservation_replays_submitted_order_and_rejects_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderIntentStore(Path(tmp) / "orders.sqlite3")
            intent = {
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "side": "buy",
                "amount": 10.0,
                "price": 0.1,
                "post_only": False,
            }
            first = store.reserve("intent-1", intent)
            store.mark_submitted("intent-1", {"id": "order-1", "status": "open"})
            replay = store.reserve("intent-1", intent)

            self.assertEqual(first["action"], "submit")
            self.assertEqual(replay["action"], "return_existing")
            self.assertEqual(replay["response"]["id"], "order-1")
            with self.assertRaisesRegex(ValueError, "collision"):
                store.reserve("intent-1", {**intent, "amount": 11.0})

    def test_compaction_never_removes_uncertain_intents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderIntentStore(Path(tmp) / "orders.sqlite3")
            intent = {
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "side": "buy",
                "amount": 10.0,
                "price": 0.1,
                "post_only": False,
            }
            store.reserve("submitted", intent)
            store.mark_submitted("submitted", {"id": "order-1"})
            store.reserve("uncertain", {**intent, "amount": 11.0})
            store.mark_unknown("uncertain", "network timeout")

            compacted = store.compact(terminal_retention_seconds=0)

            self.assertEqual(compacted["expired_removed"], 1)
            self.assertIsNone(store.get("submitted"))
            self.assertEqual(store.get("uncertain")["status"], "unknown")


class IdempotentExchangeSubmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_client_id_submits_once_and_replays_response(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.create_count = 0

            async def create_order(self, *args: object) -> dict[str, object]:
                self.create_count += 1
                return {"id": "exchange-order-1", "status": "open"}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
            manager = ExchangeManager(
                order_journal_path=str(Path(tmp) / "orders.sqlite3")
            )
            client = FakeClient()
            manager._clients[cfg.key] = client
            prepared = {"amount": 10.0, "price": 0.1, "errors": []}

            first = await manager.create_prepared_limit_order(
                cfg,
                symbol="ACS/USDC",
                side="buy",
                prepared=prepared,
                post_only=False,
                client_order_id="intent-1",
            )
            replay = await manager.create_prepared_limit_order(
                cfg,
                symbol="ACS/USDC",
                side="buy",
                prepared=prepared,
                post_only=False,
                client_order_id="intent-1",
            )

            self.assertEqual(client.create_count, 1)
            self.assertEqual(first["id"], "exchange-order-1")
            self.assertEqual(replay["id"], "exchange-order-1")
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(manager.order_reliability_summary()["pending_count"], 0)

    async def test_long_client_id_uses_same_normalized_value_for_exchange_and_journal(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.params: dict[str, object] = {}

            async def create_order(self, *args: object) -> dict[str, object]:
                self.params = dict(args[-1])  # type: ignore[arg-type]
                return {"id": "exchange-order-1", "status": "open"}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
            journal_path = Path(tmp) / "orders.sqlite3"
            manager = ExchangeManager(order_journal_path=str(journal_path))
            client = FakeClient()
            manager._clients[cfg.key] = client
            long_id = "crypto-arb-mm-coinbase-spot-acs-usdc-1781065199786-20"

            await manager.create_prepared_limit_order(
                cfg,
                symbol="ACS/USDC",
                side="buy",
                prepared={"amount": 10.0, "price": 0.1, "errors": []},
                post_only=False,
                client_order_id=long_id,
            )

            normalized = normalize_client_order_id(long_id)
            self.assertEqual(client.params["clientOrderId"], normalized)
            self.assertIsNotNone(OrderIntentStore(journal_path).get(normalized))

    async def test_missing_exchange_order_id_is_kept_as_uncertain(self) -> None:
        class FakeClient:
            async def create_order(self, *args: object) -> dict[str, object]:
                return {"status": "open"}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
            manager = ExchangeManager(
                order_journal_path=str(Path(tmp) / "orders.sqlite3")
            )
            manager._clients[cfg.key] = FakeClient()

            with self.assertRaisesRegex(RuntimeError, "returned no order id"):
                await manager.create_prepared_limit_order(
                    cfg,
                    symbol="ACS/USDC",
                    side="buy",
                    prepared={"amount": 10.0, "price": 0.1, "errors": []},
                    post_only=False,
                    client_order_id="intent-without-id",
                )

            summary = manager.order_reliability_summary()
            self.assertEqual(summary["pending_count"], 1)
            self.assertEqual(summary["counts"]["unknown"], 1)

    async def test_cancel_retries_until_open_order_absence_is_confirmed(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.cancel_count = 0
                self.open_check_count = 0

            async def cancel_order(self, *_: object) -> dict[str, object]:
                self.cancel_count += 1
                return {"id": "order-1", "status": "canceled"}

            async def fetch_open_orders(self, *_: object) -> list[dict[str, str]]:
                self.open_check_count += 1
                if self.open_check_count <= 2:
                    return [{"id": "order-1"}]
                return []

        cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
        manager = ExchangeManager(order_journal_path="")
        client = FakeClient()
        manager._clients[cfg.key] = client

        result = await manager.cancel_order(
            cfg,
            symbol="ACS/USDC",
            order_id="order-1",
        )

        self.assertEqual(client.cancel_count, 2)
        self.assertTrue(result["cancel_confirmed_absent"])
