from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_fresh_reservation_is_in_flight_until_its_outcome_is_uncertain(
        self,
    ) -> None:
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

            store.reserve("in-flight", intent)
            in_flight = store.summary()
            reserved = store.get("in-flight")
            assert reserved is not None
            with patch(
                "arbitrage_bot.order_reliability.time.time",
                return_value=float(reserved["updated_at"]) + 31.0,
            ):
                stale = store.summary()
            store.mark_unknown("in-flight", "network timeout")
            uncertain = store.summary()

            self.assertEqual(in_flight["pending_count"], 0)
            self.assertEqual(in_flight["in_flight_count"], 1)
            self.assertEqual(stale["pending_count"], 1)
            self.assertEqual(stale["stale_reserved_count"], 1)
            self.assertEqual(uncertain["pending_count"], 1)
            self.assertEqual(uncertain["in_flight_count"], 0)

    def test_uncertain_intent_quarantines_only_its_market(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderIntentStore(Path(tmp) / "orders.sqlite3")
            intent = {
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "side": "buy",
                "amount": 10.0,
                "price": 0.1,
            }
            store.reserve("uncertain", intent)
            store.mark_unknown("uncertain", "gateway timeout")

            blocked = store.first_uncertain(
                exchange="coinbase-spot",
                symbol="ACS/USDC",
            )
            other_market = store.first_uncertain(
                exchange="upbit-spot",
                symbol="ACS/USDT",
            )
            summary = store.summary()

            self.assertEqual(blocked["client_order_id"], "uncertain")
            self.assertIsNone(other_market)
            self.assertEqual(
                summary["quarantined_resources"],
                [{"exchange": "coinbase-spot", "symbol": "ACS/USDC", "count": 1}],
            )

    def test_confirmed_post_only_rejection_leaves_uncertain_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OrderIntentStore(Path(tmp) / "orders.sqlite3")
            store.reserve(
                "rejected",
                {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "side": "sell",
                    "amount": 10.0,
                    "price": 0.1,
                },
            )
            store.mark_unknown(
                "rejected",
                "ExchangeError: PREVIEW_INVALID_LIMIT_PRICE_POST_ONLY",
            )

            rows = store.reclassify_confirmed_rejections(
                exchange="coinbase-spot",
                symbol="ACS/USDC",
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(store.summary()["pending_count"], 0)


class IdempotentExchangeSubmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_coinbase_post_only_rejection_is_terminal(self) -> None:
        class ExchangeError(Exception):
            pass

        class FakeClient:
            async def create_order(self, *args: object) -> dict[str, object]:
                raise ExchangeError(
                    'coinbase {"success":false,"error_response":'
                    '{"error":"INVALID_LIMIT_PRICE_POST_ONLY"}}'
                )

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
            journal_path = Path(tmp) / "orders.sqlite3"
            manager = ExchangeManager(order_journal_path=str(journal_path))
            manager._clients[cfg.key] = FakeClient()

            with self.assertRaises(ExchangeError):
                await manager.create_prepared_limit_order(
                    cfg,
                    symbol="ACS/USDC",
                    side="sell",
                    prepared={"amount": 10.0, "price": 0.1, "errors": []},
                    post_only=True,
                    client_order_id="rejected-post-only",
                )

            intent = OrderIntentStore(journal_path).get("rejected-post-only")
            assert intent is not None
            self.assertEqual(intent["status"], "failed")
            self.assertEqual(manager.order_reliability_summary()["pending_count"], 0)

    async def test_scoped_recovery_reclassifies_confirmed_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
            journal_path = Path(tmp) / "orders.sqlite3"
            manager = ExchangeManager(order_journal_path=str(journal_path))
            assert manager._order_intents is not None
            manager._order_intents.reserve(
                "legacy-rejection",
                {
                    "exchange": cfg.key,
                    "symbol": "ACS/USDC",
                    "side": "sell",
                    "amount": 10.0,
                    "price": 0.1,
                },
            )
            manager._order_intents.mark_unknown(
                "legacy-rejection",
                "ExchangeError: INVALID_LIMIT_PRICE_POST_ONLY",
            )

            result = await manager.recover_pending_order_intents(
                [cfg],
                exchange=cfg.key,
                symbol="ACS/USDC",
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["reclassified_count"], 1)
            self.assertEqual(result["unresolved_count"], 0)

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

    async def test_uncertain_intent_blocks_only_new_orders_on_same_market(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.created: list[str] = []

            async def create_order(self, *args: object) -> dict[str, object]:
                params = dict(args[-1])  # type: ignore[arg-type]
                self.created.append(str(params.get("clientOrderId") or ""))
                return {"id": f"order-{len(self.created)}", "status": "open"}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
            manager = ExchangeManager(
                order_journal_path=str(Path(tmp) / "orders.sqlite3")
            )
            client = FakeClient()
            manager._clients[cfg.key] = client
            assert manager._order_intents is not None
            manager._order_intents.reserve(
                "uncertain-1",
                {
                    "exchange": cfg.key,
                    "symbol": "ACS/USDC",
                    "side": "buy",
                    "amount": 10.0,
                    "price": 0.1,
                },
            )
            manager._order_intents.mark_unknown("uncertain-1", "gateway timeout")

            with self.assertRaisesRegex(RuntimeError, "submission quarantined"):
                await manager.create_prepared_limit_order(
                    cfg,
                    symbol="ACS/USDC",
                    side="sell",
                    prepared={"amount": 9.0, "price": 0.2, "errors": []},
                    post_only=False,
                    client_order_id="new-order",
                )

            allowed = await manager.create_prepared_limit_order(
                cfg,
                symbol="BTC/USDC",
                side="buy",
                prepared={"amount": 0.01, "price": 100.0, "errors": []},
                post_only=False,
                client_order_id="other-market",
            )

            self.assertEqual(allowed["id"], "order-1")
            self.assertEqual(client.created, ["other-market"])

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
