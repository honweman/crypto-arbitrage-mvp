from __future__ import annotations

import unittest

from arbitrage_bot.config import (
    BotConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    SpotMarketConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.pnl import build_portfolio_pnl
from arbitrage_bot.web import MonitorState, build_market_maker_payload, build_market_rows


def make_config(
    *,
    market_maker: MarketMakerConfig | None = None,
    portfolio: PortfolioConfig | None = None,
    spot_markets: list[SpotMarketConfig] | None = None,
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
        market_maker=market_maker or MarketMakerConfig(),
        portfolio=portfolio or PortfolioConfig(),
        spot_symbols=[],
        spot_markets=spot_markets or [],
        cash_and_carry_pairs=[],
        spot_exchanges=[],
        derivative_exchanges=[],
    )


class WebMonitorTest(unittest.TestCase):
    def test_build_market_rows_converts_top_of_book(self) -> None:
        markets = [
            SpotMarketConfig(
                asset="ACS",
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                quote_currency="KRW",
            )
        ]
        books = {
            ("bithumb-spot", "ACS/KRW"): OrderBookSnapshot(
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                bids=[BookLevel(price=0.20, amount=100_000)],
                asks=[BookLevel(price=0.21, amount=90_000)],
            )
        }

        rows = build_market_rows(markets, books, {"KRW": 0.00075})

        self.assertEqual(rows[0]["status"], "ok")
        self.assertAlmostEqual(rows[0]["bid_common"], 0.00015)
        self.assertAlmostEqual(rows[0]["ask_common"], 0.0001575)

    def test_build_market_maker_payload_returns_plan(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=10,
                price_band_pct=10.0,
                quote_per_level=1.0,
            )
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }

        payload = build_market_maker_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(len(payload["plan"]["orders"]), 20)

    def test_build_portfolio_pnl_splits_sources(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                asset="ACS",
                position_base=10_000.0,
                average_entry_price=0.00010,
                realized_pnl={"market_maker": 1.25, "arbitrage": 2.50},
            ),
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }

        payload = build_portfolio_pnl(cfg, books, {"USDT": 1.0})

        self.assertEqual(payload["status"], "ok")
        self.assertAlmostEqual(payload["mark_price"], 0.00015)
        self.assertAlmostEqual(payload["sources"]["price_move"], 0.5)
        self.assertAlmostEqual(payload["sources"]["market_maker"], 1.25)
        self.assertAlmostEqual(payload["sources"]["arbitrage"], 2.5)
        self.assertAlmostEqual(payload["total_pnl"], 4.25)


class WebMonitorStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_program_switch_updates_running_state(self) -> None:
        state = MonitorState(make_config(), 1.0)

        paused = await state.set_running(False)
        self.assertFalse(await state.is_running())
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(paused["program"]["running"])

        resumed = await state.set_running(True)
        self.assertTrue(await state.is_running())
        self.assertEqual(resumed["status"], "starting")
        self.assertTrue(resumed["program"]["running"])

if __name__ == "__main__":
    unittest.main()
