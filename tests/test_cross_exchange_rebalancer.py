from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.config import (
    BotConfig,
    CrossExchangeRebalanceConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
)
from arbitrage_bot.cross_exchange_rebalancer import (
    apply_rebalance_cycle_to_runtime,
    build_cross_exchange_rebalance_plan,
    load_rebalance_runtime,
    new_rebalance_runtime,
    run_cross_exchange_rebalance_cycle,
    save_rebalance_runtime,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


def make_config(
    *,
    rebalance: CrossExchangeRebalanceConfig | None = None,
    risk: RiskConfig | None = None,
) -> BotConfig:
    return BotConfig(
        poll_seconds=1.0,
        order_book_depth=20,
        notional_quote=100.0,
        min_profit_quote=0.0,
        min_profit_bps=0.0,
        min_basis_bps=0.0,
        common_quote_currency="USD",
        quote_rates={"USD": 1.0, "USDC": 1.0, "KRW": 0.00075},
        quote_rate_sources=[],
        onchain_monitor=OnchainMonitorConfig(),
        market_maker=MarketMakerConfig(),
        slow_execution=SlowExecutionConfig(),
        portfolio=PortfolioConfig(),
        spot_symbols=[],
        spot_markets=[
            SpotMarketConfig(
                asset="ACS",
                exchange="bithumb-spot",
                symbol="ACS/KRW",
                quote_currency="KRW",
            ),
            SpotMarketConfig(
                asset="ACS",
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                quote_currency="USDC",
            ),
        ],
        cash_and_carry_pairs=[],
        spot_exchanges=[
            ExchangeConfig(id="bithumb", label="bithumb-spot", fee_bps=4.0),
            ExchangeConfig(id="coinbase", label="coinbase-spot", fee_bps=60.0),
        ],
        derivative_exchanges=[],
        cross_exchange_rebalance=rebalance
        or CrossExchangeRebalanceConfig(
            enabled=True,
            buy_exchange="bithumb-spot",
            buy_symbol="ACS/KRW",
            sell_exchange="coinbase-spot",
            sell_symbol="ACS/USDC",
            total_quote_common=100.0,
            quote_per_cycle_common=10.0,
            max_cost_bps=500.0,
            max_slippage_bps=100.0,
            order_ttl_seconds=0.0,
        ),
        risk=risk or RiskConfig(),
    )


def make_books() -> dict[tuple[str, str], OrderBookSnapshot]:
    return {
        ("bithumb-spot", "ACS/KRW"): OrderBookSnapshot(
            exchange="bithumb-spot",
            symbol="ACS/KRW",
            bids=[BookLevel(price=0.169, amount=1_000_000)],
            asks=[BookLevel(price=0.170, amount=1_000_000)],
        ),
        ("coinbase-spot", "ACS/USDC"): OrderBookSnapshot(
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            bids=[BookLevel(price=0.0001265, amount=1_000_000)],
            asks=[BookLevel(price=0.0001270, amount=1_000_000)],
        ),
    }


class PreparingManager:
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
            "amount": amount,
            "price": price,
            "cost": amount * price,
            "errors": [],
            "warnings": [],
        }


