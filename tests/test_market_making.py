import unittest
from contextlib import redirect_stdout
from io import StringIO
from typing import Optional

from arbitrage_bot.config import BotConfig, ExchangeConfig, MarketMakerConfig, RiskConfig
from arbitrage_bot.config import TradeLogConfig
from arbitrage_bot.market_maker import market_maker_quote_conversion, run_cycle, run_loop
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
            depth_shape="flat",
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
        self.assertAlmostEqual(plan.bid_depth_quote, 900.0)
        self.assertAlmostEqual(plan.ask_depth_quote, 1100.0)
        self.assertEqual(plan.max_level_gap_bps, 0.0)

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
            depth_shape="flat",
            min_distance_bps=500.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)

        self.assertEqual(len(plan.orders), 12)
        self.assertEqual(plan.orders[0].level, 5)
        self.assertAlmostEqual(plan.orders[0].distance_bps, 500.0)

    def test_linear_depth_is_shallower_near_top_of_book(self) -> None:
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
            levels=3,
            price_band_pct=3.0,
            quote_per_level=60.0,
            depth_shape="linear",
        )

        plan = build_symmetric_market_maker_plan(book, cfg)
        buy_quotes = [
            order.quote_notional for order in plan.orders if order.side == "buy"
        ]
        sell_quotes = [
            order.quote_notional for order in plan.orders if order.side == "sell"
        ]

        self.assertEqual(buy_quotes, [30.0, 60.0, 90.0])
        self.assertEqual(sell_quotes, [30.0, 60.0, 90.0])
        self.assertAlmostEqual(sum(buy_quotes), 60.0 * 3)

    def test_linear_depth_respects_min_order_quote_when_possible(self) -> None:
        book = OrderBookSnapshot(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            levels=2,
            price_band_pct=1.0,
            quote_per_level=1.2,
            depth_shape="linear",
            min_order_quote=1.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)
        buy_quotes = [
            order.quote_notional for order in plan.orders if order.side == "buy"
        ]

        self.assertAlmostEqual(buy_quotes[0], 1.0 + 0.4 / 3)
        self.assertAlmostEqual(buy_quotes[1], 1.0 + 0.8 / 3)
        self.assertTrue(buy_quotes[0] < buy_quotes[1])


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

    async def test_live_cycle_blocks_invalid_exchange_limits_before_placing(
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

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def prepare_limit_order(
                self,
                *_: object,
                **__: object,
            ) -> dict[str, object]:
                return {
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
                    "side": "buy",
                    "status": "error",
                    "requested_amount": 1.0,
                    "requested_price": 0.00015,
                    "amount": 1.0,
                    "price": 0.00015,
                    "cost": 0.00015,
                    "limits": {},
                    "precision": {},
                    "errors": ["cost 0.00015 is below exchange minimum 1"],
                    "warnings": [],
                }

            async def create_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("invalid live cycle must not place orders")

        payload = await run_cycle(
            self._cfg(
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_cycle_quote=100.0,
                )
            ),
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertEqual(payload["order_validation"]["status"], "error")
        self.assertFalse(payload["risk"]["approved"])
        self.assertTrue(
            any("order validation" in reason for reason in payload["risk"]["reasons"])
        )
        self.assertNotIn("execution", payload)

    async def test_live_cycle_replaces_only_tracked_order_ids(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.canceled: list[str] = []
                self.created = 0

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

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return [{"id": "old-mm-1"}, {"id": "manual-1"}]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, object]:
                self.canceled.append(order_id)
                return {"id": order_id, "status": "canceled"}

            async def prepare_limit_order(
                self,
                *_: object,
                amount: float,
                price: float,
                side: str,
                **__: object,
            ) -> dict[str, object]:
                return {
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
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

            async def create_limit_order(self, *_: object, **__: object) -> dict[str, str]:
                self.created += 1
                return {"id": f"new-mm-{self.created}"}

        manager = FakeManager()
        payload = await run_cycle(
            self._cfg(
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_cycle_quote=100.0,
                    max_open_orders=50,
                    max_cancels_per_cycle=10,
                    require_post_only=False,
                )
            ),
            manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=["old-mm-1"],
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(manager.canceled, ["old-mm-1"])
        self.assertEqual(payload["execution"]["canceled_count"], 1)
        self.assertEqual(payload["execution"]["placed_count"], 20)
        self.assertEqual(len(payload["execution"]["placed_order_ids"]), 20)

    async def test_live_cycle_reuses_batch_prepared_orders_for_placement(
        self,
    ) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.batch_prepare_count = 0
                self.single_prepare_count = 0
                self.prepared_create_count = 0

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

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def prepare_limit_orders(
                self,
                *_: object,
                orders: list[dict[str, object]],
                **__: object,
            ) -> list[dict[str, object]]:
                self.batch_prepare_count += 1
                return [
                    {
                        "exchange": "bybit-spot",
                        "symbol": "ACS/USDT",
                        "side": order["side"],
                        "status": "ok",
                        "requested_amount": order["amount"],
                        "requested_price": order["price"],
                        "amount": order["amount"],
                        "price": order["price"],
                        "cost": float(order["amount"]) * float(order["price"]),
                        "limits": {},
                        "precision": {},
                        "errors": [],
                        "warnings": [],
                    }
                    for order in orders
                ]

            async def prepare_limit_order(self, *_: object, **__: object) -> None:
                self.single_prepare_count += 1
                raise AssertionError("live MM should use batch-prepared orders")

            async def create_prepared_limit_order(
                self,
                *_: object,
                prepared: dict[str, object],
                **__: object,
            ) -> dict[str, str]:
                self.prepared_create_count += 1
                return {
                    "id": f"new-mm-{self.prepared_create_count}",
                    "price": str(prepared["price"]),
                }

        manager = FakeManager()
        payload = await run_cycle(
            self._cfg(
                market_maker=MarketMakerConfig(
                    enabled=True,
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    levels=2,
                    quote_per_level=1.0,
                    depth_shape="flat",
                ),
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_cycle_quote=100.0,
                    max_open_orders=50,
                    require_post_only=False,
                ),
            ),
            manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(manager.batch_prepare_count, 1)
        self.assertEqual(manager.single_prepare_count, 0)
        self.assertEqual(manager.prepared_create_count, 4)

    async def test_live_cycle_skips_reprice_when_plan_change_is_small(self) -> None:
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

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return [{"id": "old-mm-1"}]

            async def prepare_limit_orders(self, *_: object, **__: object) -> None:
                raise AssertionError("unchanged plan should not validate orders")

            async def create_prepared_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("unchanged plan should not place orders")

        cfg = self._cfg(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=2,
                quote_per_level=1.0,
                depth_shape="flat",
                reprice_threshold_bps=2.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                max_cycle_quote=100.0,
                max_open_orders=50,
                require_post_only=False,
            ),
        )
        previous_plan = build_symmetric_market_maker_plan(
            OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            ),
            cfg.market_maker,
        ).to_dict()

        payload = await run_cycle(
            cfg,
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=["old-mm-1"],
            previous_plan=previous_plan,
        )

        self.assertEqual(payload["status"], "unchanged")
        self.assertEqual(payload["execution"]["placed_count"], 0)
        self.assertEqual(payload["execution"]["canceled_count"], 0)
        self.assertAlmostEqual(payload["reprice_bps"], 0.0)

    async def test_live_cycle_reprices_when_plan_change_exceeds_threshold(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.prepared_create_count = 0

            async def fetch_order_book(
                self,
                *_: object,
                **__: object,
            ) -> OrderBookSnapshot:
                return OrderBookSnapshot(
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    bids=[BookLevel(price=0.000141, amount=100_000)],
                    asks=[BookLevel(price=0.000161, amount=100_000)],
                )

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return [{"id": "old-mm-1"}]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, object]:
                return {"id": order_id, "status": "canceled"}

            async def prepare_limit_orders(
                self,
                *_: object,
                orders: list[dict[str, object]],
                **__: object,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "exchange": "bybit-spot",
                        "symbol": "ACS/USDT",
                        "side": order["side"],
                        "status": "ok",
                        "requested_amount": order["amount"],
                        "requested_price": order["price"],
                        "amount": order["amount"],
                        "price": order["price"],
                        "cost": float(order["amount"]) * float(order["price"]),
                        "limits": {},
                        "precision": {},
                        "errors": [],
                        "warnings": [],
                    }
                    for order in orders
                ]

            async def create_prepared_limit_order(
                self,
                *_: object,
                **__: object,
            ) -> dict[str, str]:
                self.prepared_create_count += 1
                return {"id": f"new-mm-{self.prepared_create_count}"}

        cfg = self._cfg(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=2,
                quote_per_level=1.0,
                depth_shape="flat",
                reprice_threshold_bps=2.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                max_cycle_quote=100.0,
                max_open_orders=50,
                max_cancels_per_cycle=10,
                require_post_only=False,
            ),
        )
        previous_plan = build_symmetric_market_maker_plan(
            OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            ),
            cfg.market_maker,
        ).to_dict()
        manager = FakeManager()

        payload = await run_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=["old-mm-1"],
            previous_plan=previous_plan,
        )

        self.assertEqual(payload["status"], "placed")
        self.assertGreater(payload["reprice_bps"], 2.0)
        self.assertEqual(payload["execution"]["canceled_count"], 1)
        self.assertEqual(manager.prepared_create_count, 4)

    async def test_bithumb_post_only_is_blocked_before_placing(self) -> None:
        class FakeManager:
            async def fetch_order_book(
                self,
                *_: object,
                **__: object,
            ) -> OrderBookSnapshot:
                return OrderBookSnapshot(
                    exchange="bithumb-spot",
                    symbol="ACS/KRW",
                    bids=[BookLevel(price=0.20, amount=100_000)],
                    asks=[BookLevel(price=0.21, amount=100_000)],
                )

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def prepare_limit_order(
                self,
                *_: object,
                amount: float,
                price: float,
                side: str,
                **__: object,
            ) -> dict[str, object]:
                return {
                    "exchange": "bithumb-spot",
                    "symbol": "ACS/KRW",
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

            async def create_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("post-only unsupported cycle must not place orders")

        payload = await run_cycle(
            self._cfg(
                market_maker=MarketMakerConfig(
                    enabled=True,
                    exchange="bithumb-spot",
                    symbol="ACS/KRW",
                    levels=1,
                    quote_per_level=5_000.0,
                    post_only=True,
                ),
                spot_exchanges=[ExchangeConfig(id="bithumb", label="bithumb-spot")],
                quote_rates={"USD": 1.0, "KRW": 0.00073},
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_order_quote=10.0,
                    max_cycle_quote=30.0,
                    require_post_only=False,
                ),
            ),
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertEqual(payload["order_validation"]["status"], "error")
        self.assertTrue(
            any("post-only" in error for error in payload["order_validation"]["errors"])
        )
        self.assertNotIn("execution", payload)

    async def test_krw_market_maker_risk_uses_common_quote(self) -> None:
        class FakeManager:
            async def fetch_order_book(
                self,
                *_: object,
                **__: object,
            ) -> OrderBookSnapshot:
                return OrderBookSnapshot(
                    exchange="bithumb-spot",
                    symbol="ACS/KRW",
                    bids=[BookLevel(price=0.20, amount=100_000)],
                    asks=[BookLevel(price=0.21, amount=100_000)],
                )

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

        payload = await run_cycle(
            self._cfg(
                market_maker=MarketMakerConfig(
                    enabled=True,
                    exchange="bithumb-spot",
                    symbol="ACS/KRW",
                    levels=1,
                    quote_per_level=5_000.0,
                    post_only=False,
                ),
                spot_exchanges=[ExchangeConfig(id="bithumb", label="bithumb-spot")],
                quote_rates={"USD": 1.0, "KRW": 0.00073},
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_order_quote=5.0,
                    max_cycle_quote=20.0,
                    require_post_only=False,
                ),
            ),
            FakeManager(),  # type: ignore[arg-type]
            live=False,
            replace_existing=False,
        )

        self.assertEqual(payload["quote_conversion"]["quote_currency"], "KRW")
        self.assertAlmostEqual(payload["risk"]["total_quote_notional"], 7.3)
        self.assertEqual(payload["risk"]["currency"], "USD")

    def test_quote_conversion_reports_missing_rate(self) -> None:
        cfg = self._cfg(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bithumb-spot",
                symbol="ACS/KRW",
            ),
            quote_rates={"USD": 1.0},
        )

        payload = market_maker_quote_conversion(cfg, "ACS/KRW")

        self.assertFalse(payload["available"])
        self.assertIsNone(payload["quote_to_common_rate"])

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

    def _cfg(
        self,
        *,
        market_maker: Optional[MarketMakerConfig] = None,
        spot_exchanges: Optional[list[ExchangeConfig]] = None,
        quote_rates: Optional[dict[str, float]] = None,
        risk: Optional[RiskConfig] = None,
    ) -> BotConfig:
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
            quote_rates=quote_rates or {"USD": 1.0, "USDT": 1.0, "USDC": 1.0},
            quote_rate_sources=[],
            onchain_monitor=OnchainMonitorConfig(),
            market_maker=market_maker or MarketMakerConfig(
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
            spot_exchanges=spot_exchanges
            or [ExchangeConfig(id="bybit", label="bybit-spot")],
            derivative_exchanges=[],
            risk=risk or RiskConfig(),
            trade_log=TradeLogConfig(enabled=False),
        )


if __name__ == "__main__":
    unittest.main()
