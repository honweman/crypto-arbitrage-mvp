import asyncio
import time
import unittest

from arbitrage_bot.config import ExchangeConfig
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.orderbook_cache import OrderBookCache


class OrderBookCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_watch_cache_stores_fresh_websocket_snapshot(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.calls = 0

            def watch_order_book_supported(self, _: ExchangeConfig) -> bool:
                return True

            async def watch_order_book(
                self,
                cfg: ExchangeConfig,
                symbol: str,
                depth: int,
            ) -> OrderBookSnapshot:
                self.calls += 1
                await asyncio.sleep(0.01)
                return OrderBookSnapshot(
                    exchange=cfg.key,
                    symbol=symbol,
                    bids=[BookLevel(price=99.0, amount=depth)],
                    asks=[BookLevel(price=101.0, amount=depth)],
                    source="websocket",
                    received_at=time.time(),
                )

        cfg = ExchangeConfig(id="bybit", label="bybit-spot")
        manager = FakeManager()
        cache = OrderBookCache(manager)
        try:
            self.assertTrue(await cache.ensure_watch(cfg, "ACS/USDT", 5))
            snapshot = None
            for _ in range(20):
                snapshot = cache.get("bybit-spot", "ACS/USDT")
                if snapshot is not None:
                    break
                await asyncio.sleep(0.02)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source, "websocket")
            self.assertGreater(manager.calls, 0)
            status = cache.status("bybit-spot", "ACS/USDT")
            self.assertTrue(status["fresh"])
            self.assertTrue(status["watch_running"])
            self.assertTrue(status["websocket_supported"])
        finally:
            await cache.close()

    async def test_unsupported_watch_reports_status_without_task(self) -> None:
        class FakeManager:
            def watch_order_book_supported(self, _: ExchangeConfig) -> bool:
                return False

        cfg = ExchangeConfig(id="coinbase", label="coinbase-spot")
        cache = OrderBookCache(FakeManager())

        self.assertFalse(await cache.ensure_watch(cfg, "ACS/USDC", 5))
        status = cache.status("coinbase-spot", "ACS/USDC")

        self.assertFalse(status["websocket_supported"])
        self.assertFalse(status["watch_running"])
        self.assertIn("not supported", status["error"])

    async def test_get_returns_none_for_stale_snapshot(self) -> None:
        cache = OrderBookCache(object(), max_age_seconds=0.05)
        cache.update(
            OrderBookSnapshot(
                exchange="upbit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=99.0, amount=1.0)],
                asks=[BookLevel(price=101.0, amount=1.0)],
                source="websocket",
                received_at=time.time() - 1.0,
            )
        )

        self.assertIsNone(cache.get("upbit-spot", "ACS/USDT"))
        self.assertFalse(cache.status("upbit-spot", "ACS/USDT")["fresh"])


if __name__ == "__main__":
    unittest.main()
