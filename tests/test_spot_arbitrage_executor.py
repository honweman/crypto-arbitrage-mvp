from __future__ import annotations

import unittest

from arbitrage_bot.config import (
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
)
from arbitrage_bot.models import BookLevel, Opportunity, OpportunityLeg, OrderBookSnapshot
from arbitrage_bot.spot_arbitrage_executor import (
    build_spot_arbitrage_orders,
    run_spot_arbitrage_execution_cycle,
)


def make_config(*, risk: RiskConfig | None = None) -> BotConfig:
    return BotConfig(
        poll_seconds=1.0,
        order_book_depth=20,
        notional_quote=100.0,
        min_profit_quote=1.0,
        min_profit_bps=10.0,
        min_basis_bps=0.0,
        common_quote_currency="USD",
        quote_rates={"USD": 1.0, "USDC": 1.0, "USDT": 1.0},
        quote_rate_sources=[],
        onchain_monitor=OnchainMonitorConfig(),
        market_maker=MarketMakerConfig(),
        slow_execution=SlowExecutionConfig(),
        portfolio=PortfolioConfig(),
        spot_symbols=[],
        spot_markets=[
            SpotMarketConfig(
                asset="ACS",
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                quote_currency="USDC",
            ),
            SpotMarketConfig(
                asset="ACS",
                exchange="upbit-spot",
                symbol="ACS/USDT",
                quote_currency="USDT",
            ),
        ],
        cash_and_carry_pairs=[],
        spot_exchanges=[
            ExchangeConfig(id="coinbase", label="coinbase-spot", fee_bps=60.0),
            ExchangeConfig(id="upbit", label="upbit-spot", fee_bps=25.0),
        ],
        derivative_exchanges=[],
        risk=risk or RiskConfig(),
    )


def make_opportunity(quantity: float = 10.0) -> Opportunity:
    return Opportunity(
        strategy="spot-spread",
        profit_quote=1.0,
        profit_bps=100.0,
        legs=[
            OpportunityLeg(
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                side="buy",
                quantity_base=quantity,
                average_price=0.105,
                fee_quote=0.0,
                quote_currency="USDC",
                gross_quote=1.05,
                net_quote=1.05,
                common_quote_rate=1.0,
            ),
            OpportunityLeg(
                exchange="upbit-spot",
                symbol="ACS/USDT",
                side="sell",
                quantity_base=quantity,
                average_price=0.12,
                fee_quote=0.0,
                quote_currency="USDT",
                gross_quote=1.2,
                net_quote=1.2,
                common_quote_rate=1.0,
            ),
        ],
        metadata={"asset": "ACS"},
    )


def make_books() -> dict[tuple[str, str], OrderBookSnapshot]:
    return {
        ("coinbase-spot", "ACS/USDC"): OrderBookSnapshot(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            bids=[BookLevel(price=0.099, amount=100.0)],
            asks=[
                BookLevel(price=0.10, amount=5.0),
                BookLevel(price=0.11, amount=5.0),
            ],
        ),
        ("upbit-spot", "ACS/USDT"): OrderBookSnapshot(
            exchange="upbit-spot",
            symbol="ACS/USDT",
            bids=[
                BookLevel(price=0.121, amount=5.0),
                BookLevel(price=0.119, amount=5.0),
            ],
            asks=[BookLevel(price=0.122, amount=100.0)],
        ),
    }


