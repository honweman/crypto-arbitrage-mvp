import asyncio
import unittest
from contextlib import redirect_stdout
from io import StringIO
import time
from typing import Optional

from arbitrage_bot.config import (
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
)
from arbitrage_bot.config import TradeLogConfig
from arbitrage_bot.market_maker import (
    cancel_order_ids,
    market_maker_quote_conversion,
    run_cycle,
    run_loop,
)
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

    def test_inventory_control_skews_quote_depth(self) -> None:
        book = OrderBookSnapshot(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            bids=[BookLevel(price=90.0, amount=10.0)],
            asks=[BookLevel(price=110.0, amount=10.0)],
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            levels=1,
            price_band_pct=1.0,
            quote_per_level=100.0,
            depth_shape="flat",
            inventory_control_enabled=True,
            inventory_target_base=1_000.0,
            inventory_band_base=100.0,
            inventory_max_deviation_base=300.0,
        )

        high_inventory = build_symmetric_market_maker_plan(
            book,
            cfg,
            inventory_base=1_200.0,
        )
        low_inventory = build_symmetric_market_maker_plan(
            book,
            cfg,
            inventory_base=800.0,
        )

        high_buy = next(order for order in high_inventory.orders if order.side == "buy")
        high_sell = next(order for order in high_inventory.orders if order.side == "sell")
        low_buy = next(order for order in low_inventory.orders if order.side == "buy")
        low_sell = next(order for order in low_inventory.orders if order.side == "sell")
        self.assertAlmostEqual(high_inventory.inventory_buy_multiplier, 0.5)
        self.assertAlmostEqual(high_inventory.inventory_sell_multiplier, 1.5)
        self.assertAlmostEqual(high_buy.quote_notional, 50.0)
        self.assertAlmostEqual(high_sell.quote_notional, 150.0)
        self.assertAlmostEqual(low_inventory.inventory_buy_multiplier, 1.5)
        self.assertAlmostEqual(low_inventory.inventory_sell_multiplier, 0.5)
        self.assertAlmostEqual(low_buy.quote_notional, 150.0)
        self.assertAlmostEqual(low_sell.quote_notional, 50.0)

    def test_run_cycle_uses_instance_gap_override(self) -> None:
        class FakeManager:
            async def fetch_order_book(
                self,
                *_: object,
                **__: object,
            ) -> OrderBookSnapshot:
                return OrderBookSnapshot(
                    exchange="upbit-spot",
                    symbol="ACS/USDT",
                    bids=[
                        BookLevel(price=0.20, amount=100_000),
                        BookLevel(price=0.08, amount=100_000),
                    ],
                    asks=[
                        BookLevel(price=0.21, amount=100_000),
                        BookLevel(price=0.34, amount=100_000),
                    ],
                )

        payload = asyncio.run(
            run_cycle(
                BotConfig(
                    poll_seconds=1.0,
                    order_book_depth=20,
                    notional_quote=200.0,
                    min_profit_quote=0.1,
                    min_profit_bps=1.0,
                    min_basis_bps=15.0,
                    common_quote_currency="USD",
                    quote_rates={"USD": 1.0, "USDT": 1.0},
                    quote_rate_sources=[],
                    onchain_monitor=OnchainMonitorConfig(),
                    market_maker=MarketMakerConfig(
                        enabled=True,
                        exchange="upbit-spot",
                        symbol="ACS/USDT",
                        levels=1,
                        price_band_pct=1.0,
                        quote_per_level=1.0,
                        max_order_quote=2.0,
                        max_cycle_quote=3.0,
                        max_order_book_gap_bps=10_000.0,
                    ),
                    slow_execution=SlowExecutionConfig(),
                    portfolio=PortfolioConfig(),
                    spot_symbols=[],
                    spot_markets=[],
                    cash_and_carry_pairs=[],
                    spot_exchanges=[ExchangeConfig(id="upbit", label="upbit-spot")],
                    derivative_exchanges=[],
                    risk=RiskConfig(
                        allow_live_trading=True,
                        max_order_quote=0.5,
                        max_cycle_quote=1.0,
                        max_order_book_gap_bps=5_000.0,
                    ),
                    trade_log=TradeLogConfig(enabled=False),
                ),
                FakeManager(),  # type: ignore[arg-type]
                live=False,
                replace_existing=False,
            )
        )

        self.assertTrue(payload["risk"]["approved"])
        self.assertGreater(payload["plan"]["max_level_gap_bps"], 5_000.0)
        self.assertEqual(payload["risk"]["reasons"], [])

    def test_plan_keeps_order_book_received_time_for_freshness_checks(self) -> None:
        received_at = time.time()
        book = OrderBookSnapshot(
            exchange="upbit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
            timestamp_ms=123_000,
            received_at=received_at,
            source="rest",
        )
        cfg = MarketMakerConfig(
            enabled=True,
            exchange="upbit-spot",
            symbol="ACS/USDT",
            levels=1,
            price_band_pct=1.0,
            quote_per_level=10.0,
        )

        plan = build_symmetric_market_maker_plan(book, cfg)

        self.assertEqual(plan.order_book_timestamp_ms, 123_000)
        self.assertEqual(plan.order_book_received_at, received_at)
        self.assertEqual(plan.to_dict()["order_book_received_at"], received_at)


class MarketMakerLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_uses_supplied_order_book_without_rest_fetch(self) -> None:
        class FakeManager:
            async def fetch_order_book(self, *_: object, **__: object) -> None:
                raise AssertionError("cached order book should avoid REST fetch")

        payload = await run_cycle(
            self._cfg(),
            FakeManager(),  # type: ignore[arg-type]
            live=False,
            replace_existing=False,
            order_book=OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
                source="websocket",
            ),
        )

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["market_data"]["source"], "websocket")
        self.assertAlmostEqual(payload["plan"]["mid_price"], 0.00015)

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

    async def test_live_cycle_replaces_all_current_open_order_ids(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.canceled: list[str] = []
                self.open_order_ids = ["old-mm-1", "manual-1"]
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
                return [{"id": order_id} for order_id in self.open_order_ids]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, object]:
                self.canceled.append(order_id)
                self.open_order_ids = [
                    item for item in self.open_order_ids if item != order_id
                ]
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
            replace_order_ids=["old-mm-1", "manual-1"],
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(manager.canceled, ["old-mm-1", "manual-1"])
        self.assertEqual(payload["execution"]["canceled_count"], 2)
        self.assertEqual(payload["execution"]["placed_count"], 20)
        self.assertEqual(len(payload["execution"]["placed_order_ids"]), 20)

    async def test_live_cycle_does_not_place_until_cancels_are_confirmed(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.created = 0
                self.canceled: list[str] = []

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

            async def cancel_order(
                self,
                *_: object,
                **__: object,
            ) -> None:
                raise RuntimeError("temporary cancel failure")

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

        self.assertEqual(payload["status"], "cancel_retry")
        self.assertEqual(manager.created, 0)
        self.assertTrue(payload["execution"]["cancel_retry_required"])
        self.assertEqual(payload["execution"]["remaining_open_order_ids"], ["old-mm-1"])

    async def test_live_cycle_reports_partial_create_failure(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.created = 0
                self.canceled: list[str] = []

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

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, str]:
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
                if self.created == 2:
                    raise RuntimeError("temporary create failure")
                return {"id": f"new-mm-{self.created}"}

        payload = await run_cycle(
            self._cfg(
                market_maker=MarketMakerConfig(
                    enabled=True,
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    levels=2,
                    quote_per_level=1.0,
                ),
                risk=RiskConfig(
                    allow_live_trading=True,
                    max_cycle_quote=100.0,
                    max_open_orders=50,
                    max_cancels_per_cycle=10,
                    require_post_only=False,
                ),
            ),
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "execution_error")
        self.assertEqual(payload["execution"]["placed_count"], 1)
        self.assertEqual(payload["execution"]["placed_order_ids"], ["new-mm-1"])
        self.assertTrue(payload["execution"]["partial_create"])
        self.assertTrue(payload["execution"]["emergency_cancel"])
        self.assertEqual(payload["execution"]["emergency_canceled_count"], 1)
        self.assertEqual(
            payload["execution"]["emergency_canceled_order_ids"],
            ["new-mm-1"],
        )
        self.assertFalse(payload["execution"]["manual_intervention_required"])
        self.assertEqual(len(payload["execution"]["create_errors"]), 1)
        self.assertIn(
            "temporary create failure",
            payload["execution"]["create_errors"][0]["error"],
        )

    async def test_live_cycle_force_replace_rebuilds_grid_despite_reprice_threshold(
        self,
    ) -> None:
        """
        Regression test: when force_replace is active (fills detected), passing
        existing_open_orders to run_cycle allows _previous_plan_from_open_orders to
        reconstruct a "previous plan" from whatever orders remain.  If prices are
        stable the reprice-threshold check then returns "unchanged" and skips the
        full grid rebuild we explicitly want.

        The fix: pass existing_open_orders=None when force_replace=True so that
        no previous plan can be reconstructed from the remaining open orders.
        """
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
        cfg = self._cfg(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=2,
                quote_per_level=1.0,
                depth_shape="flat",
                reprice_threshold_bps=5.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                max_cycle_quote=100.0,
                max_open_orders=50,
                max_cancels_per_cycle=10,
                require_post_only=False,
            ),
        )
        plan = build_symmetric_market_maker_plan(book, cfg.market_maker)
        existing_orders = [
            {
                "id": f"old-mm-{index}",
                "side": order.side,
                "price": order.price,
                "amount": order.amount,
            }
            for index, order in enumerate(plan.orders, start=1)
        ]
        replace_order_ids = [str(order["id"]) for order in existing_orders]

        class FakeManager:
            def __init__(self) -> None:
                self.remaining_ids = list(replace_order_ids)
                self.placed: list[str] = []

            async def fetch_order_book(self, *_: object, **__: object) -> OrderBookSnapshot:
                return book

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return [{"id": oid} for oid in self.remaining_ids]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, object]:
                self.remaining_ids = [i for i in self.remaining_ids if i != order_id]
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
                self.placed.append("new")
                return {"id": f"new-mm-{len(self.placed)}"}

        # Bug scenario: passing existing_open_orders (remaining orders after fills)
        # tricks the reprice-threshold check into returning "unchanged" even though
        # force_replace is True and we need a full rebuild.
        bug_manager = FakeManager()
        bug_payload = await run_cycle(
            cfg,
            bug_manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=replace_order_ids,
            previous_plan=None,
            existing_open_orders=existing_orders,
            order_book=book,
        )
        self.assertEqual(bug_payload["status"], "unchanged",
                         "confirms bug: passing existing_open_orders with no previous_plan "
                         "triggers false 'unchanged' via reprice threshold")

        # Fix scenario: passing existing_open_orders=None (as the fix does when force_replace)
        # prevents plan reconstruction so the full grid is placed.
        fix_manager = FakeManager()
        fix_payload = await run_cycle(
            cfg,
            fix_manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=replace_order_ids,
            previous_plan=None,
            existing_open_orders=None,
            order_book=book,
        )
        self.assertEqual(fix_payload["status"], "placed",
                         "fix: passing existing_open_orders=None triggers full grid rebuild")
        self.assertEqual(fix_payload["execution"]["placed_count"], len(plan.orders))

    async def test_live_cycle_adopts_matching_open_orders_after_restart(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
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
        plan = build_symmetric_market_maker_plan(book, cfg.market_maker)
        prepared_orders = [
            {
                "exchange": "bybit-spot",
                "symbol": "ACS/USDT",
                "side": order.side,
                "status": "ok",
                "requested_amount": order.amount,
                "requested_price": order.price,
                "amount": order.amount,
                "price": order.price + (0.00000005 if order.side == "sell" else -0.00000005),
                "cost": order.amount
                * (order.price + (0.00000005 if order.side == "sell" else -0.00000005)),
                "limits": {},
                "precision": {},
                "errors": [],
                "warnings": [],
            }
            for order in plan.orders
        ]
        existing_orders = [
            {
                "id": f"old-mm-{index}",
                "side": order.side,
                "price": prepared["price"],
                "amount": order.quote_notional / float(prepared["price"]),
                "remaining": order.quote_notional / float(prepared["price"]),
            }
            for index, (order, prepared) in enumerate(
                zip(plan.orders, prepared_orders),
                start=1,
            )
        ]

        class FakeManager:
            def __init__(self) -> None:
                self.prepare_count = 0

            async def fetch_order_book(
                self,
                *_: object,
                **__: object,
            ) -> OrderBookSnapshot:
                return book

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return existing_orders

            async def cancel_order(self, *_: object, **__: object) -> None:
                raise AssertionError("matching restart orders should not be canceled")

            async def prepare_limit_orders(
                self,
                *_: object,
                **__: object,
            ) -> list[dict[str, object]]:
                self.prepare_count += 1
                return prepared_orders

        manager = FakeManager()
        payload = await run_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=[str(order["id"]) for order in existing_orders],
            existing_open_orders=existing_orders,
        )

        self.assertEqual(payload["status"], "unchanged")
        self.assertTrue(payload["adopted_existing_open_orders"])
        self.assertAlmostEqual(payload["reprice_bps"], 0.0)
        self.assertEqual(payload["execution"]["placed_count"], 0)
        self.assertEqual(manager.prepare_count, 1)

    async def test_live_cycle_rebuilds_partially_filled_orders_after_restart(
        self,
    ) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
        cfg = self._cfg(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=1,
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
        plan = build_symmetric_market_maker_plan(book, cfg.market_maker)
        prepared_orders = [
            {
                "exchange": "bybit-spot",
                "symbol": "ACS/USDT",
                "side": order.side,
                "status": "ok",
                "requested_amount": order.amount,
                "requested_price": order.price,
                "amount": order.amount,
                "price": order.price,
                "cost": order.quote_notional,
                "limits": {},
                "precision": {},
                "errors": [],
                "warnings": [],
            }
            for order in plan.orders
        ]
        existing_orders = [
            {
                "id": f"old-mm-{index}",
                "side": order.side,
                "price": order.price,
                "amount": order.amount,
                "remaining": order.amount * (0.5 if index == 1 else 1.0),
            }
            for index, order in enumerate(plan.orders, start=1)
        ]

        class FakeManager:
            def __init__(self) -> None:
                self.open_orders = list(existing_orders)
                self.placed = 0

            async def fetch_order_book(
                self,
                *_: object,
                **__: object,
            ) -> OrderBookSnapshot:
                return book

            async def fetch_open_orders(
                self,
                *_: object,
                **__: object,
            ) -> list[dict[str, object]]:
                return list(self.open_orders)

            async def prepare_limit_orders(
                self,
                *_: object,
                **__: object,
            ) -> list[dict[str, object]]:
                return prepared_orders

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, str]:
                self.open_orders = [
                    order
                    for order in self.open_orders
                    if order.get("id") != order_id
                ]
                return {"id": order_id, "status": "canceled"}

            async def create_prepared_limit_order(
                self,
                *_: object,
                **__: object,
            ) -> dict[str, str]:
                self.placed += 1
                return {"id": f"new-mm-{self.placed}"}

        manager = FakeManager()
        payload = await run_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=[str(order["id"]) for order in existing_orders],
            existing_open_orders=existing_orders,
        )

        self.assertEqual(payload["status"], "placed")
        self.assertFalse(payload.get("adopted_existing_open_orders", False))
        self.assertIsNone(payload["reprice_bps"])
        self.assertEqual(payload["execution"]["canceled_count"], 2)
        self.assertEqual(payload["execution"]["placed_count"], 2)

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

    async def test_live_cycle_uses_batch_create_when_available(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.batch_create_count = 0

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

            async def create_prepared_limit_orders(
                self,
                *_: object,
                prepared_orders: list[dict[str, object]],
                **__: object,
            ) -> list[dict[str, str]]:
                self.batch_create_count += 1
                return [
                    {"id": f"batch-mm-{index}"}
                    for index, _ in enumerate(prepared_orders, 1)
                ]

            async def create_prepared_limit_order(self, *_: object, **__: object) -> None:
                raise AssertionError("batch path should avoid single order placement")

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
        self.assertEqual(manager.batch_create_count, 1)
        self.assertTrue(payload["execution"]["used_batch_create"])
        self.assertEqual(payload["execution"]["placed_count"], 4)

    async def test_live_cycle_tracks_open_orders_after_batch_create_error(self) -> None:
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
                return [{"id": "maybe-batch-mm-1"}]

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

            async def create_prepared_limit_orders(self, *_: object, **__: object) -> None:
                raise RuntimeError("batch response timeout")

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
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
        )

        self.assertEqual(payload["status"], "execution_error")
        self.assertTrue(payload["execution"]["used_batch_create"])
        self.assertTrue(payload["execution"]["create_result_uncertain"])
        self.assertEqual(
            payload["execution"]["remaining_open_order_ids"],
            ["maybe-batch-mm-1"],
        )
        self.assertIn(
            "batch response timeout",
            payload["execution"]["create_errors"][0]["error"],
        )

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
                return [{"id": f"old-mm-{index}"} for index in range(1, 5)]

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
            replace_order_ids=[f"old-mm-{index}" for index in range(1, 5)],
            previous_plan=previous_plan,
        )

        self.assertEqual(payload["status"], "unchanged")
        self.assertEqual(payload["execution"]["placed_count"], 0)
        self.assertEqual(payload["execution"]["canceled_count"], 0)
        self.assertAlmostEqual(payload["reprice_bps"], 0.0)

    async def test_live_cycle_rebuilds_when_tracked_orders_are_missing(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.created = 0
                self.open_order_ids = ["old-mm-1", "old-mm-2", "old-mm-3"]

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
                return [{"id": order_id} for order_id in self.open_order_ids]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, object]:
                self.open_order_ids = [
                    item for item in self.open_order_ids if item != order_id
                ]
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
                self.created += 1
                return {"id": f"new-mm-{self.created}"}

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
            replace_order_ids=["old-mm-1", "old-mm-2", "old-mm-3"],
            previous_plan=previous_plan,
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(payload["tracked_open_order_count"], 3)
        self.assertEqual(payload["expected_open_order_count"], 4)
        self.assertIn("rebuilding ladder", payload["reprice_skip_blocked_reason"])
        self.assertEqual(payload["execution"]["canceled_count"], 3)
        self.assertEqual(payload["execution"]["placed_count"], 4)
        self.assertEqual(len(payload["execution"]["placed_order_ids"]), 4)

    async def test_live_cycle_reprices_when_plan_change_exceeds_threshold(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.prepared_create_count = 0
                self.open_order_ids = ["old-mm-1"]

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
                return [{"id": order_id} for order_id in self.open_order_ids]

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, object]:
                self.open_order_ids = [
                    item for item in self.open_order_ids if item != order_id
                ]
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

    async def test_live_cycle_hysteresis_keeps_the_real_order_anchor(self) -> None:
        base_book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
        moved_book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014 * 1.0003, amount=100_000)],
            asks=[BookLevel(price=0.00016 * 1.0003, amount=100_000)],
        )
        maker_cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=2,
            quote_per_level=1.0,
            depth_shape="flat",
            reprice_threshold_bps=2.0,
            reprice_hysteresis_bps=3.0,
            full_reprice_threshold_bps=25.0,
        )
        cfg = self._cfg(
            market_maker=maker_cfg,
            risk=RiskConfig(
                allow_live_trading=True,
                max_cycle_quote=100.0,
                max_open_orders=50,
                max_cancels_per_cycle=10,
                require_post_only=False,
            ),
        )
        previous_plan = build_symmetric_market_maker_plan(
            base_book,
            maker_cfg,
        ).to_dict()

        class FakeManager:
            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return [{"id": f"old-mm-{index}"} for index in range(1, 5)]

        payload = await run_cycle(
            cfg,
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=[f"old-mm-{index}" for index in range(1, 5)],
            previous_plan=previous_plan,
            previous_mid_price=float(previous_plan["mid_price"]),
            order_book=moved_book,
        )

        self.assertEqual(payload["status"], "unchanged")
        self.assertEqual(payload["replacement_mode"], "unchanged")
        self.assertAlmostEqual(payload["effective_reprice_threshold_bps"], 5.0)
        self.assertEqual(payload["active_plan"], previous_plan)
        self.assertGreater(payload["reprice_bps"], 3.0)
        self.assertLess(payload["reprice_bps"], 5.0)

    async def test_live_cycle_reprices_only_the_changed_level(self) -> None:
        book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
        maker_cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=2,
            quote_per_level=1.0,
            depth_shape="flat",
            reprice_threshold_bps=2.0,
            reprice_hysteresis_bps=3.0,
            full_reprice_threshold_bps=25.0,
        )
        cfg = self._cfg(
            market_maker=maker_cfg,
            risk=RiskConfig(
                allow_live_trading=True,
                max_cycle_quote=100.0,
                max_open_orders=50,
                max_cancels_per_cycle=10,
                require_post_only=False,
            ),
        )
        current_plan = build_symmetric_market_maker_plan(book, maker_cfg)
        previous_plan = current_plan.to_dict()
        previous_plan["orders"][0]["price"] *= 0.998
        open_orders = [
            {
                "id": f"old-mm-{index}",
                "side": order["side"],
                "price": order["price"],
                "amount": order["amount"],
                "remaining": order["amount"],
                "filled": 0.0,
            }
            for index, order in enumerate(previous_plan["orders"], start=1)
        ]

        class FakeManager:
            def __init__(self) -> None:
                self.open_orders = list(open_orders)
                self.canceled: list[str] = []
                self.created = 0

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return list(self.open_orders)

            async def cancel_order(
                self,
                *_: object,
                order_id: str,
                **__: object,
            ) -> dict[str, str]:
                self.canceled.append(order_id)
                self.open_orders = [
                    order for order in self.open_orders if order["id"] != order_id
                ]
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
                        "cost": order["quote_notional"],
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
                self.created += 1
                return {"id": f"new-mm-{self.created}"}

        manager = FakeManager()
        payload = await run_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=[str(order["id"]) for order in open_orders],
            previous_plan=previous_plan,
            existing_open_orders=open_orders,
            previous_mid_price=current_plan.mid_price,
            order_book=book,
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(payload["replacement_mode"], "partial")
        self.assertEqual(payload["replaced_levels"], [{"side": "buy", "level": 1}])
        self.assertEqual(manager.canceled, ["old-mm-1"])
        self.assertEqual(payload["execution"]["canceled_count"], 1)
        self.assertEqual(payload["execution"]["placed_count"], 1)
        self.assertEqual(payload["risk"]["expected_cancel_count"], 1)
        self.assertEqual(payload["risk"]["expected_create_count"], 1)
        self.assertEqual(payload["risk"]["projected_open_orders"], 4)
        self.assertEqual(payload["execution"]["retained_order_ids"], [
            "old-mm-2",
            "old-mm-3",
            "old-mm-4",
        ])
        self.assertEqual(len(payload["execution"]["active_order_ids"]), 4)
        self.assertEqual(len(payload["active_plan"]["orders"]), 4)

    async def test_live_cycle_full_reprices_after_significant_mid_move(self) -> None:
        base_book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014, amount=100_000)],
            asks=[BookLevel(price=0.00016, amount=100_000)],
        )
        moved_book = OrderBookSnapshot(
            exchange="bybit-spot",
            symbol="ACS/USDT",
            bids=[BookLevel(price=0.00014 * 1.003, amount=100_000)],
            asks=[BookLevel(price=0.00016 * 1.003, amount=100_000)],
        )
        maker_cfg = MarketMakerConfig(
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=2,
            quote_per_level=1.0,
            depth_shape="flat",
            reprice_threshold_bps=2.0,
            reprice_hysteresis_bps=3.0,
            full_reprice_threshold_bps=25.0,
        )
        cfg = self._cfg(
            market_maker=maker_cfg,
            risk=RiskConfig(
                allow_live_trading=True,
                max_cycle_quote=100.0,
                max_open_orders=50,
                max_cancels_per_cycle=10,
                require_post_only=False,
            ),
        )
        previous_plan = build_symmetric_market_maker_plan(
            base_book,
            maker_cfg,
        ).to_dict()
        open_orders = [
            {
                "id": f"old-mm-{index}",
                "side": order["side"],
                "price": order["price"],
                "amount": order["amount"],
                "remaining": order["amount"],
                "filled": 0.0,
            }
            for index, order in enumerate(previous_plan["orders"], start=1)
        ]

        class FakeManager:
            def __init__(self) -> None:
                self.open_orders = list(open_orders)

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return list(self.open_orders)

            async def cancel_orders(
                self,
                *_: object,
                order_ids: list[str],
                **__: object,
            ) -> list[dict[str, str]]:
                self.open_orders = [
                    order for order in self.open_orders if order["id"] not in order_ids
                ]
                return [{"id": order_id, "status": "canceled"} for order_id in order_ids]

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
                        "cost": order["quote_notional"],
                        "limits": {},
                        "precision": {},
                        "errors": [],
                        "warnings": [],
                    }
                    for order in orders
                ]

            async def create_prepared_limit_orders(
                self,
                *_: object,
                prepared_orders: list[dict[str, object]],
                **__: object,
            ) -> list[dict[str, str]]:
                return [
                    {"id": f"new-mm-{index}"}
                    for index, _ in enumerate(prepared_orders, start=1)
                ]

        payload = await run_cycle(
            cfg,
            FakeManager(),  # type: ignore[arg-type]
            live=True,
            replace_existing=False,
            replace_order_ids=[str(order["id"]) for order in open_orders],
            previous_plan=previous_plan,
            existing_open_orders=open_orders,
            previous_mid_price=float(previous_plan["mid_price"]),
            order_book=moved_book,
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(payload["replacement_mode"], "full")
        self.assertGreaterEqual(payload["mid_move_bps"], 25.0)
        self.assertIn("above full-reprice threshold", payload["full_replace_reason"])
        self.assertEqual(payload["execution"]["canceled_count"], 4)
        self.assertEqual(payload["execution"]["placed_count"], 4)

    async def test_cancel_order_ids_uses_batch_cancel_when_available(self) -> None:
        class FakeManager:
            def __init__(self) -> None:
                self.batch_cancel_count = 0

            async def cancel_orders(
                self,
                *_: object,
                order_ids: list[str],
                **__: object,
            ) -> list[dict[str, str]]:
                self.batch_cancel_count += 1
                return [
                    {"id": order_id, "status": "canceled"}
                    for order_id in order_ids
                ]

            async def cancel_order(self, *_: object, **__: object) -> None:
                raise AssertionError("batch cancel should avoid single cancel")

        manager = FakeManager()
        payload = await cancel_order_ids(
            self._cfg(),
            manager,  # type: ignore[arg-type]
            ["a", "b", "c"],
        )

        self.assertTrue(payload["used_batch_cancel"])
        self.assertEqual(payload["canceled_count"], 3)
        self.assertEqual(manager.batch_cancel_count, 1)

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

    def test_quote_conversion_strips_contract_settlement_suffix(self) -> None:
        cfg = self._cfg(
            quote_rates={"USD": 1.0, "USDT": 1.0},
        )

        payload = market_maker_quote_conversion(cfg, "BTC/USDT:USDT")

        self.assertTrue(payload["available"])
        self.assertEqual(payload["quote_currency"], "USDT")
        self.assertEqual(payload["quote_to_common_rate"], 1.0)

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
