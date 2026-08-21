from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from arbitrage_bot.web import APP_JS, STYLES_CSS
from arbitrage_bot.web.market_tickers import (
    MarketTickerService,
    MarketWatchlistStore,
    normalize_watchlist,
)


class MarketTickerUiTest(unittest.TestCase):
    def test_editor_supports_persistent_reordering(self) -> None:
        self.assertIn("function moveMarketTickerDraftItem(index, offset)", APP_JS)
        self.assertIn('moveUp.setAttribute("aria-label", uiText("Move up"))', APP_JS)
        self.assertIn(
            'moveDown.setAttribute("aria-label", uiText("Move down"))',
            APP_JS,
        )
        self.assertIn('body: JSON.stringify({ items: marketTickerDraft })', APP_JS)
        self.assertIn(".market-ticker-draft-actions", STYLES_CSS)


class MarketWatchlistStoreTest(unittest.TestCase):
    def test_normalize_watchlist_adds_perpetual_settlement_currency(self) -> None:
        rows = normalize_watchlist(
            [{"exchange": "ticker:binance:swap", "symbol": "btc/usdt"}]
        )

        self.assertEqual(rows[0]["symbol"], "BTC/USDT:USDT")

    def test_watchlists_are_persisted_per_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketWatchlistStore(Path(tmp) / "watchlists.json")
            store.set(
                "alice@example.com",
                [{"exchange": "ticker:binance:spot", "symbol": "ACS/USDT"}],
            )

            alice = MarketWatchlistStore(store.path).get("alice@example.com")
            bob = MarketWatchlistStore(store.path).get("bob@example.com")

        self.assertEqual(alice[0]["symbol"], "ACS/USDT")
        self.assertGreater(len(bob), 1)
        self.assertNotEqual(alice, bob)

    def test_watchlist_order_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketWatchlistStore(Path(tmp) / "watchlists.json")
            expected = ["SOL/USDT", "BTC/USDT", "ETH/USDT"]
            store.set(
                "trader@example.com",
                [
                    {"exchange": "ticker:binance:spot", "symbol": symbol}
                    for symbol in expected
                ],
            )

            persisted = MarketWatchlistStore(store.path).get("trader@example.com")

        self.assertEqual([row["symbol"] for row in persisted], expected)


class MarketTickerServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_batches_symbols_by_market_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketWatchlistStore(Path(tmp) / "watchlists.json")
            store.set(
                "trader@example.com",
                [
                    {"exchange": "ticker:binance:spot", "symbol": "BTC/USDT"},
                    {"exchange": "ticker:binance:spot", "symbol": "ETH/USDT"},
                    {
                        "exchange": "ticker:binance:swap",
                        "symbol": "BTC/USDT:USDT",
                    },
                ],
            )
            manager = AsyncMock()

            async def fetch_tickers(exchange, symbols):
                return {
                    symbol: {
                        "symbol": symbol,
                        "last": 100.0 if symbol.startswith("BTC") else 50.0,
                        "percentage": 2.5 if exchange.market_type == "spot" else -1.25,
                    }
                    for symbol in symbols
                }

            manager.fetch_tickers.side_effect = fetch_tickers
            service = MarketTickerService(store, manager=manager)

            payload = await service.snapshot("trader@example.com")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual(manager.fetch_tickers.await_count, 2)
        self.assertEqual(payload["items"][0]["change_24h_pct"], 2.5)
        self.assertEqual(payload["items"][2]["change_24h_pct"], -1.25)

    async def test_snapshot_calculates_change_from_open_when_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketWatchlistStore(Path(tmp) / "watchlists.json")
            store.set(
                "trader@example.com",
                [{"exchange": "ticker:bybit:spot", "symbol": "SOL/USDT"}],
            )
            manager = AsyncMock()
            manager.fetch_tickers.return_value = {
                "SOL/USDT": {"symbol": "SOL/USDT", "last": 110.0, "open": 100.0}
            }
            service = MarketTickerService(store, manager=manager)

            payload = await service.snapshot("trader@example.com")

        self.assertAlmostEqual(payload["items"][0]["change_24h_pct"], 10.0)
