from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from arbitrage_bot.config import (
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
)
from arbitrage_bot.grid_trading import GridFill
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.spot_grid_executor import (
    TrackedGridOrder,
    load_runtime_state,
    run_cycle,
    runtime_state_from_tracked,
    save_runtime_state,
    sync_tracked_grid_orders,
    tracked_orders_from_state,
)


def book(bid: float = 99.0, ask: float = 101.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange="coinbase-test",
        symbol="ACS/USDC",
        bids=[
            BookLevel(price=bid, amount=1000.0),
            BookLevel(price=bid - 1.0, amount=1000.0),
        ],
        asks=[
            BookLevel(price=ask, amount=1000.0),
            BookLevel(price=ask + 1.0, amount=1000.0),
        ],
    )


def cfg(
    *,
    live_enabled: bool = True,
    allow_live_trading: bool = True,
    max_open_orders: int = 20,
) -> BotConfig:
    exchange = ExchangeConfig(
        id="coinbase",
        label="coinbase-test",
        market_type="spot",
    )
    return BotConfig(
        poll_seconds=1.0,
        order_book_depth=10,
        notional_quote=10.0,
        min_profit_quote=0.0,
        min_profit_bps=0.0,
        min_basis_bps=0.0,
        common_quote_currency="USDC",
        quote_rates={},
        quote_rate_sources=[],
        onchain_monitor=OnchainMonitorConfig(),
        market_maker=MarketMakerConfig(),
        slow_execution=SlowExecutionConfig(),
        portfolio=PortfolioConfig(),
        spot_symbols=[],
        spot_markets=[],
        cash_and_carry_pairs=[],
        spot_exchanges=[exchange],
        derivative_exchanges=[],
        spot_grid=SpotGridConfig(
            enabled=True,
            live_enabled=live_enabled,
            exchange="coinbase-test",
            symbol="ACS/USDC",
            lower_price=90.0,
            upper_price=110.0,
            grid_count=4,
            quote_per_grid=10.0,
            max_open_orders=max_open_orders,
            min_grid_step_bps=1.0,
            post_only=True,
            client_order_prefix="test-grid",
        ),
        risk=RiskConfig(
            enabled=True,
            trading_enabled=True,
            allow_live_trading=allow_live_trading,
            require_post_only=True,
            max_order_quote=25.0,
            max_cycle_quote=100.0,
            max_open_orders=50,
            max_cancels_per_cycle=50,
            max_slippage_bps=10_000.0,
            max_price_distance_bps=10_000.0,
            max_existing_spread_bps=10_000.0,
            max_order_book_gap_bps=10_000.0,
            max_price_jump_bps=0.0,
            max_order_book_age_seconds=60.0,
            strategy_enabled={"spot_grid": True},
            account_enabled={"coinbase-test": True},
        ),
    )


class FakeGridManager:
    def __init__(
        self,
        *,
        open_orders: list[dict] | None = None,
        closed_orders: list[dict] | None = None,
    ) -> None:
        self.open_orders = list(open_orders or [])
        self.closed_orders = list(closed_orders or [])
        self.created: list[dict] = []
        self.canceled: list[str] = []
        self.fetch_open_count = 0

    async def fetch_order_book(
        self,
        exchange: ExchangeConfig,
        symbol: str,
        depth: int,
    ) -> OrderBookSnapshot:
        return book()

    async def fetch_open_orders(
        self,
        exchange: ExchangeConfig,
        *,
        symbol: str,
    ) -> list[dict]:
        self.fetch_open_count += 1
        return list(self.open_orders)

    async def fetch_closed_orders(
        self,
        exchange: ExchangeConfig,
        *,
        symbol: str,
        limit: int = 20,
    ) -> list[dict]:
        return list(self.closed_orders)

    async def prepare_limit_order(
        self,
        exchange: ExchangeConfig,
        *,
        symbol: str,
        side: str,
        amount: float,
        price: float,
    ) -> dict:
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

    async def create_limit_order(
        self,
        exchange: ExchangeConfig,
        *,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        post_only: bool = True,
        client_order_id: str | None = None,
    ) -> dict:
        order_id = f"grid-{len(self.created) + 1}"
        raw = {
            "id": order_id,
            "clientOrderId": client_order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
        }
        self.created.append(raw)
        return raw

    async def cancel_order(
        self,
        exchange: ExchangeConfig,
        *,
        symbol: str,
        order_id: str,
    ) -> dict:
        self.canceled.append(order_id)
        return {"id": order_id, "status": "canceled"}


