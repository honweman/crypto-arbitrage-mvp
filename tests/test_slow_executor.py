import unittest

from arbitrage_bot.config import (
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.slow_executor import run_cycle


class SlowExecutorLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_cycle_blocked_by_risk_does_not_advance_submitted_base(
        self,
    ) -> None:
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

        payload, submitted_base = await run_cycle(
            self._cfg(),
            FakeManager(),  # type: ignore[arg-type]
            submitted_base=0.0,
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertFalse(payload["risk"]["approved"])
        self.assertEqual(submitted_base, 0.0)

    def _cfg(self) -> BotConfig:
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
            slow_execution=SlowExecutionConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                side="sell",
                total_base=10_000.0,
                slice_base=1_000.0,
            ),
            portfolio=PortfolioConfig(),
            spot_symbols=[],
            spot_markets=[],
            cash_and_carry_pairs=[],
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            derivative_exchanges=[],
            risk=RiskConfig(allow_live_trading=False),
        )


if __name__ == "__main__":
    unittest.main()
