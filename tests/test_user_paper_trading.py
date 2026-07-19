from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.user_paper_engine import (
    UserPaperTradingService,
    simulate_user_paper_cycle,
    strategy_paper_fingerprint,
    user_paper_trading_task_loop,
)
from arbitrage_bot.user_paper_store import (
    UserPaperStateConflict,
    UserPaperTradingStore,
)
from arbitrage_bot.user_strategies import UserStrategy
from arbitrage_bot.user_workspace import (
    UserExchangeAccount,
    UserProject,
    UserRiskProfile,
    UserWorkspaceStore,
)


MASTER_KEY = base64.urlsafe_b64encode(b"p" * 32).decode("ascii").rstrip("=")


def paper_book(
    account_id: str,
    symbol: str,
    bid: float,
    ask: float,
    *,
    now: float,
    bid_amount: float = 1_000.0,
    ask_amount: float = 1_000.0,
    extra_asks: list[BookLevel] | None = None,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange=f"workspace:paper:{account_id}",
        symbol=symbol,
        bids=[BookLevel(price=bid, amount=bid_amount)],
        asks=[BookLevel(price=ask, amount=ask_amount), *(extra_asks or [])],
        timestamp_ms=int(now * 1000),
        received_at=now,
    )


def project(
    *,
    project_id: str = "project-acs",
    owner: str = "trader@example.com",
    quote: str = "USDC",
) -> UserProject:
    return UserProject.from_dict(
        {
            "id": project_id,
            "owner_email": owner,
            "asset": "ACS",
            "quote_currency": quote,
            "status": "active",
        }
    )


def account(
    project_row: UserProject,
    *,
    account_id: str,
    exchange: str,
    symbol: str,
    market_type: str = "spot",
) -> UserExchangeAccount:
    return UserExchangeAccount.from_dict(
        {
            "id": account_id,
            "owner_email": project_row.owner_email,
            "project_id": project_row.id,
            "label": account_id,
            "exchange": exchange,
            "market_type": market_type,
            "symbol": symbol,
            "enabled": True,
            "withdrawal_disabled_confirmed": True,
            "connection_status": "healthy",
            "connection_checked_at": time.time(),
        }
    )


class FakePaperManager:
    instances: list["FakePaperManager"] = []

    def __init__(self, *, credentials_by_key=None) -> None:
        self.credentials_by_key = credentials_by_key
        self.fetch_count = 0
        self.closed = False
        self.instances.append(self)

    async def fetch_order_book(self, cfg, symbol, _depth):
        self.fetch_count += 1
        now = time.time()
        return paper_book(
            cfg.key.split(":")[-1],
            symbol,
            0.20,
            0.21,
            now=now,
        )

    async def close(self):
        self.closed = True