class CrossExchangeRebalancerTest(unittest.IsolatedAsyncioTestCase):
    def test_plan_moves_cash_and_base_in_opposite_directions(self) -> None:
        plan = build_cross_exchange_rebalance_plan(
            make_config(),
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            completed_quote_common=25.0,
        )

        self.assertEqual(plan.base_asset, "ACS")
        self.assertEqual(plan.buy_quote_currency, "KRW")
        self.assertEqual(plan.sell_quote_currency, "USDC")
        self.assertEqual(plan.remaining_quote_common, 75.0)
        self.assertLessEqual(plan.buy_cost_common, 10.0 + 1e-9)
        self.assertLessEqual(plan.sell_proceeds_common, 10.0 + 1e-9)
        self.assertGreater(plan.quantity_base, 0)
        opportunity = plan.opportunity()
        self.assertEqual(opportunity.legs[0].side, "buy")
        self.assertEqual(opportunity.legs[1].side, "sell")
        self.assertEqual(
            opportunity.metadata["purpose"],
            "synthetic_cross_exchange_inventory_transfer",
        )

    async def test_cycle_waits_when_rebalance_cost_is_too_high(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=1.0,
            )
        )

        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            object(),  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=False,
        )

        self.assertEqual(payload["status"], "waiting_for_cost")
        self.assertFalse(payload["risk"]["approved"])

    async def test_live_cycle_requires_explicit_risk_strategy_switch(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
            ),
            risk=RiskConfig(allow_live_trading=True, require_post_only=False),
        )

        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            object(),  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertIn("not explicitly true", payload["risk"]["reasons"][0])

    async def test_live_cycle_blocks_opposite_open_order(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
            ),
        )

        class FakeManager(PreparingManager):
            async def fetch_open_orders(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
            ) -> list[dict[str, str]]:
                if exchange.key == "bithumb-spot":
                    return [{"id": "mm-sell", "side": "sell", "symbol": symbol}]
                return []

        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            FakeManager(),  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )

        self.assertEqual(payload["status"], "blocked_by_conflict")
        self.assertTrue(payload["conflict_guard"]["blocked"])
        self.assertEqual(payload["conflict_guard"]["orders"][0]["order_id"], "mm-sell")

    async def test_balanced_live_fills_advance_progress(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
                max_slippage_bps=100.0,
                order_ttl_seconds=0.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
                max_order_quote=100.0,
                max_cycle_quote=250.0,
                max_slippage_bps=100.0,
            ),
        )

        class FakeManager(PreparingManager):
            def __init__(self) -> None:
                self.created = 0

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_closed_orders(
                self, *_: object, **__: object
            ) -> list[object]:
                return []

            async def fetch_balance(
                self,
                exchange: ExchangeConfig,
            ) -> dict[str, dict[str, float]]:
                if exchange.key == "bithumb-spot":
                    return {"KRW": {"free": 1_000_000.0}}
                return {"ACS": {"free": 1_000_000.0}}

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
                side: str,
                prepared: dict[str, object],
                **__: object,
            ) -> dict[str, object]:
                self.created += 1
                amount = float(prepared["amount"])
                price = float(prepared["price"])
                return {
                    "id": f"fill-{self.created}",
                    "exchange": exchange.key,
                    "side": side,
                    "filled": amount,
                    "cost": amount * price,
                    "average": price,
                    "status": "closed",
                }

        manager = FakeManager()
        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )

        self.assertEqual(payload["status"], "progress")
        self.assertEqual(manager.created, 2)
        self.assertFalse(payload["execution_progress"]["hedge_required"])
        self.assertGreater(
            payload["execution_progress"]["progress_quote_common"],
            0,
        )

    async def test_live_cycle_aligns_precision_and_completes_source_spend(
        self,
    ) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="coinbase-spot",
                buy_symbol="ACS/USDC",
                sell_exchange="bithumb-spot",
                sell_symbol="ACS/KRW",
                total_quote_common=10.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
                max_slippage_bps=100.0,
                order_ttl_seconds=0.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
                max_order_quote=100.0,
                max_cycle_quote=250.0,
                max_slippage_bps=100.0,
            ),
        )

        class PrecisionManager:
            def __init__(self) -> None:
                self.created_amounts: dict[str, float] = {}

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_closed_orders(
                self, *_: object, **__: object
            ) -> list[object]:
                return []

            async def fetch_balance(
                self,
                exchange: ExchangeConfig,
            ) -> dict[str, dict[str, float]]:
                if exchange.key == "coinbase-spot":
                    return {"USDC": {"free": 1_000_000.0}}
                return {"ACS": {"free": 1_000_000.0}}

            async def prepare_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                side: str,
                amount: float,
                price: float,
            ) -> dict[str, object]:
                prepared_amount = (
                    float(math.floor(amount))
                    if exchange.key == "coinbase-spot"
                    else math.floor(amount * 10_000) / 10_000
                )
                return {
                    "exchange": exchange.key,
                    "symbol": symbol,
                    "side": side,
                    "status": "ok",
                    "amount": prepared_amount,
                    "price": price,
                    "cost": prepared_amount * price,
                    "errors": [],
                    "warnings": [],
                }

            async def create_prepared_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                side: str,
                prepared: dict[str, object],
                **__: object,
            ) -> dict[str, object]:
                amount = float(prepared["amount"])
                price = float(prepared["price"])
                self.created_amounts[exchange.key] = amount
                return {
                    "id": f"{exchange.key}-{side}",
                    "exchange": exchange.key,
                    "side": side,
                    "filled": amount,
                    "cost": amount * price,
                    "average": price,
                    "status": "closed",
                }

        manager = PrecisionManager()
        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )
        runtime = apply_rebalance_cycle_to_runtime(
            new_rebalance_runtime(
                cfg.cross_exchange_rebalance,
                common_quote_currency="USD",
            ),
            payload,
            cfg.cross_exchange_rebalance,
        )

        self.assertEqual(payload["status"], "progress")
        self.assertEqual(payload["precision_alignment"]["attempt_count"], 2)
        self.assertEqual(
            manager.created_amounts["coinbase-spot"],
            manager.created_amounts["bithumb-spot"],
        )
        self.assertFalse(payload["execution_progress"]["hedge_required"])
        self.assertAlmostEqual(
            payload["execution_progress"]["source_progress_quote_common"],
            10.0,
        )
        self.assertLess(
            payload["execution_progress"]["destination_quote_common"],
            10.0,
        )
        self.assertEqual(runtime["status"], "complete")
        self.assertEqual(runtime["completed_quote_common"], 10.0)
        self.assertGreater(runtime["completed_destination_quote_common"], 0.0)

    async def test_live_cycle_sells_only_the_confirmed_buy_fill(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="coinbase-spot",
                buy_symbol="ACS/USDC",
                sell_exchange="bithumb-spot",
                sell_symbol="ACS/KRW",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
                max_slippage_bps=100.0,
                order_ttl_seconds=0.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
                max_order_quote=100.0,
                max_cycle_quote=250.0,
                max_slippage_bps=100.0,
            ),
        )

        class PartialBuyManager(PreparingManager):
            def __init__(self) -> None:
                self.created: list[tuple[str, float]] = []

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_closed_orders(
                self, *_: object, **__: object
            ) -> list[object]:
                return []

            async def fetch_balance(
                self, exchange: ExchangeConfig
            ) -> dict[str, dict[str, float]]:
                if exchange.key == "coinbase-spot":
                    return {"USDC": {"free": 1_000_000.0}}
                return {"ACS": {"free": 1_000_000.0}}

            async def create_prepared_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                side: str,
                prepared: dict[str, object],
                **__: object,
            ) -> dict[str, object]:
                amount = float(prepared["amount"])
                price = float(prepared["price"])
                filled = amount * 0.25 if side == "buy" else amount
                self.created.append((side, amount))
                return {
                    "id": f"{side}-{len(self.created)}",
                    "exchange": exchange.key,
                    "side": side,
                    "filled": filled,
                    "cost": filled * price,
                    "average": price,
                    "status": "closed",
                }

        manager = PartialBuyManager()
        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )

        self.assertEqual(payload["status"], "progress")
        self.assertEqual([side for side, _ in manager.created], ["buy", "sell"])
        self.assertAlmostEqual(manager.created[1][1], manager.created[0][1] * 0.25)
        self.assertFalse(payload["execution_progress"]["hedge_required"])
        self.assertEqual(
            payload["execution"]["execution_mode"],
            "buy_then_sell",
        )

    async def test_live_cycle_skips_sell_when_buy_has_no_fill(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="coinbase-spot",
                buy_symbol="ACS/USDC",
                sell_exchange="bithumb-spot",
                sell_symbol="ACS/KRW",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
                max_slippage_bps=100.0,
                order_ttl_seconds=0.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
                max_order_quote=100.0,
                max_cycle_quote=250.0,
                max_slippage_bps=100.0,
            ),
        )

        class NoBuyFillManager(PreparingManager):
            def __init__(self) -> None:
                self.created_sides: list[str] = []

            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_closed_orders(
                self, *_: object, **__: object
            ) -> list[object]:
                return []

            async def fetch_balance(
                self, exchange: ExchangeConfig
            ) -> dict[str, dict[str, float]]:
                if exchange.key == "coinbase-spot":
                    return {"USDC": {"free": 1_000_000.0}}
                return {"ACS": {"free": 1_000_000.0}}

            async def create_prepared_limit_order(
                self,
                exchange: ExchangeConfig,
                *,
                side: str,
                prepared: dict[str, object],
                **__: object,
            ) -> dict[str, object]:
                self.created_sides.append(side)
                return {
                    "id": f"{side}-order",
                    "exchange": exchange.key,
                    "side": side,
                    "filled": 0.0,
                    "cost": 0.0,
                    "status": "closed",
                }

        manager = NoBuyFillManager()
        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            manager,  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )

        self.assertEqual(payload["status"], "no_fill")
        self.assertEqual(manager.created_sides, ["buy"])
        self.assertFalse(payload["halt_required"])
        self.assertFalse(payload["execution_progress"]["hedge_required"])

    async def test_live_cycle_blocks_when_configured_reserve_is_unavailable(
        self,
    ) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
                buy_quote_reserve=1_000_000.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
            ),
        )

        class FakeManager(PreparingManager):
            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_balance(
                self,
                exchange: ExchangeConfig,
            ) -> dict[str, dict[str, float]]:
                if exchange.key == "bithumb-spot":
                    return {"KRW": {"free": 1_000_000.0}}
                return {"ACS": {"free": 1_000_000.0}}

        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            FakeManager(),  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )

        self.assertEqual(payload["status"], "blocked_by_balance")
        self.assertFalse(payload["balance_guard"]["approved"])
        self.assertIn("required plus reserve", payload["balance_guard"]["reasons"][0])

    async def test_imbalanced_fill_halts_without_advancing_progress(self) -> None:
        cfg = make_config(
            rebalance=CrossExchangeRebalanceConfig(
                enabled=True,
                live_enabled=True,
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
                total_quote_common=100.0,
                quote_per_cycle_common=10.0,
                max_cost_bps=500.0,
                max_slippage_bps=100.0,
                order_ttl_seconds=0.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                strategy_enabled={"cross_exchange_rebalance": True},
                max_order_quote=100.0,
                max_cycle_quote=250.0,
                max_slippage_bps=100.0,
            ),
        )

        class FakeManager:
            async def fetch_open_orders(self, *_: object, **__: object) -> list[object]:
                return []

            async def fetch_closed_orders(
                self, *_: object, **__: object
            ) -> list[object]:
                return []

            async def fetch_balance(
                self,
                exchange: ExchangeConfig,
            ) -> dict[str, dict[str, float]]:
                if exchange.key == "bithumb-spot":
                    return {"KRW": {"free": 1_000_000.0}}
                return {"ACS": {"free": 1_000_000.0}}

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
                side: str,
                prepared: dict[str, object],
                **__: object,
            ) -> dict[str, object]:
                amount = float(prepared["amount"])
                price = float(prepared["price"])
                filled = amount if side == "buy" else 0.0
                return {
                    "id": f"{side}-order",
                    "exchange": exchange.key,
                    "side": side,
                    "filled": filled,
                    "cost": filled * price,
                    "average": price,
                    "status": "closed" if filled else "open",
                }

        payload = await run_cross_exchange_rebalance_cycle(
            cfg,
            FakeManager(),  # type: ignore[arg-type]
            books=make_books(),
            quote_rates={"KRW": 0.00075, "USDC": 1.0},
            live=True,
        )
        runtime = apply_rebalance_cycle_to_runtime(
            new_rebalance_runtime(
                cfg.cross_exchange_rebalance,
                common_quote_currency="USD",
            ),
            payload,
            cfg.cross_exchange_rebalance,
        )

        self.assertEqual(payload["status"], "hedge_required")
        self.assertTrue(payload["halt_required"])
        self.assertEqual(payload["execution_progress"]["progress_quote_common"], 0.0)
        self.assertTrue(runtime["halted"])
        self.assertEqual(runtime["status"], "halted")
        self.assertEqual(runtime["completed_quote_common"], 0.0)

    def test_runtime_progress_persists_and_route_change_resets(self) -> None:
        cfg = make_config().cross_exchange_rebalance
        runtime = new_rebalance_runtime(cfg, common_quote_currency="USD")
        runtime = apply_rebalance_cycle_to_runtime(
            runtime,
            {
                "mode": "live",
                "status": "progress",
                "execution_progress": {
                    "progress_quote_common": 10.0,
                    "matched_base": 80_000.0,
                },
            },
            cfg,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            save_rebalance_runtime(path, runtime)
            restored = load_rebalance_runtime(
                path,
                cfg,
                common_quote_currency="USD",
            )
            changed = load_rebalance_runtime(
                path,
                CrossExchangeRebalanceConfig(
                    **{
                        **cfg.__dict__,
                        "sell_exchange": "upbit-spot",
                    }
                ),
                common_quote_currency="USD",
            )

        self.assertEqual(restored["completed_quote_common"], 10.0)
        self.assertEqual(restored["completed_base"], 80_000.0)
        self.assertEqual(changed["completed_quote_common"], 0.0)

    def test_legacy_destination_progress_runtime_is_reset(self) -> None:
        cfg = make_config().cross_exchange_rebalance
        legacy = new_rebalance_runtime(cfg, common_quote_currency="USD")
        legacy.update(
            {
                "version": 1,
                "completed_quote_common": 50.0,
                "completed_base": 400_000.0,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            save_rebalance_runtime(path, legacy)
            restored = load_rebalance_runtime(
                path,
                cfg,
                common_quote_currency="USD",
            )

        self.assertEqual(restored["version"], 2)
        self.assertEqual(restored["completed_quote_common"], 0.0)
        self.assertEqual(restored["completed_base"], 0.0)


if __name__ == "__main__":
    unittest.main()
