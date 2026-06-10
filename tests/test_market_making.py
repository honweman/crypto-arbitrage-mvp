import unittest
from contextlib import redirect_stdout
from io import StringIO
from typing import Optional

from arbitrage_bot.config import BotConfig, ExchangeConfig, MarketMakerConfig, RiskConfig
from arbitrage_bot.config import TradeLogConfig
from arbitrage_bot.market_maker import run_cycle, run_loop
from arbitrage_bot.market_making import build_symmetric_market_maker_plan
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


class MarketMakingTest(unittest.TestCase):
    def test_builds_symmetric_quote_depth_around_mid(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=90.0, amount=10.0)],
            asks=[BookLevel(price=110.0, amount=10.0)],
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=2,
            price_band_pct=10.0,
            quote_per_level=100.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)

        self.assertEqual(plan.mid_price, 100.0)
        self.assertEqual(len(plan.orders), 4)
        self.assertEqual(plan.orders[0].side, "buy")
        self.assertAlmostEqual(plan.orders[0].price, 95.0)
        self.assertAlmostEqual(plan.orders[0].amount, 100.0 / 95.0)
        self.assertEqual(plan.orders[1].side, "sell")
        self.assertAlmostEqual(plan.orders[1].price, 105.0)
        self.assertAlmostEqual(plan.orders[2].price, 90.0)
        self.assertAlmostEqual(plan.orders[3].price, 110.0)

    def test_min_distance_filters_inner_levels(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=99.0, amount=10.0)],
            asks=[BookLevel(price=101.0, amount=10.0)],
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=10,
            price_band_pct=10.0,
            quote_per_level=10.0,
            min_distance_bps=500.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)

        self.assertEqual(len(plan.orders), 12)
        self.assertEqual(plan.orders[0].level, 5)
        self.assertAlmostEqual(plan.orders[0].distance_bps, 500.0)


class MarketMakerLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_cycle_is_blocked_when_risk_disallows_live(self) -> None:
        class FakeManager:
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

            async def create_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("risk-blocked live cycle must not place orders")

        payload = await run_cycle(
            self._cfg(
                risk=RiskConfig(
                    allow_live_trading=False,
                    max_cycle_quote=100.0,
                )
            ),
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertFalse(payload["risk"]["approved"])
        self.assertNotIn("execution", payload)

    async def test_run_loop_clamps_interval_to_one_second(self) -> None:
        class StopLoop(Exception):
            pass

        sleep_calls = []
        original_sleep = __import__("asyncio").sleep

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise StopLoop

        class FakeManager:
            async def close(self) -> None:
                return None

        import arbitrage_bot.market_maker as market_maker_module

        original_manager = market_maker_module.ExchangeManager
        original_run_cycle = market_maker_module.run_cycle
        market_maker_module.ExchangeManager = lambda: FakeManager()
        market_maker_module.run_cycle = self._fake_run_cycle
        __import__("asyncio").sleep = fake_sleep
        try:
            with redirect_stdout(StringIO()):
                with self.assertRaises(StopLoop):
                    await run_loop(
                        self._cfg(),
                        live=False,
                        loop=True,
                        poll_seconds=0.1,
                        replace_existing=False,
                    )
        finally:
            __import__("asyncio").sleep = original_sleep
            market_maker_module.ExchangeManager = original_manager
            market_maker_module.run_cycle = original_run_cycle

        self.assertEqual(len(sleep_calls), 1)
        self.assertGreaterEqual(sleep_calls[0], 0.9)

    async def _fake_run_cycle(self, *_: object, **__: object) -> dict[str, object]:
        return {"type": "market_maker", "status": "planned"}

    def _cfg(self, *, risk: Optional[RiskConfig] = None) -> BotConfig:
        from arbitrage_bot.config import (
            OnchainMonitorConfig,
            PortfolioConfig,
            SlowExecutionConfig,
        )

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
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                poll_seconds=0.1,
                quote_per_level=1.0,
            ),
            slow_execution=SlowExecutionConfig(),
            portfolio=PortfolioConfig(),
            spot_symbols=[],
            spot_markets=[],
            cash_and_carry_pairs=[],
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            derivative_exchanges=[],
            risk=risk or RiskConfig(),
            trade_log=TradeLogConfig(enabled=False),
        )


if __name__ == "__main__":
    unittest.main()