class SpotGridExecutorTest(unittest.TestCase):
    def test_cycle_is_blocked_when_live_trading_is_not_allowed(self) -> None:
        manager = FakeGridManager()

        payload = asyncio.run(
            run_cycle(cfg(allow_live_trading=False), manager, live=True)
        )

        self.assertEqual(payload["status"], "blocked_by_risk")
        self.assertFalse(payload["risk"]["approved"])
        self.assertIn(
            "risk.allow_live_trading is false",
            payload["risk"]["reasons"],
        )
        self.assertEqual(manager.created, [])

    def test_live_cycle_places_initial_grid(self) -> None:
        manager = FakeGridManager()

        payload = asyncio.run(run_cycle(cfg(max_open_orders=2), manager, live=True))

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(payload["execution"]["placed_count"], 2)
        self.assertEqual([row["side"] for row in manager.created], ["buy", "sell"])
        self.assertEqual(
            payload["execution"]["placed_order_ids"],
            ["grid-1", "grid-2"],
        )

    def test_live_cycle_does_not_rebuild_until_cancel_is_confirmed(self) -> None:
        manager = FakeGridManager(open_orders=[{"id": "old-grid-1"}])

        payload = asyncio.run(
            run_cycle(
                cfg(max_open_orders=2),
                manager,
                live=True,
                replace_order_ids=["old-grid-1"],
            )
        )

        self.assertEqual(payload["status"], "cancel_retry")
        self.assertEqual(manager.canceled, ["old-grid-1"])
        self.assertEqual(manager.created, [])
        self.assertEqual(
            payload["execution"]["remaining_open_order_ids"],
            ["old-grid-1"],
        )

    def test_confirmed_fill_places_adjacent_replacement_only(self) -> None:
        manager = FakeGridManager()

        payload = asyncio.run(
            run_cycle(
                cfg(max_open_orders=10),
                manager,
                live=True,
                replacement_fills=[
                    GridFill(
                        side="buy",
                        level=2,
                        price=95.0,
                        amount=0.1,
                        quote_notional=9.5,
                    )
                ],
            )
        )

        self.assertEqual(payload["status"], "placed")
        self.assertEqual(payload["action"], "replace_filled_orders")
        self.assertEqual(payload["execution"]["placed_count"], 1)
        self.assertEqual(manager.created[0]["side"], "sell")
        self.assertEqual(manager.created[0]["price"], 100.0)

    def test_runtime_state_roundtrip(self) -> None:
        tracked = [
            TrackedGridOrder(
                order_id="grid-1",
                client_order_id="client-1",
                side="buy",
                level=2,
                price=95.0,
                amount=1.0,
                quote_notional=95.0,
                exchange="coinbase-test",
                symbol="ACS/USDC",
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "spot_grid_runtime.json"
            save_runtime_state(
                path,
                runtime_state_from_tracked(
                    tracked,
                    exchange="coinbase-test",
                    symbol="ACS/USDC",
                    stats={"placed_count": 1},
                ),
            )
            loaded = load_runtime_state(path)

        restored = tracked_orders_from_state(loaded)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].order_id, "grid-1")
        self.assertEqual(loaded["stats"]["placed_count"], 1)

    def test_sync_tracks_confirmed_fills_and_missing_orders(self) -> None:
        tracked = [
            TrackedGridOrder(
                order_id="open-1",
                client_order_id="",
                side="buy",
                level=2,
                price=95.0,
                amount=1.0,
                quote_notional=95.0,
                exchange="coinbase-test",
                symbol="ACS/USDC",
            ),
            TrackedGridOrder(
                order_id="filled-1",
                client_order_id="",
                side="sell",
                level=4,
                price=105.0,
                amount=1.0,
                quote_notional=105.0,
                exchange="coinbase-test",
                symbol="ACS/USDC",
            ),
            TrackedGridOrder(
                order_id="missing-1",
                client_order_id="",
                side="buy",
                level=1,
                price=90.0,
                amount=1.0,
                quote_notional=90.0,
                exchange="coinbase-test",
                symbol="ACS/USDC",
            ),
        ]

        sync = sync_tracked_grid_orders(
            tracked,
            open_orders=[{"id": "open-1"}],
            closed_orders=[
                {
                    "id": "filled-1",
                    "status": "closed",
                    "filled": 1.0,
                    "cost": 105.0,
                }
            ],
        )

        self.assertEqual(sync["open_tracked_count"], 1)
        self.assertEqual(sync["confirmed_fill_count"], 1)
        self.assertEqual(sync["missing_unconfirmed_count"], 1)
        self.assertEqual(sync["confirmed_fills"][0]["side"], "sell")


if __name__ == "__main__":
    unittest.main()