class UserPaperTradingTest(unittest.IsolatedAsyncioTestCase):
    def test_contract_arbitrage_scans_hyperliquid_dex_leg_without_live_fills(self) -> None:
        now = time.time()
        project_row = project()
        spot = account(
            project_row,
            account_id="coinbase-spot",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        derivative = account(
            project_row,
            account_id="hyperliquid-swap",
            exchange="hyperliquid",
            symbol="ACS/USDC:USDC",
            market_type="swap",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "contract_arbitrage",
                "account_ids": [spot.id, derivative.id],
                "parameters": {
                    "min_basis_bps": 50.0,
                    "min_funding_bps": 0.5,
                    "max_cycle_quote": 10.0,
                    "max_leverage": 1.0,
                    "scan_interval_seconds": 1.0,
                    "require_dex_leg": True,
                },
                "risk": {
                    "max_order_quote": 10.0,
                    "max_total_quote": 20.0,
                    "max_daily_loss_quote": 10.0,
                    "max_open_orders": 2,
                    "max_slippage_bps": 50.0,
                    "paper_fee_bps": 10.0,
                },
            }
        )
        books = {
            spot.id: paper_book(
                spot.id,
                spot.symbol,
                99.9,
                100.0,
                now=now,
            ),
            derivative.id: paper_book(
                derivative.id,
                derivative.symbol,
                102.0,
                102.1,
                now=now,
            ),
        }

        state, fills, event = simulate_user_paper_cycle(
            strategy,
            project_row,
            [spot, derivative],
            books,
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            funding_rates={derivative.id: 0.0001},
            now=now,
        )

        self.assertEqual(fills, [])
        self.assertEqual(state["status"], "candidate")
        self.assertFalse(state["live_submit_allowed"])
        self.assertEqual(
            state["contract_scan"]["best"]["derivative_venue_type"], "dex"
        )
        self.assertEqual(
            state["contract_scan"]["best"]["direction"], "positive_basis"
        )
        self.assertGreater(state["contract_scan"]["best"]["basis_bps"], 50.0)
        self.assertIsNotNone(event)
        self.assertEqual(event["event_type"], "candidate")

    def test_store_is_owner_scoped_idempotent_and_restart_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.sqlite3"
            store = UserPaperTradingStore(path)
            first_project = project()
            second_project = project(
                project_id="project-other",
                owner="other@example.com",
            )
            first = UserStrategy.from_dict(
                {
                    "id": "strategy-first",
                    "owner_email": first_project.owner_email,
                    "project_id": first_project.id,
                    "strategy_type": "market_maker",
                }
            )
            second = UserStrategy.from_dict(
                {
                    "id": "strategy-second",
                    "owner_email": second_project.owner_email,
                    "project_id": second_project.id,
                    "strategy_type": "market_maker",
                }
            )
            state = {
                "run_id": "run-first",
                "status": "running",
                "total_pnl_common": 1.25,
                "daily_pnl_common": 0.25,
                "fill_count": 1,
                "open_order_count": 2,
                "common_quote_currency": "USD",
            }
            fill = {
                "fill_id": "fill-first",
                "run_id": "run-first",
                "account_id": "coinbase-main",
                "exchange": "coinbase",
                "symbol": "ACS/USDC",
                "side": "buy",
                "price": 0.2,
                "amount": 5.0,
                "gross_quote": 1.0,
                "fee_quote": 0.001,
                "quote_currency": "USDC",
                "realized_pnl_common": 0.0,
                "filled_at": 100.0,
            }
            event = {
                "event_key": "event-first",
                "run_id": "run-first",
                "event_type": "fill",
                "status": "running",
                "reason": "paper fill",
                "created_at": 100.0,
            }
            store.persist_cycle(first, state, fills=[fill], event=event)
            store.persist_cycle(first, state, fills=[fill], event=event)
            store.persist_cycle(
                second,
                {
                    **state,
                    "run_id": "run-second",
                    "total_pnl_common": 99.0,
                },
            )

            first_payload = UserPaperTradingStore(path).public_payload(
                owner_email=first.owner_email,
                is_admin=False,
            )
            admin_payload = store.public_payload(
                owner_email=first.owner_email,
                is_admin=True,
            )
            counts = store.counts(first.id)
            reset_counts = store.reset_strategy(first)

            self.assertEqual(
                [row["strategy_id"] for row in first_payload["states"]], [first.id]
            )
            self.assertEqual(len(admin_payload["states"]), 2)
            self.assertEqual(counts["fill_count"], 1)
            self.assertEqual(counts["event_count"], 1)
            self.assertEqual(reset_counts, counts)
            self.assertIsNone(store.get_state(first.id))
            self.assertEqual(store.counts(first.id)["event_count"], 1)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_store_rejects_a_stale_cycle_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserPaperTradingStore(Path(tmp) / "paper.sqlite3")
            project_row = project()
            strategy = UserStrategy.from_dict(
                {
                    "id": "strategy-concurrent",
                    "owner_email": project_row.owner_email,
                    "project_id": project_row.id,
                    "strategy_type": "market_maker",
                }
            )
            store.persist_cycle(
                strategy,
                {
                    "run_id": "run-concurrent",
                    "status": "running",
                    "updated_at": 100.0,
                },
                expected_state_updated_at=None,
            )
            store.persist_cycle(
                strategy,
                {
                    "run_id": "run-concurrent",
                    "status": "running",
                    "updated_at": 101.0,
                },
                expected_state_updated_at=100.0,
            )
            stale_fill = {
                "fill_id": "stale-fill",
                "account_id": "coinbase-main",
                "exchange": "coinbase",
                "symbol": "ACS/USDC",
                "side": "buy",
                "price": 0.2,
                "amount": 5.0,
                "gross_quote": 1.0,
                "fee_quote": 0.001,
                "quote_currency": "USDC",
                "filled_at": 102.0,
            }
            stale_event = {
                "event_key": "stale-event",
                "event_type": "fill",
                "status": "running",
                "reason": "must roll back",
                "created_at": 102.0,
            }

            with self.assertRaises(UserPaperStateConflict):
                store.persist_cycle(
                    strategy,
                    {
                        "run_id": "run-concurrent",
                        "status": "running",
                        "updated_at": 102.0,
                    },
                    fills=[stale_fill],
                    event=stale_event,
                    expected_state_updated_at=100.0,
                )

            self.assertEqual(store.get_state(strategy.id)["updated_at"], 101.0)
            self.assertEqual(
                store.counts(strategy.id),
                {"state_count": 1, "fill_count": 0, "event_count": 0},
            )

    def test_auto_buy_checks_start_stop_and_completes_with_real_depth(self) -> None:
        now = time.time()
        project_row = project()
        account_row = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "auto_buy_sell",
                "account_ids": [account_row.id],
                "parameters": {
                    "side": "buy",
                    "total_quote": 10.0,
                    "quote_per_order": 5.0,
                    "interval_seconds": 1.0,
                    "start_price": 0.215,
                    "stop_price": 0.23,
                },
                "risk": {
                    "max_order_quote": 5.0,
                    "max_total_quote": 10.0,
                    "paper_fee_bps": 10.0,
                },
            }
        )
        book = paper_book(account_row.id, account_row.symbol, 0.20, 0.21, now=now)

        first_state, first_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now,
        )
        second_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.20,
            0.21,
            now=now + 1,
        )
        second_state, second_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: second_book},
            first_state,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 1,
        )

        self.assertEqual(len(first_fills), 1)
        self.assertEqual(len(second_fills), 1)
        self.assertEqual(second_state["status"], "complete")
        self.assertTrue(second_state["terminal"])
        self.assertAlmostEqual(second_state["strategy_filled_quote"], 10.0)
        self.assertEqual(second_state["progress_pct"], 100.0)
        self.assertGreater(second_state["fees_common"], 0.0)

        stop_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.229,
            0.23,
            now=now + 2,
        )
        stopped, fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: stop_book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 2,
        )
        self.assertEqual(stopped["status"], "stopped_by_price")
        self.assertTrue(stopped["terminal"])
        self.assertEqual(fills, [])

    def test_existing_wallet_uses_the_latest_quote_conversion_rate(self) -> None:
        now = time.time()
        project_row = project(quote="KRW")
        account_row = account(
            project_row,
            account_id="bithumb-main",
            exchange="bithumb",
            symbol="ACS/KRW",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "auto_buy_sell",
                "account_ids": [account_row.id],
                "parameters": {
                    "side": "buy",
                    "total_quote": 50.0,
                    "quote_per_order": 5.0,
                    "interval_seconds": 1.0,
                    "start_price": 0.1,
                },
                "risk": {
                    "max_order_quote": 5.0,
                    "max_total_quote": 50.0,
                    "paper_fee_bps": 0.0,
                },
            }
        )
        book = paper_book(
            account_row.id,
            account_row.symbol,
            0.20,
            0.21,
            now=now,
        )
        first_state, _, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: book},
            None,
            quote_rates={"KRW": 0.001},
            common_quote_currency="USD",
            now=now,
        )
        second_state, _, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: book},
            first_state,
            quote_rates={"KRW": 0.002},
            common_quote_currency="USD",
            now=now + 1,
        )

        self.assertEqual(second_state["wallets"][account_row.id]["quote_rate"], 0.002)
        self.assertAlmostEqual(second_state["equity_common"], 0.1)
        self.assertAlmostEqual(second_state["total_pnl_common"], 0.05)

    def test_dca_waits_for_trigger_and_stops_at_take_profit(self) -> None:
        now = time.time()
        project_row = project()
        account_row = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "dca",
                "account_ids": [account_row.id],
                "parameters": {
                    "side": "buy",
                    "total_quote": 10.0,
                    "quote_per_order": 5.0,
                    "interval_seconds": 1.0,
                    "trigger_price": 0.20,
                    "take_profit_pct": 5.0,
                },
                "risk": {
                    "max_order_quote": 5.0,
                    "max_total_quote": 10.0,
                    "paper_fee_bps": 0.0,
                },
            }
        )
        waiting_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.20,
            0.21,
            now=now,
        )
        waiting_state, waiting_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: waiting_book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now,
        )
        trigger_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.19,
            0.20,
            now=now + 1,
        )
        filled_state, fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: trigger_book},
            waiting_state,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 1,
        )
        profit_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.211,
            0.212,
            now=now + 2,
        )
        complete_state, complete_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: profit_book},
            filled_state,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 2,
        )

        self.assertEqual(waiting_state["status"], "waiting")
        self.assertEqual(waiting_fills, [])
        self.assertEqual(len(fills), 1)
        self.assertEqual(filled_state["status"], "running")
        self.assertEqual(complete_state["status"], "complete")
        self.assertTrue(complete_state["terminal"])
        self.assertEqual(complete_fills, [])

    def test_market_maker_only_fills_previous_crossed_orders(self) -> None:
        now = time.time()
        project_row = project()
        account_row = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "market_maker",
                "account_ids": [account_row.id],
                "parameters": {
                    "levels": 1,
                    "price_band_pct": 1.0,
                    "quote_per_level": 10.0,
                    "refresh_seconds": 1.0,
                    "post_only": True,
                },
                "risk": {
                    "max_order_quote": 10.0,
                    "max_total_quote": 20.0,
                    "max_open_orders": 2,
                    "paper_fee_bps": 10.0,
                },
            }
        )
        first_book = paper_book(
            account_row.id, account_row.symbol, 99.0, 101.0, now=now
        )
        first_state, first_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: first_book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now,
        )
        crossed_book = paper_book(
            account_row.id,
            account_row.symbol,
            98.0,
            98.5,
            now=now + 1,
        )
        second_state, second_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: crossed_book},
            first_state,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 1,
        )

        self.assertEqual(first_fills, [])
        self.assertEqual(first_state["open_order_count"], 2)
        self.assertEqual(len(second_fills), 1)
        self.assertEqual(second_fills[0]["side"], "buy")
        self.assertEqual(second_fills[0]["fill_kind"], "market_maker")
        self.assertEqual(second_state["status"], "orders_active")
        self.assertEqual(second_state["open_order_count"], 2)

    def test_daily_loss_terminal_state_clears_virtual_orders(self) -> None:
        now = time.time()
        project_row = project()
        account_row = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "market_maker",
                "account_ids": [account_row.id],
                "parameters": {
                    "levels": 1,
                    "price_band_pct": 1.0,
                    "quote_per_level": 10.0,
                    "refresh_seconds": 1.0,
                    "post_only": True,
                },
                "risk": {
                    "max_order_quote": 10.0,
                    "max_total_quote": 20.0,
                    "max_daily_loss_quote": 0.01,
                    "max_open_orders": 2,
                },
            }
        )
        initial_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.20,
            0.21,
            now=now,
        )
        first_state, _, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: initial_book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now,
        )
        lower_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.10,
            0.11,
            now=now + 1,
        )
        stopped_state, fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: lower_book},
            first_state,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 1,
        )

        self.assertGreater(len(first_state["open_orders"]), 0)
        self.assertEqual(stopped_state["status"], "blocked_daily_loss")
        self.assertTrue(stopped_state["terminal"])
        self.assertEqual(stopped_state["open_orders"], [])
        self.assertEqual(stopped_state["open_order_count"], 0)
        self.assertEqual(fills, [])

    def test_market_order_is_blocked_when_visible_depth_exceeds_slippage_limit(
        self,
    ) -> None:
        now = time.time()
        project_row = project()
        account_row = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "auto_buy_sell",
                "account_ids": [account_row.id],
                "parameters": {
                    "side": "buy",
                    "total_quote": 5.0,
                    "quote_per_order": 5.0,
                    "interval_seconds": 1.0,
                },
                "risk": {
                    "max_order_quote": 5.0,
                    "max_total_quote": 5.0,
                    "max_slippage_bps": 5.0,
                    "paper_fee_bps": 10.0,
                },
            }
        )
        thin_book = paper_book(
            account_row.id,
            account_row.symbol,
            0.99,
            1.0,
            now=now,
            ask_amount=1.0,
            extra_asks=[BookLevel(price=2.0, amount=10.0)],
        )
        state, fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: thin_book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now,
        )

        self.assertEqual(state["status"], "blocked_slippage")
        self.assertGreater(state["last_slippage_bps"], 5.0)
        self.assertEqual(fills, [])

    def test_grid_rebuilds_after_crossed_fill(self) -> None:
        now = time.time()
        project_row = project()
        account_row = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "spot_grid",
                "account_ids": [account_row.id],
                "parameters": {
                    "lower_price": 90.0,
                    "upper_price": 110.0,
                    "grid_count": 2,
                    "quote_per_grid": 5.0,
                    "spacing": "arithmetic",
                    "refresh_seconds": 1.0,
                },
                "risk": {
                    "max_order_quote": 5.0,
                    "max_total_quote": 15.0,
                    "max_open_orders": 3,
                    "paper_fee_bps": 10.0,
                },
            }
        )
        first_book = paper_book(
            account_row.id, account_row.symbol, 99.0, 101.0, now=now
        )
        first_state, _, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: first_book},
            None,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now,
        )
        crossed_book = paper_book(
            account_row.id,
            account_row.symbol,
            88.0,
            89.0,
            now=now + 1,
        )
        second_state, fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [account_row],
            {account_row.id: crossed_book},
            first_state,
            quote_rates={"USDC": 1.0},
            common_quote_currency="USD",
            now=now + 1,
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fill_kind"], "spot_grid")
        self.assertEqual(second_state["status"], "waiting")
        self.assertEqual(second_state["open_order_count"], 0)

    def test_spot_spread_is_atomic_when_second_wallet_cannot_fill(self) -> None:
        now = time.time()
        project_row = project()
        buy_account = account(
            project_row,
            account_id="coinbase-main",
            exchange="coinbase",
            symbol="ACS/USDC",
        )
        sell_account = account(
            project_row,
            account_id="bybit-main",
            exchange="bybit",
            symbol="ACS/USDT",
        )
        strategy = UserStrategy.from_dict(
            {
                "owner_email": project_row.owner_email,
                "project_id": project_row.id,
                "strategy_type": "spot_spread",
                "account_ids": [buy_account.id, sell_account.id],
                "parameters": {
                    "min_profit_bps": 10.0,
                    "max_cycle_quote": 10.0,
                    "scan_interval_seconds": 1.0,
                },
                "risk": {
                    "max_order_quote": 10.0,
                    "max_total_quote": 50.0,
                    "max_daily_loss_quote": 1_000.0,
                    "paper_fee_bps": 10.0,
                },
            }
        )
        books = {
            buy_account.id: paper_book(
                buy_account.id,
                buy_account.symbol,
                99.0,
                100.0,
                now=now,
            ),
            sell_account.id: paper_book(
                sell_account.id,
                sell_account.symbol,
                102.0,
                103.0,
                now=now,
            ),
        }
        first_state, fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [buy_account, sell_account],
            books,
            None,
            quote_rates={"USDC": 1.0, "USDT": 1.0},
            common_quote_currency="USD",
            now=now,
        )
        self.assertEqual(len(fills), 2)
        self.assertEqual({fill["side"] for fill in fills}, {"buy", "sell"})
        self.assertEqual(first_state["status"], "running")

        empty_sell_state = {**first_state, "terminal": False}
        empty_sell_state["wallets"] = copy_wallets = {
            key: dict(value) for key, value in first_state["wallets"].items()
        }
        copy_wallets[sell_account.id]["base_balance"] = 0.0
        copy_wallets[sell_account.id]["base_cost_quote"] = 0.0
        buy_quote_before = copy_wallets[buy_account.id]["quote_balance"]
        blocked, blocked_fills, _ = simulate_user_paper_cycle(
            strategy,
            project_row,
            [buy_account, sell_account],
            {
                buy_account.id: paper_book(
                    buy_account.id,
                    buy_account.symbol,
                    99.0,
                    100.0,
                    now=now + 1,
                ),
                sell_account.id: paper_book(
                    sell_account.id,
                    sell_account.symbol,
                    102.0,
                    103.0,
                    now=now + 1,
                ),
            },
            empty_sell_state,
            quote_rates={"USDC": 1.0, "USDT": 1.0},
            common_quote_currency="USD",
            now=now + 1,
        )
        self.assertEqual(blocked["status"], "blocked_balance")
        self.assertEqual(blocked_fills, [])
        self.assertEqual(
            blocked["wallets"][buy_account.id]["quote_balance"],
            buy_quote_before,
        )

    async def test_service_uses_public_books_once_and_recovers_persisted_state(
        self,
    ) -> None:
        FakePaperManager.instances.clear()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_PAPER_MASTER_KEY": MASTER_KEY},
            ),
        ):
            root = Path(tmp)
            workspace = UserWorkspaceStore(
                root / "workspace.sqlite3",
                master_key_env="TEST_PAPER_MASTER_KEY",
            )
            project_row = workspace.upsert_project(project())
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "coinbase-main",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "private-key", "secret": "private-secret"},
            )
            saved = workspace.update_account_connection(saved.id, status="healthy")
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict({**saved.to_dict(), "enabled": True})
            )
            strategy = workspace.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "paper-auto",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "strategy_type": "auto_buy_sell",
                        "account_ids": [saved.id],
                        "enabled": True,
                        "parameters": {
                            "side": "buy",
                            "total_quote": 10.0,
                            "quote_per_order": 5.0,
                            "interval_seconds": 1.0,
                        },
                        "risk": {
                            "max_order_quote": 5.0,
                            "max_total_quote": 10.0,
                            "paper_fee_bps": 10.0,
                        },
                    }
                )
            )
            paper_path = root / "paper.sqlite3"
            first_service = UserPaperTradingService(
                workspace,
                UserPaperTradingStore(paper_path),
                quote_rates={"USDC": 1.0},
                common_quote_currency="USD",
                manager_factory=FakePaperManager,
            )
            first_result = await first_service.run_once()
            await first_service.close()

            restored_store = UserPaperTradingStore(paper_path)
            first_state = restored_store.get_state(strategy.id)
            second_service = UserPaperTradingService(
                workspace,
                restored_store,
                quote_rates={"USDC": 1.0},
                common_quote_currency="USD",
                manager_factory=FakePaperManager,
            )
            second_result = await second_service.run_once(
                now=float(first_state["next_due_at"]),
            )
            await second_service.close()
            final_state = restored_store.get_state(strategy.id)
            paper_bytes = paper_path.read_bytes()

        self.assertEqual(first_result["fetched"], 1)
        self.assertEqual(second_result["fetched"], 1)
        self.assertEqual(final_state["status"], "complete")
        self.assertEqual(final_state["fill_count"], 2)
        self.assertIsNone(FakePaperManager.instances[0].credentials_by_key)
        self.assertTrue(all(instance.closed for instance in FakePaperManager.instances))
        self.assertNotIn(b"private-key", paper_bytes)
        self.assertNotIn(b"private-secret", paper_bytes)

    async def test_many_strategies_share_one_public_order_book_fetch(self) -> None:
        FakePaperManager.instances.clear()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_PAPER_MASTER_KEY": MASTER_KEY},
            ),
        ):
            root = Path(tmp)
            workspace = UserWorkspaceStore(
                root / "workspace.sqlite3",
                master_key_env="TEST_PAPER_MASTER_KEY",
            )
            project_row = workspace.upsert_project(project())
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "coinbase-main",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            saved = workspace.update_account_connection(saved.id, status="healthy")
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict({**saved.to_dict(), "enabled": True})
            )
            secondary = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "coinbase-secondary",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key-2", "secret": "secret-2"},
            )
            secondary = workspace.update_account_connection(
                secondary.id,
                status="healthy",
            )
            secondary = workspace.upsert_account(
                UserExchangeAccount.from_dict({**secondary.to_dict(), "enabled": True})
            )
            for index in range(25):
                workspace.upsert_strategy(
                    UserStrategy.from_dict(
                        {
                            "id": f"paper-auto-{index:02d}",
                            "owner_email": project_row.owner_email,
                            "project_id": project_row.id,
                            "strategy_type": "auto_buy_sell",
                            "account_ids": [saved.id if index % 2 else secondary.id],
                            "enabled": True,
                            "parameters": {
                                "side": "buy",
                                "total_quote": 1.0,
                                "quote_per_order": 1.0,
                                "interval_seconds": 1.0,
                            },
                        }
                    )
                )
            paper_store = UserPaperTradingStore(root / "paper.sqlite3")
            service = UserPaperTradingService(
                workspace,
                paper_store,
                quote_rates={"USDC": 1.0},
                common_quote_currency="USD",
                manager_factory=FakePaperManager,
            )
            result = await service.run_once()
            payload = paper_store.public_payload(
                owner_email=project_row.owner_email,
                is_admin=False,
            )
            manager = FakePaperManager.instances[-1]
            await service.close()

        self.assertEqual(result["processed"], 25)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(manager.fetch_count, 1)
        self.assertEqual(len(payload["states"]), 25)
        self.assertEqual(payload["summary"]["fill_count"], 25)

    async def test_user_risk_switch_blocks_all_owner_strategies(self) -> None:
        FakePaperManager.instances.clear()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_PAPER_MASTER_KEY": MASTER_KEY},
            ),
        ):
            root = Path(tmp)
            workspace = UserWorkspaceStore(
                root / "workspace.sqlite3",
                master_key_env="TEST_PAPER_MASTER_KEY",
            )
            project_row = workspace.upsert_project(project())
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "coinbase-risk-off",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            saved = workspace.update_account_connection(saved.id, status="healthy")
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict({**saved.to_dict(), "enabled": True})
            )
            strategy = workspace.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "paper-mm-risk-off",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "strategy_type": "market_maker",
                        "account_ids": [saved.id],
                        "enabled": True,
                    }
                )
            )
            workspace.upsert_risk_profile(
                UserRiskProfile(
                    owner_email=project_row.owner_email,
                    trading_enabled=False,
                )
            )
            paper_store = UserPaperTradingStore(root / "paper.sqlite3")
            service = UserPaperTradingService(
                workspace,
                paper_store,
                quote_rates={"USDC": 1.0},
                common_quote_currency="USD",
                manager_factory=FakePaperManager,
            )

            result = await service.run_once()
            blocked = paper_store.get_state(strategy.id)
            await service.close()

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(blocked["status"], "blocked_user_risk")
        self.assertIn("switch is disabled", blocked["reason"])

    async def test_global_pause_clears_virtual_orders_and_is_idempotent(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_PAPER_MASTER_KEY": MASTER_KEY},
            ),
        ):
            root = Path(tmp)
            workspace = UserWorkspaceStore(
                root / "workspace.sqlite3",
                master_key_env="TEST_PAPER_MASTER_KEY",
            )
            project_row = workspace.upsert_project(project())
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "coinbase-main",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            saved = workspace.update_account_connection(saved.id, status="healthy")
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict({**saved.to_dict(), "enabled": True})
            )
            strategy = workspace.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "paper-mm-pause",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "strategy_type": "market_maker",
                        "account_ids": [saved.id],
                        "enabled": True,
                    }
                )
            )
            paper_store = UserPaperTradingStore(root / "paper.sqlite3")
            paper_store.persist_cycle(
                strategy,
                {
                    "run_id": "run-pause",
                    "status": "orders_active",
                    "updated_at": 100.0,
                    "open_orders": [
                        {
                            "paper_order_id": "order-before-pause",
                            "account_id": saved.id,
                        }
                    ],
                    "open_order_count": 1,
                },
            )
            service = UserPaperTradingService(
                workspace,
                paper_store,
                quote_rates={"USDC": 1.0},
                common_quote_currency="USD",
                manager_factory=FakePaperManager,
            )

            first_result = await service.pause_all(now=101.0)
            second_result = await service.pause_all(now=102.0)
            paused_state = paper_store.get_state(strategy.id)
            await service.close()

        self.assertEqual(first_result, {"processed": 1, "conflicts": 0})
        self.assertEqual(second_result, {"processed": 0, "conflicts": 0})
        self.assertEqual(paused_state["status"], "program_paused")
        self.assertEqual(paused_state["open_orders"], [])
        self.assertEqual(paused_state["open_order_count"], 0)
        self.assertEqual(paused_state["next_due_at"], 101.0)

    async def test_terminal_state_survives_strategy_pause_and_resume(self) -> None:
        FakePaperManager.instances.clear()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_PAPER_MASTER_KEY": MASTER_KEY},
            ),
        ):
            root = Path(tmp)
            workspace = UserWorkspaceStore(
                root / "workspace.sqlite3",
                master_key_env="TEST_PAPER_MASTER_KEY",
            )
            project_row = workspace.upsert_project(project())
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "coinbase-main",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "withdrawal_disabled_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            saved = workspace.update_account_connection(saved.id, status="healthy")
            saved = workspace.upsert_account(
                UserExchangeAccount.from_dict({**saved.to_dict(), "enabled": True})
            )
            strategy = workspace.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "paper-auto-complete",
                        "owner_email": project_row.owner_email,
                        "project_id": project_row.id,
                        "strategy_type": "auto_buy_sell",
                        "account_ids": [saved.id],
                        "enabled": True,
                    }
                )
            )
            paper_store = UserPaperTradingStore(root / "paper.sqlite3")
            paper_store.persist_cycle(
                strategy,
                {
                    "run_id": "run-complete",
                    "status": "complete",
                    "terminal": True,
                    "config_fingerprint": strategy_paper_fingerprint(
                        strategy,
                        project_row,
                        [saved],
                    ),
                    "strategy_updated_at": strategy.updated_at,
                    "updated_at": 100.0,
                },
            )
            disabled = workspace.upsert_strategy(
                UserStrategy.from_dict({**strategy.to_dict(), "enabled": False})
            )
            service = UserPaperTradingService(
                workspace,
                paper_store,
                quote_rates={"USDC": 1.0},
                common_quote_currency="USD",
                manager_factory=FakePaperManager,
            )
            disabled_result = await service.run_once(now=101.0)
            workspace.upsert_strategy(
                UserStrategy.from_dict({**disabled.to_dict(), "enabled": True})
            )
            resumed_result = await service.run_once(now=102.0)
            final_state = paper_store.get_state(strategy.id)
            manager = FakePaperManager.instances[-1]
            await service.close()

        self.assertEqual(disabled_result["processed"], 0)
        self.assertEqual(resumed_result["processed"], 0)
        self.assertEqual(manager.fetch_count, 0)
        self.assertEqual(final_state["status"], "complete")
        self.assertTrue(final_state["terminal"])

    async def test_background_loop_honors_the_global_program_pause(self) -> None:
        class LoopService:
            def __init__(self) -> None:
                self.run_count = 0
                self.rate_updates = 0
                self.pause_count = 0

            async def run_once(self) -> None:
                self.run_count += 1

            def update_quote_rates(self, _rates) -> None:
                self.rate_updates += 1

            async def pause_all(self) -> None:
                self.pause_count += 1

        service = LoopService()
        check_count = 0

        async def program_is_running() -> bool:
            nonlocal check_count
            check_count += 1
            return False

        async def quote_rates() -> dict[str, float]:
            raise AssertionError("quote rates must not refresh while globally paused")

        task = asyncio.create_task(
            user_paper_trading_task_loop(
                service,
                scan_seconds=0.01,
                running_check=program_is_running,
                quote_rates_provider=quote_rates,
            )
        )
        await asyncio.sleep(0.13)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertGreaterEqual(check_count, 1)
        self.assertEqual(service.run_count, 0)
        self.assertEqual(service.rate_updates, 0)
        self.assertGreaterEqual(service.pause_count, 1)


if __name__ == "__main__":
    unittest.main()