class SpotArbitrageExecutorTest(unittest.IsolatedAsyncioTestCase):
    def test_plan_uses_last_consumed_order_book_level_as_limit_price(self) -> None:
        orders, errors = build_spot_arbitrage_orders(
            make_config(),
            make_opportunity(),
            books=make_books(),
            quote_rates={"USDC": 1.0, "USDT": 1.0},
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(orders), 2)
        self.assertAlmostEqual(orders[0].price, 0.11)
        self.assertAlmostEqual(orders[1].price, 0.119)

    async def test_live_cycle_blocks_before_placing_when_live_is_disabled(self) -> None:
        class FakeManager:
            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def prepare_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("risk-blocked arbitrage must not validate orders")

            async def create_prepared_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("risk-blocked arbitrage must not place orders")

        payload = await run_spot_arbitrage_execution_cycle(
            make_config(risk=RiskConfig(allow_live_trading=False)),
            FakeManager(),  # type: ignore[arg-type]
            opportunities=[make_opportunity()],
            books=make_books(),
            quote_rates={"USDC": 1.0, "USDT": 1.0},
            live=True,
            order_ttl_seconds=0.0,
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertFalse(payload["risk"]["approved"])
        self.assertNotIn("execution", payload)

    async def test_live_cycle_emergency_cancels_when_one_leg_create_fails(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.open_orders = {
                    ("coinbase-spot", "ACS/USDC"): [{"id": "coinbase-buy-1"}],
                    ("upbit-spot", "ACS/USDT"): [],
                }
                self.canceled: list[tuple[str, str, str]] = []

            async def fetch_open_orders(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
            ) -> list[object]:
                return list(self.open_orders.get((exchange.key, symbol), []))

            async def fetch_balance(self, exchange: ExchangeConfig) -> dict[str, object]:
                if exchange.key == "coinbase-spot":
                    return {"USDC": {"free": 100.0}}
                return {"ACS": {"free": 100.0}}

            async def prepare_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                side: str,
                amount: float,
                price: float,
            ) -> dict[str, object]:
                return {
                    "exchange": exchange.key,
                    "symbol": symbol,
                    "side": side,
                    "status": "ok",
                    "requested_amount": amount,
                    "requested_price": price,
                    "amount": amount,
                    "price": price,
                    "cost": amount * price,
                    "limits": {},
                    "precision": {},
                    "errors": [],
                    "warnings": [],
                }

            async def create_prepared_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                side: str,
                prepared: dict[str, object],
                post_only: bool,
                client_order_id: str,
            ) -> dict[str, str]:
                if exchange.key == "upbit-spot":
                    raise RuntimeError("upbit create failed")
                return {"id": "coinbase-buy-1", "symbol": symbol, "side": side}

            async def cancel_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                order_id: str,
            ) -> dict[str, str]:
                self.canceled.append((exchange.key, symbol, order_id))
                self.open_orders[(exchange.key, symbol)] = [
                    item
                    for item in self.open_orders.get((exchange.key, symbol), [])
                    if item.get("id") != order_id
                ]
                return {"id": order_id, "status": "canceled"}

        manager = FakeManager()
        payload = await run_spot_arbitrage_execution_cycle(
            make_config(
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_order_quote=10.0,
                    max_cycle_quote=30.0,
                    max_open_orders=10,
                    max_slippage_bps=2_000.0,
                    require_post_only=False,
                )
            ),
            manager,  # type: ignore[arg-type]
            opportunities=[make_opportunity()],
            books=make_books(),
            quote_rates={"USDC": 1.0, "USDT": 1.0},
            live=True,
            order_ttl_seconds=0.0,
        )

        self.assertEqual(payload["status"], "execution_error")
        execution = payload["execution"]
        self.assertEqual(execution["placed_order_ids"], ["coinbase-buy-1"])
        self.assertEqual(len(execution["create_errors"]), 1)
        self.assertTrue(execution["emergency_cancel"])
        self.assertFalse(execution["manual_intervention_required"])
        self.assertEqual(execution["cancel_reason"], "create_error")
        self.assertEqual(execution["canceled_order_ids"], ["coinbase-buy-1"])
        self.assertIn("create_latency_ms", execution)
        self.assertIn("opportunity_to_submit_ms", execution)
        self.assertIn("fill_status", execution)
        self.assertIn("paper_execution", payload)
        self.assertIn("paper_vs_live", payload)
        self.assertEqual(
            manager.canceled,
            [("coinbase-spot", "ACS/USDC", "coinbase-buy-1")],
        )

    async def test_live_cycle_marks_hedge_required_when_fills_are_imbalanced(self) -> None:
        class FakeManager:
            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_balance(self, exchange: ExchangeConfig) -> dict[str, object]:
                if exchange.key == "coinbase-spot":
                    return {"USDC": {"free": 100.0}}
                return {"ACS": {"free": 100.0}}

            async def prepare_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                side: str,
                amount: float,
                price: float,
            ) -> dict[str, object]:
                return {
                    "exchange": exchange.key,
                    "symbol": symbol,
                    "side": side,
                    "amount": amount,
                    "price": price,
                    "cost": amount * price,
                    "errors": [],
                    "warnings": [],
                }

            async def create_prepared_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                side: str,
                prepared: dict[str, object],
                post_only: bool,
                client_order_id: str,
            ) -> dict[str, object]:
                if side == "buy":
                    return {
                        "id": "buy-filled",
                        "symbol": symbol,
                        "side": side,
                        "filled": 10.0,
                        "cost": 1.1,
                    }
                return {
                    "id": "sell-open",
                    "symbol": symbol,
                    "side": side,
                    "filled": 0.0,
                    "cost": 0.0,
                }

        payload = await run_spot_arbitrage_execution_cycle(
            make_config(
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_order_quote=10.0,
                    max_cycle_quote=30.0,
                    max_open_orders=10,
                    max_slippage_bps=2_000.0,
                    require_post_only=False,
                )
            ),
            FakeManager(),  # type: ignore[arg-type]
            opportunities=[make_opportunity()],
            books=make_books(),
            quote_rates={"USDC": 1.0, "USDT": 1.0},
            live=True,
            order_ttl_seconds=0.0,
        )

        self.assertEqual(payload["status"], "hedge_required")
        fill_status = payload["execution"]["fill_status"]
        self.assertTrue(fill_status["hedge_required"])
        self.assertEqual(fill_status["hedge_side"], "sell")
        self.assertAlmostEqual(fill_status["hedge_base"], 10.0)
        self.assertTrue(payload["execution"]["manual_intervention_required"])
        self.assertTrue(payload["paper_vs_live"]["hedge_required"])
