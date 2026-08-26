from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from eth_account import Account
from eth_account.messages import encode_defunct

from arbitrage_bot.config import (
    CashAndCarryPair,
    CrossExchangeRebalanceConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OptionComboConfig,
    OptionsArbitrageConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
    StrategyCenterConfig,
    TradeLogConfig,
    WebSecurityConfig,
)
from arbitrage_bot.auto_buy_sell_task import AutoBuySellTaskService
from arbitrage_bot.cross_exchange_rebalancer import (
    new_rebalance_runtime,
    save_rebalance_runtime,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.web import (
    APP_JS,
    HTML as INDEX_HTML,
    LOGIN_MAX_FAILURES,
    MonitorState,
    _preflight_candidate_from_payload,
    _require_user_owned_execution,
    _user_execution_preflight,
    _user_auto_buy_sell_payload,
    api_create_auto_buy_sell_task,
    api_control_auto_buy_sell_task,
    api_cancel_bulk_orders,
    api_cancel_order,
    api_cross_exchange_rebalance,
    api_market_maker,
    api_risk,
    api_slow_execution,
    api_strategy_center,
    build_market_maker_payload,
    cancel_bulk_orders_payload,
    cancel_order_payload,
    create_app,
    fetch_account_balances_payload,
    fetch_derivatives_risk_payload,
    fetch_funding_basis_payload,
    fetch_options_arbitrage_payload,
    fetch_order_activity_payload,
)
from arbitrage_bot.web.render_payloads import state_payload_for_view
from arbitrage_bot.web.strategy_preflight import (
    StrategyPreflightService,
    build_strategy_preflight,
)
from arbitrage_bot.web.loops import (
    _complete_market_maker_cycle_on_shutdown,
    _load_initial_rebalance_runtime,
)
from arbitrage_bot.web.users import WebUser, WebUserStore, totp_code
from arbitrage_bot.user_strategies import UserStrategy
from arbitrage_bot.user_workspace import (
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
    api_connection_egress_blockers,
)
from tests.web_test_support import make_config


HTML = f"{INDEX_HTML}\n{APP_JS}"


class WebMonitorStateTest(unittest.IsolatedAsyncioTestCase):
    def test_strategy_preflight_checks_private_data_market_balance_and_budget(
        self,
    ) -> None:
        now = time.time()
        cfg = make_config(
            risk=RiskConfig(
                trading_enabled=True,
                allow_live_trading=True,
                account_enabled={"coinbase-spot": True},
                strategy_enabled={"market_maker": True},
                max_order_quote=5.0,
                max_cycle_quote=20.0,
                max_orders_per_cycle=10,
                max_open_orders=20,
                max_order_book_age_seconds=10.0,
                max_order_book_gap_bps=100.0,
            ),
        )
        candidate = {
            "exchange": "coinbase-spot",
            "symbol": "ACS/USDC",
            "levels": 2,
            "quote_per_level": 2.0,
        }
        state_payload = {
            "quote_rates": {"USDC": 1.0},
            "markets": [
                {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "status": "ok",
                    "bid": 0.10,
                    "ask": 0.1005,
                    "bid_size": 10_000.0,
                    "ask_size": 10_000.0,
                }
            ],
            "account_balances": {
                "last_finished": now,
                "accounts": [
                    {
                        "exchange": "coinbase-spot",
                        "auth": {"configured": True, "missing_env": []},
                        "errors": [],
                        "balance": {
                            "checked": True,
                            "currencies": [
                                {"currency": "ACS", "free": 1_000.0},
                                {"currency": "USDC", "free": 1_000.0},
                            ],
                        },
                        "markets": [
                            {
                                "symbol": "ACS/USDC",
                                "status": "ok",
                                "market": {
                                    "found": True,
                                    "active": True,
                                    "limits": {"cost_min": 1.0},
                                },
                            }
                        ],
                    }
                ],
            },
            "order_activity": {
                "last_finished": now,
                "open_orders": [],
                "daily_pnl": {"total_realized_pnl": 0.0},
            },
            "market_maker": {"runtime": {"instances": []}},
            "slow_execution": {"tasks": {"tasks": []}},
        }

        ready = build_strategy_preflight(
            cfg,
            strategy_id="market_maker",
            candidate=candidate,
            state_payload=state_payload,
            now=now,
        )
        oversized = build_strategy_preflight(
            cfg,
            strategy_id="market_maker",
            candidate={**candidate, "quote_per_level": 6.0},
            state_payload=state_payload,
            now=now,
        )

        self.assertTrue(ready["ready"], ready["blockers"])
        self.assertFalse(oversized["ready"])
        self.assertIn("exceeds", "; ".join(oversized["blockers"]))

    async def test_non_admin_cannot_call_platform_trading_apis(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            method = "POST"
            match_info: dict[str, str] = {}

            def __init__(self, app: dict[str, object], path: str) -> None:
                self.app = app
                self.path = path

            def get(self, key: str, default: object = None) -> object:
                if key == "user_email":
                    return "trader@example.com"
                return default

            async def json(self) -> dict[str, object]:
                raise AssertionError("non-admin request reached payload handling")

        with tempfile.TemporaryDirectory() as tmp:
            user_store = WebUserStore(Path(tmp) / "users.json")
            user_store.create_user(
                email="admin@example.com",
                username="admin",
                password="AdminPass!234",
            )
            user_store.create_user(
                email="trader@example.com",
                username="trader",
                password="TraderPass!234",
                allowed_assets=["ACS"],
            )
            app: dict[str, object] = {
                "web_user_store": user_store,
                "monitor_state": object(),
                "config": make_config(),
                "auto_buy_sell_tasks": object(),
                "strategy_center_store": object(),
            }
            responses = [
                await api_market_maker(  # type: ignore[arg-type]
                    FakeRequest(app, "/api/market-maker")
                ),
                await api_strategy_center(  # type: ignore[arg-type]
                    FakeRequest(app, "/api/strategy-center")
                ),
            ]

        self.assertTrue(all(response.status == 403 for response in responses))
        self.assertTrue(all("admin role" in response.text for response in responses))

    async def test_non_admin_can_cancel_only_orders_from_owned_runtime(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            method = "POST"

            def __init__(
                self,
                app: dict[str, object],
                path: str,
                payload: dict[str, object],
            ) -> None:
                self.app = app
                self.path = path
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                if key == "user_email":
                    return "trader@example.com"
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            user_store = WebUserStore(Path(tmp) / "users.json")
            user_store.create_user(
                email="admin@example.com",
                username="admin",
                password="AdminPass!234",
            )
            user_store.create_user(
                email="trader@example.com",
                username="trader",
                password="TraderPass!234",
            )
            owner_exchange = ExchangeConfig(
                id="bybit",
                label="workspace:owner-bybit:spot",
                market_type="spot",
                credential_owner_email="trader@example.com",
            )
            owner_cfg = make_config(
                spot_exchanges=[owner_exchange],
                spot_markets=[
                    SpotMarketConfig(
                        asset="ACS",
                        exchange=owner_exchange.key,
                        symbol="ACS/USDT",
                        quote_currency="USDT",
                    )
                ],
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=str(Path(tmp) / "trade-events.jsonl"),
                ),
            )
            state = MonitorState(owner_cfg, 1.0)
            app: dict[str, object] = {
                "web_user_store": user_store,
                "monitor_state": state,
                "config": owner_cfg,
            }
            activity = {
                "status": "ok",
                "open_orders": [],
                "open_order_count": 0,
            }
            manager = AsyncMock()
            manager.close = AsyncMock()
            single_payload = {
                "exchange": owner_exchange.key,
                "symbol": "ACS/USDT",
                "order_id": "owner-order-1",
            }

            with (
                patch(
                    "arbitrage_bot.web.routes.trading."
                    "_user_auto_buy_sell_runtime_config",
                    return_value=owner_cfg,
                ),
                patch(
                    "arbitrage_bot.web.routes.trading.ExchangeManager",
                    return_value=manager,
                ),
                patch(
                    "arbitrage_bot.web.routes.trading.cancel_order_payload",
                    new=AsyncMock(return_value={"ok": True}),
                ) as cancel_one,
                patch(
                    "arbitrage_bot.web.routes.trading.cancel_bulk_orders_payload",
                    new=AsyncMock(return_value={"ok": True}),
                ) as cancel_bulk,
                patch(
                    "arbitrage_bot.web.routes.trading.fetch_order_activity_payload",
                    new=AsyncMock(return_value=activity),
                ),
                patch.object(
                    state,
                    "set_order_activity",
                    new=AsyncMock(),
                ) as set_platform_activity,
            ):
                single = await api_cancel_order(
                    FakeRequest(  # type: ignore[arg-type]
                        app,
                        "/api/orders/cancel",
                        single_payload,
                    )
                )
                bulk = await api_cancel_bulk_orders(
                    FakeRequest(  # type: ignore[arg-type]
                        app,
                        "/api/orders/cancel-bulk",
                        {"scope": "all"},
                    )
                )

        self.assertEqual(single.status, 200, single.text)
        self.assertEqual(bulk.status, 200, bulk.text)
        self.assertIs(cancel_one.await_args.args[0], owner_cfg)
        self.assertIs(cancel_bulk.await_args.args[0], owner_cfg)
        set_platform_activity.assert_not_awaited()

    async def test_non_admin_cancel_rejects_foreign_exchange_owner(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/orders/cancel"
            method = "POST"

            def __init__(self, app: dict[str, object]) -> None:
                self.app = app

            def get(self, key: str, default: object = None) -> object:
                if key == "user_email":
                    return "trader@example.com"
                return default

            async def json(self) -> dict[str, object]:
                return {
                    "exchange": "workspace:foreign:spot",
                    "symbol": "ACS/USDT",
                    "order_id": "foreign-order-1",
                }

        with tempfile.TemporaryDirectory() as tmp:
            user_store = WebUserStore(Path(tmp) / "users.json")
            user_store.create_user(
                email="admin@example.com",
                username="admin",
                password="AdminPass!234",
            )
            user_store.create_user(
                email="trader@example.com",
                username="trader",
                password="TraderPass!234",
            )
            foreign_cfg = make_config(
                spot_exchanges=[
                    ExchangeConfig(
                        id="bybit",
                        label="workspace:foreign:spot",
                        market_type="spot",
                        credential_owner_email="other@example.com",
                    )
                ]
            )
            state = MonitorState(foreign_cfg, 1.0)
            app: dict[str, object] = {
                "web_user_store": user_store,
                "monitor_state": state,
                "config": foreign_cfg,
            }
            with patch(
                "arbitrage_bot.web.routes.trading."
                "_user_auto_buy_sell_runtime_config",
                return_value=foreign_cfg,
            ):
                response = await api_cancel_order(  # type: ignore[arg-type]
                    FakeRequest(app)
                )

        self.assertEqual(response.status, 403)
        self.assertIn("not owned by this user", response.text)

    async def test_owner_auto_buy_sell_defaults_persist_across_refresh(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/auto-buy-sell"
            method = "POST"

            def __init__(
                self,
                app: dict[str, object],
                payload: dict[str, object],
            ) -> None:
                self.app = app
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                if key == "user_email":
                    return "trader@example.com"
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            user_store = WebUserStore(Path(tmp) / "users.json")
            user_store.create_user(
                email="admin@example.com",
                username="admin",
                password="AdminPass!234",
            )
            user = user_store.create_user(
                email="trader@example.com",
                username="trader",
                password="TraderPass!234",
            )
            workspace_store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env=None,
            )
            owner_exchange = ExchangeConfig(
                id="coinbase",
                label="workspace:coinbase-main:spot",
                market_type="spot",
                credential_owner_email=user.email,
            )
            owner_cfg = make_config(
                spot_exchanges=[owner_exchange],
                spot_markets=[
                    SpotMarketConfig(
                        asset="ACS",
                        exchange=owner_exchange.key,
                        symbol="ACS/USDC",
                        quote_currency="USDC",
                    )
                ],
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=str(Path(tmp) / "trade-events.jsonl"),
                ),
            )
            state = MonitorState(owner_cfg, 1.0)
            tasks = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            app: dict[str, object] = {
                "web_user_store": user_store,
                "user_workspace_store": workspace_store,
                "monitor_state": state,
                "config": owner_cfg,
                "auto_buy_sell_tasks": tasks,
            }
            request = FakeRequest(
                app,
                {
                    "enabled": False,
                    "exchange": owner_exchange.key,
                    "symbol": "ACS/USDC",
                    "side": "buy",
                    "instrument_type": "spot",
                    "total_quote": 25.0,
                    "slice_base_min": 100.0,
                    "slice_base_max": 100.0,
                    "interval_seconds": 30.0,
                    "price_mode": "taker",
                },
            )

            with patch(
                "arbitrage_bot.web.routes.strategies."
                "_user_auto_buy_sell_runtime_config",
                return_value=owner_cfg,
            ):
                initial = await _user_auto_buy_sell_payload(
                    request,  # type: ignore[arg-type]
                    user,
                    owner_cfg,
                    runtime_cfg=owner_cfg,
                )
                response = await api_slow_execution(request)  # type: ignore[arg-type]
                refreshed = await _user_auto_buy_sell_payload(
                    request,  # type: ignore[arg-type]
                    user,
                    owner_cfg,
                    runtime_cfg=owner_cfg,
                )
            saved = workspace_store.strategy_default(user.email, "auto_buy_sell")

        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(initial["config"]["instrument_type"], "spot")
        self.assertEqual(initial["config"]["exchange"], owner_exchange.key)
        self.assertEqual(initial["config"]["symbol"], "ACS/USDC")
        self.assertEqual(saved["exchange"], owner_exchange.key)
        self.assertEqual(saved["symbol"], "ACS/USDC")
        self.assertEqual(saved["side"], "buy")
        self.assertEqual(saved["total_quote"], 25.0)
        self.assertEqual(refreshed["config"]["exchange"], owner_exchange.key)
        self.assertEqual(refreshed["config"]["symbol"], "ACS/USDC")
        self.assertEqual(refreshed["config"]["side"], "buy")
        self.assertEqual(refreshed["config"]["total_quote"], 25.0)

    def test_non_admin_auto_buy_sell_accepts_owned_spot_and_perpetual(self) -> None:
        user = WebUser(
            email="trader@example.com",
            username="trader",
            password_hash="unused",
            totp_secret="unused",
            role="user",
        )
        close_long = SlowExecutionConfig(
            instrument_type="perpetual",
            position_effect="reduce_only",
            position_side="long",
            side="sell",
        )
        _require_user_owned_execution(user, close_long)
        _require_user_owned_execution(
            user,
            replace(close_long, position_effect="open"),
        )
        _require_user_owned_execution(
            user,
            replace(close_long, instrument_type="spot"),
        )

    async def test_owner_spot_execution_preflight_uses_private_balance(self) -> None:
        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(
                    id="binance",
                    label="owner-spot",
                    market_type="spot",
                    credential_owner_email="trader@example.com",
                )
            ],
            risk=RiskConfig(
                allow_live_trading=True,
                max_order_quote=50.0,
                max_cycle_quote=50.0,
            ),
            quote_rates={"USD": 1.0, "USDT": 1.0},
        )
        task = SlowExecutionConfig(
            enabled=True,
            exchange="owner-spot",
            symbol="BTC/USDT",
            side="buy",
            total_quote=20.0,
            slice_base_min=0.001,
            slice_base_max=0.001,
            interval_seconds=10.0,
        )
        manager = AsyncMock()
        manager.fetch_market_info.return_value = {
            "active": True,
            "spot": True,
        }
        manager.fetch_order_book.return_value = OrderBookSnapshot(
            exchange="owner-spot",
            symbol="BTC/USDT",
            bids=[BookLevel(price=9_990.0, amount=2.0)],
            asks=[BookLevel(price=10_000.0, amount=2.0)],
        )
        manager.prepare_limit_order.return_value = {"amount": 0.001}
        manager.fetch_balance.return_value = {"USDT": {"free": 100.0}}
        manager.fetch_open_orders.return_value = []

        with patch(
            "arbitrage_bot.web.routes.control.ExchangeManager",
            return_value=manager,
        ):
            result = await _user_execution_preflight(cfg, task)

        self.assertTrue(result["ready"], result["blockers"])
        self.assertEqual(result["status"], "ready")
        manager.fetch_balance.assert_awaited_once()
        manager.close.assert_awaited_once()

    async def test_owner_perpetual_execution_preflight_allows_guarded_open(self) -> None:
        cfg = make_config(
            derivative_exchanges=[
                ExchangeConfig(
                    id="binanceusdm",
                    label="owner-swap",
                    market_type="swap",
                    credential_owner_email="trader@example.com",
                )
            ],
            risk=RiskConfig(
                allow_live_trading=True,
                max_order_quote=50.0,
                max_cycle_quote=50.0,
                max_derivative_leverage=3.0,
            ),
            quote_rates={"USD": 1.0, "USDT": 1.0},
        )
        task = SlowExecutionConfig(
            enabled=True,
            exchange="owner-swap",
            symbol="BTC/USDT:USDT",
            side="buy",
            total_quote=20.0,
            slice_base_min=0.001,
            slice_base_max=0.001,
            interval_seconds=10.0,
            instrument_type="perpetual",
            position_effect="open",
            position_side="long",
            margin_mode="cross",
            leverage=2.0,
            max_position_quote=100.0,
        )
        manager = AsyncMock()
        manager.fetch_market_info.return_value = {
            "active": True,
            "swap": True,
        }
        manager.fetch_order_book.return_value = OrderBookSnapshot(
            exchange="owner-swap",
            symbol="BTC/USDT:USDT",
            bids=[BookLevel(price=9_990.0, amount=2.0)],
            asks=[BookLevel(price=10_000.0, amount=2.0)],
        )
        manager.prepare_linear_contract_order.return_value = {"contracts": 1.0}
        manager.fetch_positions.return_value = []
        manager.fetch_open_orders.return_value = []

        with patch(
            "arbitrage_bot.web.routes.control.ExchangeManager",
            return_value=manager,
        ):
            result = await _user_execution_preflight(cfg, task)

        self.assertTrue(result["ready"], result["blockers"])
        manager.fetch_positions.assert_awaited_once()
        manager.prepare_linear_contract_order.assert_awaited_once()
        manager.close.assert_awaited_once()

    async def test_owner_perpetual_preflight_explains_disabled_leverage_cap(self) -> None:
        cfg = make_config(
            derivative_exchanges=[
                ExchangeConfig(
                    id="binanceusdm",
                    label="owner-swap",
                    market_type="swap",
                    credential_owner_email="trader@example.com",
                )
            ],
            risk=RiskConfig(
                allow_live_trading=True,
                max_order_quote=50.0,
                max_cycle_quote=50.0,
                max_derivative_leverage=0.0,
            ),
            quote_rates={"USD": 1.0, "USDT": 1.0},
        )
        task = SlowExecutionConfig(
            enabled=True,
            exchange="owner-swap",
            symbol="BTC/USDT:USDT",
            side="sell",
            total_quote=20.0,
            slice_base_min=0.001,
            slice_base_max=0.001,
            interval_seconds=10.0,
            instrument_type="perpetual",
            position_effect="open",
            position_side="short",
            margin_mode="cross",
            leverage=3.0,
            max_position_quote=100.0,
        )
        manager = AsyncMock()
        manager.fetch_market_info.return_value = {"active": True, "swap": True}
        manager.fetch_order_book.return_value = OrderBookSnapshot(
            exchange="owner-swap",
            symbol="BTC/USDT:USDT",
            bids=[BookLevel(price=9_990.0, amount=2.0)],
            asks=[BookLevel(price=10_000.0, amount=2.0)],
        )
        manager.fetch_positions.return_value = []
        manager.fetch_open_orders.return_value = []

        with patch(
            "arbitrage_bot.web.routes.control.ExchangeManager",
            return_value=manager,
        ):
            result = await _user_execution_preflight(cfg, task)

        self.assertFalse(result["ready"])
        self.assertTrue(
            any(
                "set Risk Controls > Max Leverage to at least 3x" in blocker
                for blocker in result["blockers"]
            ),
            result["blockers"],
        )
        manager.close.assert_awaited_once()

    async def test_market_maker_cycle_finishes_before_shutdown(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def cycle() -> dict[str, object]:
            started.set()
            await release.wait()
            return {"status": "placed"}

        task = asyncio.create_task(_complete_market_maker_cycle_on_shutdown(cycle()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)

        self.assertFalse(task.done())
        release.set()
        payload, shutdown_requested = await task

        self.assertEqual(payload, {"status": "placed"})
        self.assertTrue(shutdown_requested)

    async def test_rebalance_api_requires_live_phrase_and_persists_config(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/cross-exchange-rebalance"
            method = "POST"

            def __init__(
                self,
                app: dict[str, object],
                payload: dict[str, object],
            ) -> None:
                self.app = app
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                spot_exchanges=[
                    ExchangeConfig(id="bithumb", label="bithumb-spot"),
                    ExchangeConfig(id="coinbase", label="coinbase-spot"),
                ],
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
                quote_rates={"USD": 1.0, "KRW": 0.00075, "USDC": 1.0},
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                ),
            )
            store_path = os.path.join(tmp, "web_runtime_overrides.json")
            state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            preflight = StrategyPreflightService()
            app: dict[str, object] = {
                "monitor_state": state,
                "config": cfg,
                "strategy_preflight_service": preflight,
            }
            payload: dict[str, object] = {
                "action": "update",
                "enabled": True,
                "live_enabled": True,
                "buy_exchange": "bithumb-spot",
                "buy_symbol": "ACS/KRW",
                "sell_exchange": "coinbase-spot",
                "sell_symbol": "ACS/USDC",
                "total_quote_common": 100.0,
                "quote_per_cycle_common": 10.0,
                "interval_seconds": 30.0,
            }

            denied = await api_cross_exchange_rebalance(
                FakeRequest(app, payload)  # type: ignore[arg-type]
            )
            candidate, _ = await _preflight_candidate_from_payload(
                state,
                cfg,
                strategy_id="cross_exchange_rebalance",
                payload=payload,
            )
            grant = preflight.issue(
                owner_email="legacy-admin",
                strategy_id="cross_exchange_rebalance",
                candidate=candidate,
            )
            approved = await api_cross_exchange_rebalance(
                FakeRequest(  # type: ignore[arg-type]
                    app,
                    {
                        **payload,
                        "confirm_live": "ENABLE LIVE REBALANCE",
                        "preflight_token": grant.token,
                    },
                )
            )
            reconfirm_denied = await api_cross_exchange_rebalance(
                FakeRequest(  # type: ignore[arg-type]
                    app,
                    {**payload, "total_quote_common": 110.0},
                )
            )
            restored = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            restored_cfg = await restored.runtime_config(cfg)

        self.assertEqual(denied.status, 400)
        self.assertIn("confirm_live", json.loads(denied.text)["error"])
        self.assertEqual(approved.status, 200, approved.text)
        self.assertTrue(json.loads(approved.text)["ok"])
        self.assertEqual(reconfirm_denied.status, 400)
        self.assertIn("saving live config", reconfirm_denied.text)
        self.assertTrue(restored_cfg.cross_exchange_rebalance.enabled)
        self.assertTrue(restored_cfg.cross_exchange_rebalance.live_enabled)
        self.assertEqual(
            restored_cfg.cross_exchange_rebalance.buy_exchange,
            "bithumb-spot",
        )
        self.assertEqual(
            restored_cfg.cross_exchange_rebalance.sell_symbol,
            "ACS/USDC",
        )

    async def test_rebalance_api_acknowledges_residual_without_restarting_live(
        self,
    ) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/cross-exchange-rebalance"
            method = "POST"

            def __init__(
                self, app: dict[str, object], payload: dict[str, object]
            ) -> None:
                self.app = app
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = os.path.join(tmp, "rebalance_runtime.json")
            cfg = make_config(
                cross_exchange_rebalance=CrossExchangeRebalanceConfig(
                    enabled=True,
                    live_enabled=True,
                    buy_exchange="bithumb-spot",
                    buy_symbol="ACS/KRW",
                    sell_exchange="coinbase-spot",
                    sell_symbol="ACS/USDC",
                    total_quote_common=100.0,
                    quote_per_cycle_common=10.0,
                    runtime_path=runtime_path,
                ),
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                ),
            )
            state = MonitorState(cfg, 1.0)
            runtime = new_rebalance_runtime(
                cfg.cross_exchange_rebalance,
                common_quote_currency=cfg.common_quote_currency,
            )
            runtime.update(
                {
                    "status": "halted",
                    "halted": True,
                    "halt_reason": "hedge_required",
                    "residual_exposure": {
                        "asset": "ACS",
                        "side": "buy",
                        "quantity_base": 123.45,
                        "detected_at": 1.0,
                    },
                }
            )
            save_rebalance_runtime(runtime_path, runtime)
            await state.set_cross_exchange_rebalance_runtime(runtime)
            app: dict[str, object] = {"monitor_state": state, "config": cfg}

            response = await api_cross_exchange_rebalance(
                FakeRequest(
                    app,
                    {
                        "action": "acknowledge_exposure",
                        "confirm_acknowledgement": "ACKNOWLEDGE RESIDUAL EXPOSURE",
                    },
                )  # type: ignore[arg-type]
            )
            updated = await state.cross_exchange_rebalance_runtime()

        self.assertEqual(response.status, 200, response.text)
        self.assertTrue(json.loads(response.text)["ok"])
        self.assertFalse(updated["halted"])
        self.assertEqual(updated["status"], "acknowledged_exposure")
        self.assertTrue(updated["residual_exposure_acknowledged"])
        self.assertIn("acknowledged_at", updated["residual_exposure"])

    async def test_rebalance_api_stops_and_releases_coordination_atomically(
        self,
    ) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/cross-exchange-rebalance"
            method = "POST"

            def __init__(self, app: dict[str, object]) -> None:
                self.app = app

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return {
                    "action": "stop_and_release",
                    "confirm_stop": "STOP REBALANCE AND RELEASE MM",
                }

        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = os.path.join(tmp, "rebalance_runtime.json")
            cfg = make_config(
                cross_exchange_rebalance=CrossExchangeRebalanceConfig(
                    enabled=True,
                    live_enabled=True,
                    buy_exchange="bithumb-spot",
                    buy_symbol="ACS/KRW",
                    sell_exchange="coinbase-spot",
                    sell_symbol="ACS/USDC",
                    total_quote_common=100.0,
                    quote_per_cycle_common=10.0,
                    runtime_path=runtime_path,
                ),
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                ),
            )
            state = MonitorState(
                cfg,
                1.0,
                runtime_store_path=os.path.join(tmp, "web_runtime_overrides.json"),
            )
            runtime = new_rebalance_runtime(
                cfg.cross_exchange_rebalance,
                common_quote_currency=cfg.common_quote_currency,
            )
            runtime.update(
                {
                    "status": "halted",
                    "halted": True,
                    "halt_reason": "hedge_required",
                    "residual_exposure": {
                        "asset": "ACS",
                        "side": "sell",
                        "quantity_base": 12.5,
                        "detected_at": 1.0,
                    },
                }
            )
            save_rebalance_runtime(runtime_path, runtime)
            await state.set_cross_exchange_rebalance_runtime(runtime)
            await state.acquire_coordination_hold(
                "cross_exchange_rebalance",
                [("coinbase-spot", "ACS/USDC")],
                reason="test",
                ttl_seconds=60,
            )

            response = await api_cross_exchange_rebalance(
                FakeRequest({"monitor_state": state, "config": cfg})  # type: ignore[arg-type]
            )
            updated = await state.cross_exchange_rebalance_runtime()
            runtime_cfg = await state.runtime_config(cfg)
            holds = await state.coordination_holds()

        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(updated["status"], "stopped_by_operator")
        self.assertFalse(updated["halted"])
        self.assertTrue(updated["residual_exposure_acknowledged"])
        self.assertFalse(runtime_cfg.cross_exchange_rebalance.enabled)
        self.assertFalse(runtime_cfg.cross_exchange_rebalance.live_enabled)
        self.assertEqual(holds, [])

    async def test_market_maker_api_requires_confirmation_when_starting_live(
        self,
    ) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/market-maker"
            method = "POST"

            def __init__(
                self, app: dict[str, object], payload: dict[str, object]
            ) -> None:
                self.app = app
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                spot_exchanges=[
                    ExchangeConfig(id="coinbase", label="coinbase-spot"),
                ],
                spot_markets=[
                    SpotMarketConfig(
                        asset="ACS",
                        exchange="coinbase-spot",
                        symbol="ACS/USDC",
                        quote_currency="USDC",
                    ),
                ],
                market_maker=MarketMakerConfig(
                    id="coinbase-spot-acs-usdc",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                ),
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                ),
            )
            state = MonitorState(
                cfg,
                1.0,
                runtime_store_path=os.path.join(tmp, "web_runtime_overrides.json"),
            )
            preflight = StrategyPreflightService()
            app: dict[str, object] = {
                "monitor_state": state,
                "config": cfg,
                "strategy_preflight_service": preflight,
            }
            payload: dict[str, object] = {
                "id": "coinbase-spot-acs-usdc",
                "enabled": True,
                "live_enabled": True,
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "levels": 2,
                "price_band_pct": 1.0,
                "quote_per_level": 1.0,
                "poll_seconds": 10.0,
                "post_only": True,
            }

            denied = await api_market_maker(
                FakeRequest(app, payload)  # type: ignore[arg-type]
            )
            candidate, _ = await _preflight_candidate_from_payload(
                state,
                cfg,
                strategy_id="market_maker",
                payload=payload,
            )
            grant = preflight.issue(
                owner_email="legacy-admin",
                strategy_id="market_maker",
                candidate=candidate,
            )
            with patch(
                "arbitrage_bot.web.routes.trading._cleanup_market_maker_instance",
                new=AsyncMock(return_value={"status": "ok", "canceled_count": 0}),
            ) as cleanup:
                approved = await api_market_maker(
                    FakeRequest(  # type: ignore[arg-type]
                        app,
                        {
                            **payload,
                            "confirm_live": "ENABLE LIVE MARKET MAKER",
                            "cleanup_recoverable_state": True,
                            "preflight_token": grant.token,
                        },
                    )
                )
            unchanged_live = await api_market_maker(
                FakeRequest(app, payload)  # type: ignore[arg-type]
            )
            live_change_denied = await api_market_maker(
                FakeRequest(  # type: ignore[arg-type]
                    app,
                    {**payload, "quote_per_level": 2.0},
                )
            )
            runtime_cfg = await state.runtime_config(cfg)

        self.assertEqual(denied.status, 400)
        self.assertIn("confirm_live", json.loads(denied.text)["error"])
        self.assertEqual(approved.status, 200, approved.text)
        cleanup.assert_awaited_once()
        self.assertEqual(unchanged_live.status, 200, unchanged_live.text)
        self.assertEqual(live_change_denied.status, 400)
        self.assertIn("confirm_live", json.loads(live_change_denied.text)["error"])
        self.assertTrue(runtime_cfg.market_maker.enabled)
        self.assertTrue(runtime_cfg.market_maker.live_enabled)

    async def test_auto_buy_sell_task_api_requires_start_confirmation(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/auto-buy-sell/tasks"
            method = "POST"
            app: dict[str, object] = {
                "monitor_state": object(),
                "config": make_config(),
                "auto_buy_sell_tasks": object(),
            }

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return {}

        response = await api_create_auto_buy_sell_task(  # type: ignore[arg-type]
            FakeRequest()
        )

        self.assertEqual(response.status, 400)
        self.assertIn("confirm_live", json.loads(response.text)["error"])

    async def test_existing_blocked_task_requires_confirmation_to_enable_mm_coordination(
        self,
    ) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/auto-buy-sell/tasks/auto-1/control"
            method = "POST"

            def __init__(
                self,
                app: dict[str, object],
                task_id: str,
                payload: dict[str, object],
            ) -> None:
                self.app = app
                self.match_info = {"task_id": task_id}
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                ),
            )
            state = MonitorState(cfg, 1.0)
            tasks = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            task = await tasks.create_task(
                SlowExecutionConfig(
                    enabled=True,
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    side="buy",
                    total_quote=10_000.0,
                    slice_base_min=50_000.0,
                    slice_base_max=80_000.0,
                )
            )
            tasks._tasks[0].status = "blocked_by_risk"
            tasks._tasks[0].last_risk = {
                "approved": False,
                "self_trade_guard": {"blocked": True},
            }
            tasks.store.save(tasks._tasks)
            app: dict[str, object] = {
                "monitor_state": state,
                "config": cfg,
                "auto_buy_sell_tasks": tasks,
            }

            denied = await api_control_auto_buy_sell_task(
                FakeRequest(
                    app,
                    task["id"],
                    {"action": "enable_mm_coordination"},
                )  # type: ignore[arg-type]
            )
            approved = await api_control_auto_buy_sell_task(
                FakeRequest(
                    app,
                    task["id"],
                    {
                        "action": "enable_mm_coordination",
                        "confirm_live": "ENABLE LIVE AUTO BUY SELL",
                    },
                )  # type: ignore[arg-type]
            )

        self.assertEqual(denied.status, 400)
        self.assertIn("confirm_live", denied.text)
        self.assertEqual(approved.status, 200, approved.text)
        self.assertTrue(json.loads(approved.text)["task"]["config"]["coordinate_market_maker"])

    async def test_owner_can_enable_mm_coordination_only_for_owned_task(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/auto-buy-sell/tasks/auto-1/control"
            method = "POST"

            def __init__(
                self,
                app: dict[str, object],
                task_id: str,
                email: str,
            ) -> None:
                self.app = app
                self.match_info = {"task_id": task_id}
                self.email = email

            def get(self, key: str, default: object = None) -> object:
                if key == "user_email":
                    return self.email
                return default

            async def json(self) -> dict[str, object]:
                return {
                    "action": "enable_mm_coordination",
                    "confirm_live": "ENABLE LIVE AUTO BUY SELL",
                }

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                ),
            )
            user_store = WebUserStore(Path(tmp) / "users.json")
            user_store.create_user(
                email="admin@example.com",
                username="admin",
                password="AdminPass!234",
            )
            user_store.create_user(
                email="owner@example.com",
                username="owner",
                password="OwnerPass!234",
            )
            user_store.create_user(
                email="other@example.com",
                username="other",
                password="OtherPass!234",
            )
            state = MonitorState(cfg, 1.0)
            tasks = AutoBuySellTaskService(Path(tmp) / "tasks.json")
            task = await tasks.create_task(
                SlowExecutionConfig(
                    enabled=True,
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    side="buy",
                    total_quote=10_000.0,
                    slice_base_min=50_000.0,
                    slice_base_max=80_000.0,
                ),
                owner_email="owner@example.com",
            )
            tasks._tasks[0].status = "blocked_by_risk"
            tasks._tasks[0].last_risk = {
                "approved": False,
                "self_trade_guard": {"blocked": True},
            }
            tasks.store.save(tasks._tasks)
            app: dict[str, object] = {
                "web_user_store": user_store,
                "monitor_state": state,
                "config": cfg,
                "auto_buy_sell_tasks": tasks,
            }

            denied = await api_control_auto_buy_sell_task(
                FakeRequest(  # type: ignore[arg-type]
                    app,
                    task["id"],
                    "other@example.com",
                )
            )
            approved = await api_control_auto_buy_sell_task(
                FakeRequest(  # type: ignore[arg-type]
                    app,
                    task["id"],
                    "owner@example.com",
                )
            )

        self.assertEqual(denied.status, 400)
        self.assertIn("unknown Auto Buy/Sell task", denied.text)
        self.assertEqual(approved.status, 200, approved.text)
        self.assertTrue(
            json.loads(approved.text)["task"]["config"]["coordinate_market_maker"]
        )

    async def test_login_lockout_after_repeated_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env="WEB_LOGIN_PW_TEST",
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(data_dir / "web_users.json"),
                ),
            )
            with patch.dict(os.environ, {"WEB_LOGIN_PW_TEST": "correct horse"}):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    for _ in range(LOGIN_MAX_FAILURES):
                        bad = await client.post("/login", data={"password": "wrong"})
                        self.assertEqual(bad.status, 401)

                    locked = await client.post("/login", data={"password": "wrong"})
                    self.assertEqual(locked.status, 429)
                    self.assertIn("Retry-After", locked.headers)

                    # Even the correct password is refused while locked out.
                    blocked = await client.post(
                        "/login", data={"password": "correct horse"}
                    )
                    self.assertEqual(blocked.status, 429)
                finally:
                    await client.close()

    async def test_metrics_endpoint_allows_local_scrape_without_dashboard_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cfg = make_config(
                strategy_center=StrategyCenterConfig(
                    path=str(data_dir / "strategy_center.sqlite3"),
                ),
                web_security=WebSecurityConfig(
                    password_env="TEST_WEB_PASSWORD",
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(data_dir / "web_users.json"),
                ),
            )
            with patch.dict(os.environ, {"TEST_WEB_PASSWORD": "123456"}, clear=False):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    metrics_response = await client.get("/metrics")
                    metrics_text = await metrics_response.text()
                    state_response = await client.get("/api/state")

                    self.assertEqual(metrics_response.status, 200)
                    self.assertIn("crypto_arb_scan_count", metrics_text)
                    self.assertEqual(state_response.status, 401)
                finally:
                    await client.close()

    async def test_proxy_forwarded_health_and_metrics_require_authentication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cfg = make_config(
                strategy_center=StrategyCenterConfig(
                    path=str(data_dir / "strategy_center.sqlite3"),
                ),
                web_security=WebSecurityConfig(
                    password_env="TEST_WEB_PASSWORD",
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(data_dir / "web_users.json"),
                ),
            )
            with patch.dict(os.environ, {"TEST_WEB_PASSWORD": "123456"}, clear=False):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    headers = {"X-Forwarded-For": "203.0.113.10"}
                    health = await client.get("/api/health", headers=headers)
                    metrics = await client.get(
                        "/metrics",
                        headers=headers,
                        allow_redirects=False,
                    )

                    self.assertEqual(health.status, 401)
                    self.assertEqual(metrics.status, 302)
                    self.assertEqual(metrics.headers.get("Location"), "/login")
                finally:
                    await client.close()

    async def test_strategy_center_api_upsert_creates_with_supplied_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cfg = make_config(
                strategy_center=StrategyCenterConfig(
                    path=str(data_dir / "strategy_center.sqlite3"),
                ),
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(data_dir / "web_users.json"),
                ),
                trade_log=TradeLogConfig(
                    enabled=True,
                    path=str(data_dir / "trade_events.jsonl"),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                account_response = await client.post(
                    "/api/strategy-center",
                    json={
                        "action": "upsert_account",
                        "account": {
                            "id": "coinbase-main",
                            "label": "Coinbase Main",
                            "exchange": "coinbase-spot",
                            "asset_scope": ["ACS"],
                            "api_key_env": "COINBASE_API_KEY",
                            "secret_env": "COINBASE_SECRET",
                            "enabled": True,
                        },
                    },
                )
                account_payload = await account_response.json()
                strategy_response = await client.post(
                    "/api/strategy-center",
                    json={
                        "action": "upsert_strategy",
                        "strategy": {
                            "id": "acs-mm",
                            "name": "ACS Coinbase MM",
                            "strategy_type": "market_maker",
                            "account_id": "coinbase-main",
                            "exchange": "coinbase-spot",
                            "symbol": "ACS/USDC",
                            "asset": "ACS",
                            "enabled": True,
                        },
                    },
                )
                strategy_payload = await strategy_response.json()
            finally:
                await client.close()

        self.assertEqual(account_response.status, 200, account_payload)
        self.assertEqual(strategy_response.status, 200, strategy_payload)
        self.assertTrue(account_payload["ok"])
        self.assertTrue(strategy_payload["ok"])
        self.assertEqual(
            strategy_payload["strategy_center"]["summary"]["strategy_count"],
            1,
        )

    async def test_fetch_order_activity_payload_summarizes_orders_and_fills(
        self,
    ) -> None:
        class FakeOrderManager:
            async def fetch_open_orders(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "id": "order-open-1",
                        "symbol": symbol,
                        "side": "buy",
                        "type": "limit",
                        "status": "open",
                        "price": 0.00014,
                        "amount": 1000.0,
                        "filled": 100.0,
                        "remaining": 900.0,
                        "cost": 0.14,
                        "timestamp": 123_000,
                    }
                ]

            async def fetch_closed_orders(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
                limit: int = 20,
            ) -> list[dict[str, object]]:
                if limit != 20:
                    raise AssertionError(limit)
                return [
                    {
                        "id": "order-closed-1",
                        "symbol": symbol,
                        "side": "sell",
                        "status": "closed",
                        "price": 0.00015,
                        "amount": 500.0,
                        "filled": 500.0,
                        "remaining": 0.0,
                        "timestamp": 124_000,
                    }
                ]

            async def fetch_my_trades(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
                limit: int = 20,
            ) -> list[dict[str, object]]:
                if limit != 20:
                    raise AssertionError(limit)
                return [
                    {
                        "id": "trade-1",
                        "order": "order-closed-1",
                        "symbol": symbol,
                        "side": "sell",
                        "price": 0.00015,
                        "amount": 500.0,
                        "cost": 0.075,
                        "fee": {"cost": 0.0001, "currency": "USDC"},
                        "timestamp": 125_000,
                    }
                ]

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                    market_type="spot",
                    api_key_env="COINBASE_API_KEY",
                    secret_env="COINBASE_SECRET",
                )
            ],
        )

        with patch.dict(
            os.environ,
            {"COINBASE_API_KEY": "key", "COINBASE_SECRET": "secret"},
            clear=True,
        ):
            payload = await fetch_order_activity_payload(cfg, FakeOrderManager())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["open_order_count"], 1)
        self.assertEqual(payload["closed_order_count"], 1)
        self.assertEqual(payload["recent_trade_count"], 1)
        self.assertEqual(payload["open_orders"][0]["id"], "order-open-1")
        self.assertAlmostEqual(payload["open_orders"][0]["open_notional"], 0.126)
        self.assertEqual(payload["recent_trades"][0]["order_id"], "order-closed-1")
        self.assertEqual(payload["recent_trades"][0]["fee"]["currency"], "USDC")

    async def test_fetch_order_activity_payload_treats_unused_accounts_as_idle(
        self,
    ) -> None:
        class FakeOrderManager:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_open_orders(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
            ) -> list[dict[str, object]]:
                self.calls += 1
                return []

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
        )
        manager = FakeOrderManager()

        with patch.dict(os.environ, {}, clear=True):
            payload = await fetch_order_activity_payload(cfg, manager)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["accounts"][0]["status"], "idle")
        self.assertEqual(payload["checked_account_count"], 0)
        self.assertEqual(manager.calls, 0)

    async def test_cancel_order_payload_validates_and_cancels_configured_symbol(
        self,
    ) -> None:
        class FakeCancelManager:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            async def cancel_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                order_id: str,
            ) -> dict[str, object]:
                self.calls.append((exchange.key, symbol, order_id))
                return {"id": order_id, "status": "canceled"}

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                    market_type="spot",
                    api_key_env="COINBASE_API_KEY",
                    secret_env="COINBASE_SECRET",
                )
            ],
        )
        manager = FakeCancelManager()

        with patch.dict(
            os.environ,
            {"COINBASE_API_KEY": "key", "COINBASE_SECRET": "secret"},
            clear=True,
        ):
            payload = await cancel_order_payload(
                cfg,
                manager,
                {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "order_id": "order-open-1",
                },
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(manager.calls, [("coinbase-spot", "ACS/USDC", "order-open-1")])
        self.assertEqual(payload["event"]["type"], "manual_order_cancel")

    async def test_cancel_bulk_orders_payload_cancels_single_account(self) -> None:
        class FakeBulkCancelManager:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            async def fetch_open_orders(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
            ) -> list[dict[str, object]]:
                if exchange.key == "coinbase-spot":
                    return [
                        {
                            "id": "coinbase-order-1",
                            "symbol": symbol,
                            "side": "buy",
                            "status": "open",
                            "price": 0.00014,
                            "amount": 1000.0,
                            "cost": 0.14,
                        }
                    ]
                return [
                    {
                        "id": "bybit-order-1",
                        "symbol": symbol,
                        "side": "sell",
                        "status": "open",
                        "price": 0.00015,
                        "amount": 1000.0,
                        "cost": 0.15,
                    }
                ]

            async def fetch_closed_orders(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
                limit: int = 20,
            ) -> list[dict[str, object]]:
                return []

            async def fetch_my_trades(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
                limit: int = 20,
            ) -> list[dict[str, object]]:
                return []

            async def cancel_order(
                self,
                exchange: ExchangeConfig,
                *,
                symbol: str,
                order_id: str,
            ) -> dict[str, object]:
                self.calls.append((exchange.key, symbol, order_id))
                return {"id": order_id, "symbol": symbol, "status": "canceled"}

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                ),
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                    api_key_env="COINBASE_API_KEY",
                    secret_env="COINBASE_SECRET",
                ),
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                ),
            ],
        )
        manager = FakeBulkCancelManager()

        with patch.dict(
            os.environ,
            {
                "COINBASE_API_KEY": "key",
                "COINBASE_SECRET": "secret",
                "BYBIT_API_KEY": "key",
                "BYBIT_SECRET": "secret",
            },
            clear=True,
        ):
            payload = await cancel_bulk_orders_payload(
                cfg,
                manager,
                {"scope": "account", "exchange": "coinbase-spot"},
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["requested_count"], 1)
        self.assertEqual(payload["canceled_count"], 1)
        self.assertEqual(
            manager.calls,
            [("coinbase-spot", "ACS/USDC", "coinbase-order-1")],
        )
        self.assertEqual(payload["event"]["type"], "manual_bulk_cancel")

    async def test_cancel_order_payload_rejects_unconfigured_symbol(self) -> None:
        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                    market_type="spot",
                    api_key_env="COINBASE_API_KEY",
                    secret_env="COINBASE_SECRET",
                )
            ],
        )

        with patch.dict(
            os.environ,
            {"COINBASE_API_KEY": "key", "COINBASE_SECRET": "secret"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "symbol is not configured"):
                await cancel_order_payload(
                    cfg,
                    object(),
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "BTC/USDC",
                        "order_id": "order-open-1",
                    },
                )

    async def test_fetch_account_balances_payload_summarizes_totals(self) -> None:
        class FakeBalanceManager:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                self.calls += 1
                return {
                    "free": {"ACS": 1000.0, "USDT": 20.0},
                    "used": {"ACS": 0.0, "USDT": 1.0},
                    "total": {"ACS": 1000.0, "USDT": 21.0},
                }

            async def fetch_market_info(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
            ) -> dict[str, object]:
                assert symbol == "ACS/USDT"
                return {
                    "id": "ACSUSDT",
                    "symbol": "ACS/USDT",
                    "active": True,
                    "type": "spot",
                    "spot": True,
                    "precision": {"amount": 1.0, "price": 0.000001},
                    "limits": {
                        "amount": {"min": 10.0, "max": 1_000_000.0},
                        "cost": {"min": 5.0, "max": 100_000.0},
                    },
                }

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
        )
        manager = FakeBalanceManager()

        with patch.dict(
            os.environ,
            {"BYBIT_API_KEY": "key", "BYBIT_SECRET": "secret"},
            clear=True,
        ):
            payload = await fetch_account_balances_payload(cfg, manager)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checked_account_count"], 1)
        self.assertEqual(manager.calls, 1)
        totals = {row["currency"]: row for row in payload["totals"]}
        self.assertEqual(totals["ACS"]["total"], 1000.0)
        self.assertEqual(totals["USDT"]["free"], 20.0)
        self.assertEqual(payload["accounts"][0]["status"], "ok")
        self.assertEqual(payload["accounts"][0]["markets"][0]["status"], "ok")
        self.assertEqual(
            payload["accounts"][0]["markets"][0]["market"]["limits"]["cost_min"],
            5.0,
        )

    async def test_fetch_account_balances_adjusts_for_open_order_reserves(self) -> None:
        class FakeBalanceManager:
            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                return {
                    "free": {"ACS": 10_000.0, "USDC": 5_000.0},
                    "used": {"ACS": 0.0, "USDC": 0.0},
                    "total": {"ACS": 10_000.0, "USDC": 5_000.0},
                }

            async def fetch_open_orders(
                self,
                _: ExchangeConfig,
                *,
                symbol: str,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "id": "buy-1",
                        "symbol": symbol,
                        "side": "buy",
                        "price": 0.00014,
                        "amount": 10_000_000.0,
                        "remaining": 10_000_000.0,
                    },
                    {
                        "id": "sell-1",
                        "symbol": symbol,
                        "side": "sell",
                        "price": 0.00015,
                        "amount": 1_000.0,
                        "remaining": 900.0,
                    },
                ]

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase",
                    label="coinbase-spot",
                    market_type="spot",
                    api_key_env="COINBASE_API_KEY",
                    secret_env="COINBASE_SECRET",
                )
            ],
        )

        with patch.dict(
            os.environ,
            {"COINBASE_API_KEY": "key", "COINBASE_SECRET": "secret"},
            clear=True,
        ):
            payload = await fetch_account_balances_payload(cfg, FakeBalanceManager())

        balances = {
            row["currency"]: row
            for row in payload["accounts"][0]["balance"]["currencies"]
        }
        self.assertAlmostEqual(balances["USDC"]["open_order_reserved"], 1400.0)
        self.assertAlmostEqual(balances["USDC"]["used"], 1400.0)
        self.assertAlmostEqual(balances["USDC"]["free"], 5000.0)
        self.assertAlmostEqual(balances["USDC"]["total"], 6400.0)
        self.assertEqual(
            balances["USDC"]["open_order_reserve_adjustment"],
            "added_to_total",
        )
        self.assertAlmostEqual(balances["ACS"]["open_order_reserved"], 900.0)
        self.assertAlmostEqual(balances["ACS"]["used"], 900.0)
        self.assertAlmostEqual(balances["ACS"]["free"], 10000.0)
        self.assertAlmostEqual(balances["ACS"]["total"], 10900.0)

    async def test_fetch_account_balances_treats_unused_accounts_as_idle(self) -> None:
        class FakeBalanceManager:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                self.calls += 1
                return {}

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
        )
        manager = FakeBalanceManager()

        with patch.dict(os.environ, {}, clear=True):
            payload = await fetch_account_balances_payload(cfg, manager)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checked_account_count"], 0)
        self.assertEqual(manager.calls, 0)
        self.assertEqual(payload["accounts"][0]["status"], "idle")
        self.assertEqual(
            payload["accounts"][0]["balance"]["skipped_reason"],
            "no configured symbols",
        )

    async def test_fetch_account_balances_warns_when_used_account_missing_api_env(
        self,
    ) -> None:
        class FakeBalanceManager:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                self.calls += 1
                return {}

        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
        )
        manager = FakeBalanceManager()

        with patch.dict(os.environ, {}, clear=True):
            payload = await fetch_account_balances_payload(cfg, manager)

        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["checked_account_count"], 0)
        self.assertEqual(manager.calls, 0)
        self.assertEqual(
            payload["accounts"][0]["balance"]["skipped_reason"],
            "api env vars missing",
        )

    async def test_fetch_derivatives_risk_payload_flags_leverage_and_liquidation(
        self,
    ) -> None:
        test_case = self

        class FakeDerivativeManager:
            async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
                return {
                    "free": {"USDT": 800.0},
                    "used": {"USDT": 200.0},
                    "total": {"USDT": 1000.0},
                }

            async def fetch_positions(
                self,
                _: ExchangeConfig,
                symbols: list[str],
            ) -> list[dict[str, object]]:
                test_case.assertEqual(symbols, ["BTC/USDT:USDT"])
                return [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "contracts": 1.0,
                        "contractSize": 1.0,
                        "markPrice": 100.0,
                        "entryPrice": 95.0,
                        "liquidationPrice": 85.0,
                        "leverage": 5.0,
                        "notional": 100.0,
                        "unrealizedPnl": 5.0,
                    }
                ]

            async def fetch_funding_rates(
                self,
                _: list[ExchangeConfig],
                __: dict[str, list[str]],
            ) -> dict[tuple[str, str], float]:
                return {("binance-swap", "BTC/USDT:USDT"): 0.0001}

        cfg = make_config(
            cash_and_carry_pairs=[
                CashAndCarryPair(
                    spot_symbol="BTC/USDT",
                    derivative_symbol="BTC/USDT:USDT",
                )
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="binanceusdm",
                    label="binance-swap",
                    market_type="swap",
                    api_key_env="BINANCE_API_KEY",
                    secret_env="BINANCE_SECRET",
                )
            ],
            risk=RiskConfig(
                max_derivative_leverage=3.0,
                min_liquidation_buffer_pct=20.0,
                max_margin_usage_pct=10.0,
            ),
        )

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "key", "BINANCE_SECRET": "secret"},
            clear=True,
        ):
            payload = await fetch_derivatives_risk_payload(
                cfg,
                FakeDerivativeManager(),
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["checked_account_count"], 1)
        self.assertEqual(payload["position_count"], 1)
        account = payload["accounts"][0]
        self.assertEqual(account["status"], "blocked")
        self.assertAlmostEqual(account["summary"]["margin_usage_pct"], 20.0)
        position = account["positions"][0]
        self.assertEqual(position["status"], "blocked")
        self.assertAlmostEqual(position["liquidation_buffer_pct"], 15.0)
        self.assertEqual(position["funding_rate"], 0.0001)
        self.assertTrue(
            any("leverage" in reason for reason in position["risk_reasons"])
        )
        self.assertTrue(
            any("liquidation buffer" in reason for reason in position["risk_reasons"])
        )

    async def test_fetch_funding_basis_payload_uses_strategy_center_settings(
        self,
    ) -> None:
        class FakeFundingManager:
            async def fetch_order_books(
                self,
                configs: list[ExchangeConfig],
                symbols_by_exchange: dict[str, set[str]],
                depth: int,
            ) -> dict[tuple[str, str], OrderBookSnapshot]:
                self.last_depth = depth
                books: dict[tuple[str, str], OrderBookSnapshot] = {}
                for exchange in configs:
                    for symbol in symbols_by_exchange.get(exchange.key, set()):
                        if exchange.key == "binance-spot":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=99.0, amount=10.0)],
                                asks=[BookLevel(price=101.0, amount=10.0)],
                            )
                        if exchange.key == "binance-swap":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=102.0, amount=10.0)],
                                asks=[BookLevel(price=104.0, amount=10.0)],
                            )
                return books

            async def fetch_funding_rates(
                self,
                _: list[ExchangeConfig],
                __: dict[str, set[str]],
            ) -> dict[tuple[str, str], float]:
                return {("binance-swap", "BTC/USDT:USDT"): 0.0002}

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(id="binance", label="binance-spot", fee_bps=10.0)
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="binanceusdm",
                    label="binance-swap",
                    market_type="swap",
                    fee_bps=5.0,
                )
            ],
        )
        payload = await fetch_funding_basis_payload(
            cfg,
            FakeFundingManager(),
            strategy_center_payload={
                "funding_arbitrage": {
                    "enabled": True,
                    "pair_id": "btc funding",
                    "spot_exchange": "binance-spot",
                    "spot_symbol": "BTC/USDT",
                    "derivative_exchange": "binance-swap",
                    "derivative_symbol": "BTC/USDT:USDT",
                    "min_funding_bps": 1.0,
                    "min_entry_basis_bps": 10.0,
                }
            },
        )

        self.assertEqual(payload["status"], "candidate")
        self.assertEqual(payload["checked_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["paper_execution"]["mode"], "paper")
        self.assertEqual(row["paper_execution"]["state"], "would_open")
        self.assertFalse(row["paper_execution"]["live_enabled"])
        self.assertIn("protection", row["paper_execution"])
        self.assertFalse(row["paper_execution"]["protection"]["live_submit_allowed"])

    async def test_fetch_options_arbitrage_payload_finds_paper_candidate(self) -> None:
        class FakeOptionsManager:
            async def fetch_order_books(
                self,
                configs: list[ExchangeConfig],
                symbols_by_exchange: dict[str, set[str]],
                depth: int,
            ) -> dict[tuple[str, str], OrderBookSnapshot]:
                books: dict[tuple[str, str], OrderBookSnapshot] = {}
                for exchange in configs:
                    for symbol in symbols_by_exchange.get(exchange.key, set()):
                        if symbol == "BTC/USDT":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=99.0, amount=10.0)],
                                asks=[BookLevel(price=100.0, amount=10.0)],
                            )
                        elif symbol == "BTC-100-C":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=8.0, amount=10.0)],
                                asks=[BookLevel(price=8.5, amount=10.0)],
                            )
                        elif symbol == "BTC-100-P":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=1.0, amount=10.0)],
                                asks=[BookLevel(price=1.5, amount=10.0)],
                            )
                return books

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(id="binance", label="binance-spot", fee_bps=0.0)
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="deribit",
                    label="deribit-options",
                    market_type="option",
                    fee_bps=0.0,
                )
            ],
            option_combos=[
                OptionComboConfig(
                    underlying="BTC",
                    spot_exchange="binance-spot",
                    spot_symbol="BTC/USDT",
                    option_exchange="deribit-options",
                    call_symbol="BTC-100-C",
                    put_symbol="BTC-100-P",
                    strike=100.0,
                    contract_size=1.0,
                    quote_currency="USDT",
                )
            ],
            options_arbitrage=OptionsArbitrageConfig(
                enabled=True,
                notional_quote=200.0,
                min_edge_quote=0.1,
                min_edge_bps=1.0,
            ),
        )

        payload = await fetch_options_arbitrage_payload(cfg, FakeOptionsManager())

        self.assertEqual(payload["status"], "candidate")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["parity_candidate_count"], 1)
        self.assertEqual(payload["enhanced_candidate_count"], 0)
        self.assertEqual(payload["checked_count"], 1)
        self.assertEqual(len(payload["option_chain"]), 2)
        self.assertEqual(payload["risk"]["status"], "ok")
        self.assertEqual(payload["risk"]["greeks_available_count"], 0)
        self.assertFalse(payload["execution_controls"]["auto_submit_live_orders"])
        row = payload["rows"][0]
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["paper_execution"]["mode"], "paper")
        self.assertEqual(row["paper_execution"]["state"], "would_open")
        self.assertFalse(row["paper_execution"]["live_enabled"])
        self.assertEqual(row["paper_execution"]["order_ticket"]["order_count"], 3)
        self.assertTrue(
            row["paper_execution"]["order_ticket"]["requires_final_confirmation"]
        )
        self.assertIn("protection", row["paper_execution"])
        self.assertTrue(row["paper_execution"]["protection"]["requires_manual_review"])
        self.assertEqual(
            [leg["side"] for leg in row["paper_execution"]["suggested_legs"]],
            ["sell", "buy", "buy"],
        )

    async def test_fetch_options_arbitrage_payload_blocks_wide_option_spread(
        self,
    ) -> None:
        class FakeOptionsManager:
            async def fetch_order_books(
                self,
                configs: list[ExchangeConfig],
                symbols_by_exchange: dict[str, set[str]],
                depth: int,
            ) -> dict[tuple[str, str], OrderBookSnapshot]:
                books: dict[tuple[str, str], OrderBookSnapshot] = {}
                for exchange in configs:
                    for symbol in symbols_by_exchange.get(exchange.key, set()):
                        if symbol == "BTC/USDT":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=99.0, amount=10.0)],
                                asks=[BookLevel(price=100.0, amount=10.0)],
                            )
                        elif symbol == "BTC-100-C":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=8.0, amount=10.0)],
                                asks=[BookLevel(price=12.0, amount=10.0)],
                            )
                        elif symbol == "BTC-100-P":
                            books[(exchange.key, symbol)] = OrderBookSnapshot(
                                exchange=exchange.key,
                                symbol=symbol,
                                bids=[BookLevel(price=1.0, amount=10.0)],
                                asks=[BookLevel(price=1.5, amount=10.0)],
                            )
                return books

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(id="binance", label="binance-spot", fee_bps=0.0)
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="deribit",
                    label="deribit-options",
                    market_type="option",
                    fee_bps=0.0,
                )
            ],
            option_combos=[
                OptionComboConfig(
                    underlying="BTC",
                    spot_exchange="binance-spot",
                    spot_symbol="BTC/USDT",
                    option_exchange="deribit-options",
                    call_symbol="BTC-100-C",
                    put_symbol="BTC-100-P",
                    strike=100.0,
                    contract_size=1.0,
                    quote_currency="USDT",
                )
            ],
            options_arbitrage=OptionsArbitrageConfig(
                enabled=True,
                notional_quote=200.0,
                min_edge_quote=0.1,
                min_edge_bps=1.0,
                max_option_spread_bps=100.0,
            ),
        )

        payload = await fetch_options_arbitrage_payload(cfg, FakeOptionsManager())

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["risk"]["status"], "blocked")
        self.assertEqual(payload["risk"]["blocked_new_open_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("call: spread", row["preflight_reasons"][0])
        self.assertEqual(row["paper_execution"]["state"], "blocked")
        self.assertEqual(row["paper_execution"]["protection"]["status"], "blocked")
        self.assertFalse(row["paper_execution"]["protection"]["live_submit_allowed"])

    async def test_fetch_options_arbitrage_payload_finds_box_spread_candidate(
        self,
    ) -> None:
        class FakeOptionsManager:
            async def fetch_order_books(
                self,
                configs: list[ExchangeConfig],
                symbols_by_exchange: dict[str, set[str]],
                depth: int,
            ) -> dict[tuple[str, str], OrderBookSnapshot]:
                quotes = {
                    "BTC/USDT": (99.0, 100.0),
                    "BTC-100-C": (7.8, 8.0),
                    "BTC-100-P": (1.0, 1.2),
                    "BTC-110-C": (3.0, 3.2),
                    "BTC-110-P": (3.8, 4.0),
                }
                books: dict[tuple[str, str], OrderBookSnapshot] = {}
                for exchange in configs:
                    for symbol in symbols_by_exchange.get(exchange.key, set()):
                        bid, ask = quotes[symbol]
                        books[(exchange.key, symbol)] = OrderBookSnapshot(
                            exchange=exchange.key,
                            symbol=symbol,
                            bids=[BookLevel(price=bid, amount=10.0)],
                            asks=[BookLevel(price=ask, amount=10.0)],
                        )
                return books

        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(id="binance", label="binance-spot", fee_bps=0.0)
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="deribit",
                    label="deribit-options",
                    market_type="option",
                    fee_bps=0.0,
                )
            ],
            option_combos=[
                OptionComboConfig(
                    underlying="BTC",
                    spot_exchange="binance-spot",
                    spot_symbol="BTC/USDT",
                    option_exchange="deribit-options",
                    call_symbol="BTC-100-C",
                    put_symbol="BTC-100-P",
                    strike=100.0,
                    expiry="2026-12-31",
                    quote_currency="USDT",
                ),
                OptionComboConfig(
                    underlying="BTC",
                    spot_exchange="binance-spot",
                    spot_symbol="BTC/USDT",
                    option_exchange="deribit-options",
                    call_symbol="BTC-110-C",
                    put_symbol="BTC-110-P",
                    strike=110.0,
                    expiry="2026-12-31",
                    quote_currency="USDT",
                ),
            ],
            options_arbitrage=OptionsArbitrageConfig(
                enabled=True,
                notional_quote=200.0,
                min_edge_quote=0.1,
                min_edge_bps=1.0,
            ),
        )

        payload = await fetch_options_arbitrage_payload(cfg, FakeOptionsManager())

        strategy_types = {
            row["strategy_type"] for row in payload["strategy_candidates"]
        }
        self.assertIn("box_spread", strategy_types)
        self.assertGreaterEqual(payload["enhanced_candidate_count"], 1)
        box = next(
            row
            for row in payload["strategy_candidates"]
            if row["strategy_type"] == "box_spread"
        )
        self.assertFalse(box["auto_submit_live_orders"])
        self.assertTrue(box["requires_final_confirmation"])
        self.assertEqual(len(box["legs"]), 4)

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

    async def test_auto_stop_state_survives_paused_poll_until_resume(self) -> None:
        state = MonitorState(make_config(), 1.0)

        stopped = await state.set_auto_stopped(
            reason="daily loss limit breached",
            warnings=["Daily loss exceeded"],
        )
        await state.set_paused()
        still_stopped = await state.get()
        resumed = await state.set_running(True)

        self.assertFalse(stopped["program"]["running"])
        self.assertTrue(stopped["program"]["auto_stopped"])
        self.assertEqual(stopped["status"], "auto_stopped")
        self.assertEqual(still_stopped["status"], "auto_stopped")
        self.assertEqual(
            still_stopped["program"]["stop_reason"],
            "daily loss limit breached",
        )
        self.assertTrue(resumed["program"]["running"])
        self.assertFalse(resumed["program"]["auto_stopped"])
        self.assertIsNone(resumed["program"]["stop_reason"])

    async def test_state_view_payloads_trim_hidden_page_data(self) -> None:
        state = MonitorState(make_config(), 1.0)

        full = await state.get()
        status = await state.get(view="status")
        trading = await state.get(view="trading")
        quant = await state.get(view="quant")
        settings = await state.get(view="settings")
        records = await state.get(view="records")

        self.assertIn("account_balances", full)
        self.assertIn("derivatives", full)
        self.assertIn("funding_basis", full)
        self.assertIn("options_arbitrage", full)
        self.assertIn("contract_strategies", full)
        self.assertIn("execution_protection", full)
        self.assertIn("trading_console", full)
        self.assertIn("recent_opportunities", full)

        self.assertIn("account_balances", status)
        self.assertNotIn("derivatives", status)
        self.assertIn("funding_basis", status)
        self.assertIn("options_arbitrage", status)
        self.assertIn("contract_strategies", status)
        self.assertNotIn("rows", status["contract_strategies"])
        self.assertIn("execution_protection", status)
        self.assertIn("readiness", status)
        self.assertNotIn("trading_console", status)
        self.assertNotIn("recent_opportunities", status)

        self.assertIn("config", trading["market_maker"])
        self.assertIn("spot_markets", trading["config"])
        self.assertNotIn("account_balances", trading)
        self.assertNotIn("trading_console", trading)

        self.assertIn("derivatives", quant)
        self.assertIn("accounts", quant["derivatives"])
        self.assertIn("rows", quant["funding_basis"])
        self.assertIn("rows", quant["options_arbitrage"])
        self.assertIn("rows", quant["contract_strategies"])
        self.assertIn("config", quant["spot_grid"])
        self.assertIn("config", quant["dca"])
        self.assertIn("config", quant["execution_algo"])

        self.assertIn("trading_console", settings)
        self.assertNotIn("account_balances", settings)
        self.assertNotIn("derivatives", settings)
        self.assertIn("funding_basis", settings)
        self.assertNotIn("rows", settings["funding_basis"])
        self.assertIn("options_arbitrage", settings)
        self.assertNotIn("rows", settings["options_arbitrage"])
        self.assertIn("contract_strategies", settings)
        self.assertNotIn("rows", settings["contract_strategies"])
        self.assertIn("execution_protection", settings)
        self.assertNotIn("rows", settings["execution_protection"])
        self.assertNotIn("readiness", settings)
        self.assertIn("risk", settings["operations"])
        self.assertNotIn("trade_log", settings["operations"])

        self.assertIn("trading_console", records)
        self.assertIn("order_activity", records)
        self.assertIn("trade_log", records["operations"])
        self.assertNotIn("account_balances", records)
        self.assertNotIn("readiness", records)

    async def test_state_view_payloads_compact_auto_buy_sell_task_history(self) -> None:
        state = MonitorState(make_config(), 1.0)
        await state.set_auto_buy_sell_tasks(
            {
                "status": "ok",
                "path": "/tmp/tasks.json",
                "task_count": 1,
                "active_count": 1,
                "updated_at": 123.0,
                "tasks": [
                    {
                        "id": "task-1",
                        "status": "running",
                        "config": {
                            "exchange": "binance-usdm",
                            "symbol": "ACS/USDT:USDT",
                            "instrument_type": "perpetual",
                            "side": "buy",
                            "position_effect": "reduce_only",
                            "position_side": "short",
                            "position_mode": "one_way",
                            "margin_mode": "isolated",
                            "leverage": 1.0,
                            "max_position_quote": 25.0,
                            "total_quote": 10.0,
                            "price_mode": "taker",
                        },
                        "filled_quote": 1.5,
                        "remaining_quote": 8.5,
                        "progress_pct": 15.0,
                        "open_order_count": 1,
                        "last_execution": {
                            "placed_count": 1,
                            "contract_size": 10.0,
                            "contracts": 100.0,
                            "base_amount": 1000.0,
                            "quote_currency": "USDC",
                            "settle_currency": "USDC",
                            "order_params": {"reduceOnly": True},
                        },
                        "placed_order_ids": [f"order-{i}" for i in range(100)],
                        "known_trade_ids": [f"trade-{i}" for i in range(100)],
                        "order_created_at": {f"order-{i}": 123.0 for i in range(100)},
                    }
                ],
            }
        )

        full_task = (await state.get())["slow_execution"]["tasks"]["tasks"][0]
        view_task = (await state.get(view="trading"))["slow_execution"]["tasks"][
            "tasks"
        ][0]

        self.assertIn("placed_order_ids", full_task)
        self.assertIn("known_trade_ids", full_task)
        self.assertNotIn("placed_order_ids", view_task)
        self.assertNotIn("known_trade_ids", view_task)
        self.assertNotIn("order_created_at", view_task)
        self.assertEqual(view_task["config"]["exchange"], "binance-usdm")
        self.assertEqual(view_task["config"]["price_mode"], "taker")
        self.assertEqual(view_task["config"]["instrument_type"], "perpetual")
        self.assertEqual(view_task["config"]["position_effect"], "reduce_only")
        self.assertEqual(view_task["config"]["position_side"], "short")
        self.assertEqual(view_task["config"]["margin_mode"], "isolated")
        self.assertEqual(view_task["config"]["max_position_quote"], 25.0)
        self.assertIn("total_quote", view_task["config"])
        self.assertEqual(view_task["last_execution"]["contract_size"], 10.0)
        self.assertEqual(view_task["last_execution"]["contracts"], 100.0)
        self.assertEqual(view_task["last_execution"]["quote_currency"], "USDC")
        self.assertEqual(view_task["last_execution"]["settle_currency"], "USDC")
        self.assertTrue(view_task["last_execution"]["order_params"]["reduceOnly"])

    async def test_trading_view_includes_compact_market_limits(self) -> None:
        payload = {
            "status": "running",
            "config": {"spot_markets": []},
            "account_balances": {
                "accounts": [
                    {
                        "exchange": "bithumb-spot",
                        "label": "bithumb-spot",
                        "market_type": "spot",
                        "balance": {"currencies": [{"currency": "KRW", "total": 1}]},
                        "markets": [
                            {
                                "exchange": "bithumb-spot",
                                "symbol": "ACS/KRW",
                                "status": "ok",
                                "market": {
                                    "symbol": "ACS/KRW",
                                    "limits": {
                                        "amount_min": 1.0,
                                        "cost_min": 5000.0,
                                    },
                                    "precision": {"price": 0.0001},
                                },
                            }
                        ],
                    }
                ],
                "totals": [{"currency": "KRW", "total": 1}],
            },
        }

        trading = state_payload_for_view(payload, "trading", sections="slow-orders")

        self.assertNotIn("account_balances", trading)
        self.assertEqual(trading["market_limits"][0]["exchange"], "bithumb-spot")
        self.assertEqual(trading["market_limits"][0]["limits"]["cost_min"], 5000.0)

    async def test_program_state_persists_in_runtime_store(self) -> None:
        cfg = make_config()

        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "web_runtime_overrides.json")
            paused_state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            await paused_state.set_running(False)
            restored_paused = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            paused_payload = await restored_paused.get()

            stopped_state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            await stopped_state.set_auto_stopped(
                reason="repeated degraded cycles: 3",
                warnings=["Auto-stop triggered"],
            )
            restored_stopped = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            stopped_payload = await restored_stopped.get()

        self.assertFalse(await restored_paused.is_running())
        self.assertEqual(paused_payload["status"], "paused")
        self.assertFalse(paused_payload["program"]["running"])
        self.assertFalse(await restored_stopped.is_running())
        self.assertEqual(stopped_payload["status"], "auto_stopped")
        self.assertTrue(stopped_payload["program"]["auto_stopped"])
        self.assertEqual(
            stopped_payload["program"]["stop_reason"],
            "repeated degraded cycles: 3",
        )

    async def test_startup_guard_tracks_only_unverified_current_config(self) -> None:
        cfg = make_config()

        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "web_runtime_overrides.json")
            state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            initial = await state.config_versions(limit=5)
            await state.set_risk_overrides(
                {"max_order_quote": 25.0},
                cfg=cfg,
                actor_email="operator@example.com",
            )
            pending = await state.startup_config_guard_candidate()

            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(
                pending["previous_known_good_id"],
                initial["current_version_id"],
            )
            self.assertIsNone(
                await state.mark_current_config_known_good(
                    expected_current_hash="stale-hash",
                )
            )
            self.assertIsNotNone(
                await state.mark_current_config_known_good(
                    expected_current_hash=pending["hash"],
                )
            )
            self.assertIsNone(await state.startup_config_guard_candidate())

    async def test_runtime_configuration_rolls_back_to_known_good_snapshot(
        self,
    ) -> None:
        cfg = make_config(risk=RiskConfig(max_order_quote=5.0))

        with tempfile.TemporaryDirectory() as tmp:
            state = MonitorState(
                cfg,
                1.0,
                runtime_store_path=os.path.join(tmp, "runtime.json"),
            )
            baseline = await state.config_versions(limit=5)
            await state.set_risk_overrides(
                {"max_order_quote": 25.0},
                cfg=cfg,
                actor_email="operator@example.com",
            )
            changed = await state.config_versions(limit=5)

            with self.assertRaisesRegex(ValueError, "configuration changed"):
                await state.rollback_config_version(
                    baseline["current_version_id"],
                    expected_current_hash="stale-hash",
                    actor_email="operator@example.com",
                )
            result = await state.rollback_config_version(
                baseline["current_version_id"],
                expected_current_hash=changed["current_hash"],
                actor_email="operator@example.com",
            )
            runtime = await state.runtime_config(cfg)
            versions = await state.config_versions(limit=5)

        self.assertTrue(result["ok"])
        self.assertEqual(runtime.risk.max_order_quote, 5.0)
        self.assertTrue(versions["versions"][0]["known_good"])

    async def test_runtime_configuration_rejects_unverified_live_rollback(
        self,
    ) -> None:
        cfg = make_config(risk=RiskConfig(allow_live_trading=False))

        with tempfile.TemporaryDirectory() as tmp:
            state = MonitorState(
                cfg,
                1.0,
                runtime_store_path=os.path.join(tmp, "runtime.json"),
            )
            await state.set_risk_overrides(
                {"allow_live_trading": True},
                cfg=cfg,
                actor_email="operator@example.com",
            )
            live_version = await state.config_versions(limit=5)
            await state.set_risk_overrides(
                {"allow_live_trading": False},
                cfg=cfg,
                actor_email="operator@example.com",
            )
            safe_version = await state.config_versions(limit=5)

            with self.assertRaisesRegex(ValueError, "unverified configuration"):
                await state.rollback_config_version(
                    live_version["current_version_id"],
                    expected_current_hash=safe_version["current_hash"],
                    actor_email="operator@example.com",
                )
            runtime = await state.runtime_config(cfg)

        self.assertFalse(runtime.risk.allow_live_trading)

    async def test_market_update_changes_runtime_spot_markets(self) -> None:
        cfg = make_config(
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
        )
        state = MonitorState(cfg, 1.0)

        update = await state.set_spot_markets(
            [
                SpotMarketConfig(
                    asset="BTC",
                    exchange="bybit-spot",
                    symbol="BTC/USDT",
                    quote_currency="USDT",
                )
            ],
            cfg=cfg,
        )
        runtime_cfg = await state.runtime_config(cfg)
        payload = await state.get()

        self.assertEqual(runtime_cfg.spot_markets[0].asset, "BTC")
        self.assertEqual(payload["config"]["spot_markets"][0]["symbol"], "BTC/USDT")
        self.assertEqual(
            update["market_maker"]["accounts"][0]["symbols"],
            ["BTC/USDT"],
        )

    async def test_market_maker_payload_includes_configured_bybit_symbol(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            spot_markets=[],
        )

        payload = build_market_maker_payload(cfg, {})
        accounts = {row["key"]: row for row in payload["accounts"]}

        self.assertIn("bybit-spot", accounts)
        self.assertIn("ACS/USDT", accounts["bybit-spot"]["symbols"])

    async def test_market_maker_payload_keeps_base_symbols_after_market_override(
        self,
    ) -> None:
        base_cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                ),
            ],
        )
        runtime_cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
            ),
            spot_exchanges=base_cfg.spot_exchanges,
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
        )

        payload = build_market_maker_payload(runtime_cfg, {}, base_cfg=base_cfg)
        accounts = {row["key"]: row for row in payload["accounts"]}

        self.assertIn("ACS/USDT", accounts["bybit-spot"]["symbols"])
        self.assertIn("ACS/USDC", accounts["coinbase-spot"]["symbols"])

    async def test_market_update_keeps_base_symbols_for_market_maker(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                ),
            ],
        )
        state = MonitorState(cfg, 1.0)

        update = await state.set_spot_markets(
            [
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
            cfg=cfg,
        )
        accounts = {row["key"]: row for row in update["market_maker"]["accounts"]}

        self.assertIn("ACS/USDT", accounts["bybit-spot"]["symbols"])
        self.assertIn("ACS/USDC", accounts["coinbase-spot"]["symbols"])

    async def test_grid_and_dca_runtime_overrides_persist(self) -> None:
        cfg = make_config(
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "web_runtime_overrides.json")
            state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            await state.set_spot_grid_overrides(
                {
                    "enabled": True,
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
                    "lower_price": 0.0001,
                    "upper_price": 0.0002,
                    "grid_count": 12,
                },
                cfg=cfg,
            )
            await state.set_dca_overrides(
                {
                    "enabled": True,
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
                    "quote_per_order": 2.0,
                    "max_orders": 6,
                },
                cfg=cfg,
            )
            await state.set_execution_algo_overrides(
                {
                    "enabled": True,
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
                    "algo": "vwap",
                    "total_quote": 25.0,
                },
                cfg=cfg,
            )
            await state.set_backtest_overrides(
                {
                    "enabled": True,
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
                    "strategy": "execution_algo",
                    "step_count": 50,
                },
                cfg=cfg,
            )

            restored = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            runtime_cfg = await restored.runtime_config(cfg)
            payload = await restored.get(view="quant")

        self.assertTrue(runtime_cfg.spot_grid.enabled)
        self.assertEqual(runtime_cfg.spot_grid.grid_count, 12)
        self.assertTrue(runtime_cfg.dca.enabled)
        self.assertEqual(runtime_cfg.dca.max_orders, 6)
        self.assertTrue(runtime_cfg.execution_algo.enabled)
        self.assertEqual(runtime_cfg.execution_algo.algo, "vwap")
        self.assertTrue(runtime_cfg.backtest.enabled)
        self.assertEqual(runtime_cfg.backtest.strategy, "execution_algo")
        self.assertEqual(payload["spot_grid"]["config"]["symbol"], "ACS/USDT")
        self.assertEqual(payload["dca"]["config"]["quote_per_order"], 2.0)
        self.assertEqual(payload["execution_algo"]["config"]["total_quote"], 25.0)
        self.assertEqual(payload["backtest"]["config"]["step_count"], 50)

    async def test_cash_and_carry_update_changes_runtime_pairs(self) -> None:
        cfg = make_config(
            spot_exchanges=[ExchangeConfig(id="binance", label="binance-spot")],
            derivative_exchanges=[
                ExchangeConfig(
                    id="binanceusdm",
                    label="binance-swap",
                    market_type="swap",
                )
            ],
        )
        state = MonitorState(cfg, 1.0)

        update = await state.set_cash_and_carry_pairs(
            [
                CashAndCarryPair(
                    spot_symbol="BTC/USDT",
                    derivative_symbol="BTC/USDT:USDT",
                )
            ],
            cfg=cfg,
        )
        runtime_cfg = await state.runtime_config(cfg)
        payload = await state.get()

        self.assertEqual(runtime_cfg.cash_and_carry_pairs[0].spot_symbol, "BTC/USDT")
        self.assertEqual(
            payload["config"]["cash_and_carry_pairs"][0]["derivative_symbol"],
            "BTC/USDT:USDT",
        )
        mm_accounts = {row["key"]: row for row in payload["market_maker"]["accounts"]}
        self.assertIn("BTC/USDT:USDT", mm_accounts["binance-swap"]["symbols"])
        strategies = {row["id"]: row for row in update["trading_console"]["strategies"]}
        self.assertTrue(strategies["cash_and_carry"]["configured"])

    async def test_strategy_pause_updates_trading_console(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(enabled=True, exchange="bybit-spot"),
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
        )
        state = MonitorState(cfg, 1.0)

        console = await state.set_strategy_paused(
            "market_maker",
            True,
            cfg=cfg,
        )

        strategies = {row["id"]: row for row in console["strategies"]}
        self.assertTrue(strategies["market_maker"]["paused"])
        self.assertEqual(strategies["market_maker"]["mode"], "paused")

    async def test_risk_api_requires_button_confirmation_for_new_live_switches(
        self,
    ) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/risk"
            method = "POST"

            def __init__(
                self,
                app: dict[str, object],
                payload: dict[str, object],
            ) -> None:
                self.app = app
                self._payload = payload

            def get(self, key: str, default: object = None) -> object:
                return default

            async def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=str(Path(tmp) / "trades.jsonl"),
                ),
                risk=RiskConfig(allow_live_trading=False),
            )
            state = MonitorState(
                cfg,
                1.0,
                runtime_store_path=str(Path(tmp) / "runtime.json"),
            )
            app: dict[str, object] = {"monitor_state": state, "config": cfg}
            payload: dict[str, object] = {
                "allow_live_trading": True,
                "auto_hedge_live_enabled": True,
                "max_auto_hedge_quote": 5.0,
                "auto_hedge_max_attempts": 1,
            }

            denied = await api_risk(  # type: ignore[arg-type]
                FakeRequest(app, payload)
            )
            approved = await api_risk(  # type: ignore[arg-type]
                FakeRequest(app, {**payload, "confirm_live_risk": True})
            )
            runtime = await state.runtime_config(cfg)

        self.assertEqual(denied.status, 400)
        self.assertIn("confirm_live_risk", denied.text)
        self.assertEqual(approved.status, 200)
        self.assertTrue(runtime.risk.allow_live_trading)
        self.assertTrue(runtime.risk.auto_hedge_live_enabled)

    async def test_risk_update_updates_runtime_config_and_console(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            risk=RiskConfig(allow_live_trading=False, max_order_quote=5.0),
        )
        state = MonitorState(cfg, 1.0)

        update = await state.set_risk_overrides(
            {
                "allow_live_trading": True,
                "max_order_quote": 1.25,
                "account_enabled": {"bybit-spot": False},
                "strategy_enabled": {"market_maker": False},
            },
            cfg=cfg,
        )
        runtime_risk = await state.risk_config(cfg.risk)
        payload = await state.get()

        self.assertTrue(runtime_risk.allow_live_trading)
        self.assertEqual(runtime_risk.max_order_quote, 1.25)
        self.assertFalse(runtime_risk.account_enabled["bybit-spot"])
        self.assertFalse(runtime_risk.strategy_enabled["market_maker"])
        strategies = {row["id"]: row for row in update["trading_console"]["strategies"]}
        accounts = {row["key"]: row for row in update["trading_console"]["accounts"]}
        self.assertFalse(strategies["market_maker"]["live"])
        self.assertFalse(accounts["bybit-spot"]["enabled"])
        self.assertEqual(payload["operations"]["risk"]["max_order_quote"], 1.25)

    async def test_risk_update_merges_partial_account_and_strategy_maps(self) -> None:
        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            risk=RiskConfig(
                account_enabled={"bybit-spot": True, "coinbase-spot": True},
                strategy_enabled={
                    "market_maker": True,
                    "slow_execution": False,
                    "spot_spread": True,
                },
            ),
        )
        state = MonitorState(cfg, 1.0)

        await state.set_risk_overrides(
            {
                "account_enabled": {"bybit-spot": False},
                "strategy_enabled": {"slow_execution": True},
            },
            cfg=cfg,
        )
        runtime_risk = await state.risk_config(cfg.risk)

        self.assertFalse(runtime_risk.account_enabled["bybit-spot"])
        self.assertTrue(runtime_risk.account_enabled["coinbase-spot"])
        self.assertTrue(runtime_risk.strategy_enabled["market_maker"])
        self.assertTrue(runtime_risk.strategy_enabled["slow_execution"])
        self.assertTrue(runtime_risk.strategy_enabled["spot_spread"])

    async def test_market_maker_update_updates_runtime_config_and_console(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                live_enabled=False,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            risk=RiskConfig(allow_live_trading=True, allow_market_maker=True),
        )
        state = MonitorState(cfg, 1.0)

        update = await state.set_market_maker_overrides(
            {
                "live_enabled": True,
                "levels": 4,
                "quote_per_level": 2.0,
                "depth_shape": "flat",
            },
            cfg=cfg,
        )
        runtime_cfg = await state.runtime_config(cfg)

        self.assertTrue(runtime_cfg.market_maker.live_enabled)
        self.assertEqual(runtime_cfg.market_maker.levels, 4)
        self.assertEqual(runtime_cfg.market_maker.depth_shape, "flat")
        self.assertEqual(update["config"]["quote_per_level"], 2.0)
        strategies = {row["id"]: row for row in update["trading_console"]["strategies"]}
        self.assertTrue(strategies["market_maker"]["live"])

    async def test_control_updates_do_not_reload_operations_logs(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                live_enabled=False,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[ExchangeConfig(id="bybit", label="bybit-spot")],
            risk=RiskConfig(
                allow_live_trading=True,
                allow_market_maker=True,
                max_order_quote=2.0,
            ),
        )
        state = MonitorState(cfg, 1.0)

        with patch(
            "arbitrage_bot.web.state.build_operations_payload",
            side_effect=AssertionError("control update reloaded operations logs"),
        ):
            mm_update = await state.set_market_maker_overrides(
                {"levels": 4},
                cfg=cfg,
            )
            risk_update = await state.set_risk_overrides(
                {"max_order_quote": 7.0},
                cfg=cfg,
            )

        self.assertEqual(mm_update["config"]["levels"], 4)
        self.assertEqual(risk_update["operations"]["risk"]["max_order_quote"], 7.0)

    async def test_strategy_preflight_payload_is_compact(self) -> None:
        cfg = make_config()
        state = MonitorState(cfg, 1.0)
        state._payload.update(
            {
                "quote_rates": {"USD": 1.0},
                "markets": [{"exchange": "coinbase-spot"}],
                "account_balances": {"accounts": []},
                "order_activity": {"open_orders": []},
                "market_maker": {"runtime": {"instances": []}},
                "slow_execution": {"tasks": {"tasks": []}},
                "operations": {"strategy_timeline": ["large-history"]},
                "onchain": {"holders": ["large-history"]},
            }
        )

        payload = await state.strategy_preflight_payload()

        self.assertEqual(payload["quote_rates"], {"USD": 1.0})
        self.assertIn("market_maker", payload)
        self.assertNotIn("operations", payload)
        self.assertNotIn("onchain", payload)

    async def test_market_maker_instances_persist_across_state_restart(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                id="bybit-acs",
                enabled=True,
                live_enabled=False,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                ),
            ],
        )
        instances = [
            MarketMakerConfig(
                id="coinbase-acs",
                enabled=True,
                live_enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                levels=20,
                quote_per_level=100.0,
            ),
            MarketMakerConfig(
                id="bybit-acs",
                enabled=True,
                live_enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                levels=20,
                quote_per_level=50.0,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "web_runtime_overrides.json")
            state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            await state.set_market_maker_instances(instances, cfg=cfg)

            restored = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            runtime_cfg = await restored.runtime_config(cfg)
            payload = await restored.get()

        self.assertEqual(
            [(item.exchange, item.symbol) for item in runtime_cfg.market_makers],
            [("coinbase-spot", "ACS/USDC"), ("bybit-spot", "ACS/USDT")],
        )
        self.assertEqual(len(payload["market_maker"]["instances"]), 2)
        self.assertTrue(payload["runtime_store"]["loaded"])
        self.assertIsNone(payload["runtime_store"]["error"])

    async def test_runtime_overrides_persist_across_state_restart(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                live_enabled=False,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            risk=RiskConfig(allow_live_trading=False),
        )

        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "web_runtime_overrides.json")
            state = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            await state.set_risk_overrides(
                {
                    "allow_live_trading": True,
                    "max_order_quote": 1.25,
                    "account_enabled": {"bybit-spot": False},
                },
                cfg=cfg,
            )
            await state.set_market_maker_overrides(
                {"live_enabled": True, "levels": 4},
                cfg=cfg,
            )
            await state.set_slow_execution_overrides(
                {
                    "enabled": True,
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "side": "buy",
                },
                cfg=cfg,
            )
            await state.set_spot_markets(
                [
                    SpotMarketConfig(
                        asset="BTC",
                        exchange="bybit-spot",
                        symbol="BTC/USDT",
                        quote_currency="USDT",
                    )
                ],
                cfg=cfg,
            )
            await state.set_strategy_paused("market_maker", True, cfg=cfg)

            restored = MonitorState(cfg, 1.0, runtime_store_path=store_path)
            restored_cfg = await restored.runtime_config(cfg)
            pauses = await restored.strategy_pauses()
            payload = await restored.get()

        self.assertTrue(restored_cfg.risk.allow_live_trading)
        self.assertEqual(restored_cfg.risk.max_order_quote, 1.25)
        self.assertFalse(restored_cfg.risk.account_enabled["bybit-spot"])
        self.assertTrue(restored_cfg.market_maker.live_enabled)
        self.assertEqual(restored_cfg.market_maker.levels, 4)
        self.assertTrue(restored_cfg.slow_execution.enabled)
        self.assertEqual(restored_cfg.slow_execution.exchange, "coinbase-spot")
        self.assertEqual(restored_cfg.spot_markets[0].symbol, "BTC/USDT")
        self.assertTrue(pauses["market_maker"])
        self.assertTrue(payload["runtime_store"]["loaded"])
        self.assertIsNone(payload["runtime_store"]["error"])

    async def test_rebalance_startup_loads_runtime_with_effective_overrides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_path = str(Path(tmp) / "base-runtime.json")
            effective_path = str(Path(tmp) / "effective-runtime.json")
            cfg = make_config(
                cross_exchange_rebalance=CrossExchangeRebalanceConfig(
                    runtime_path=base_path,
                )
            )
            state = MonitorState(cfg, 1.0)
            await state.set_cross_exchange_rebalance_overrides(
                {
                    "enabled": True,
                    "live_enabled": True,
                    "buy_exchange": "coinbase-spot",
                    "buy_symbol": "ACS/USDC",
                    "sell_exchange": "bithumb-spot",
                    "sell_symbol": "ACS/KRW",
                    "total_quote_common": 100.0,
                    "runtime_path": effective_path,
                },
                cfg=cfg,
            )
            runtime_cfg = await state.runtime_config(cfg)
            stored = new_rebalance_runtime(
                runtime_cfg.cross_exchange_rebalance,
                common_quote_currency=runtime_cfg.common_quote_currency,
            )
            stored.update(
                {
                    "status": "waiting_for_cost",
                    "completed_quote_common": 25.0,
                    "remaining_quote_common": 75.0,
                    "progress_pct": 25.0,
                    "cycle_count": 58,
                    "live_cycle_count": 3,
                }
            )
            save_rebalance_runtime(effective_path, stored)

            loaded_path, loaded = await _load_initial_rebalance_runtime(cfg, state)

        self.assertEqual(loaded_path, effective_path)
        self.assertEqual(loaded["completed_quote_common"], 25.0)
        self.assertEqual(loaded["cycle_count"], 58)
        self.assertEqual(loaded["live_cycle_count"], 3)

    async def test_admin_users_endpoint_rejects_non_admin_callers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            store = WebUserStore(store_path)
            store.create_user(email="admin@example.com", password="Strong-pass-1!")
            member = store.create_user(
                email="member@example.com",
                password="Strong-pass-2!",
                allowed_assets=["ACS"],
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": member.email,
                        "password": "Strong-pass-2!",
                        "totp": totp_code(member.totp_secret),
                    },
                )
                responses = {}
                for action, body in (
                    ("list", {"action": "list"}),
                    (
                        "create_user",
                        {
                            "action": "create_user",
                            "email": "new@example.com",
                            "password": "Strong-pass-3!",
                        },
                    ),
                    (
                        "update_user",
                        {
                            "action": "update_user",
                            "email": member.email,
                            "role": "admin",
                        },
                    ),
                    (
                        "delete_user",
                        {"action": "delete_user", "email": member.email},
                    ),
                ):
                    response = await client.post("/api/admin/users", json=body)
                    responses[action] = (response.status, await response.json())
            finally:
                await client.close()

        for action, (status, payload) in responses.items():
            self.assertEqual(status, 403, (action, payload))

    async def test_admin_users_endpoint_create_user_rejects_duplicate_email_and_weak_password(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            store = WebUserStore(store_path)
            admin = store.create_user(
                email="admin@example.com", password="Strong-pass-1!"
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": admin.email,
                        "password": "Strong-pass-1!",
                        "totp": totp_code(admin.totp_secret),
                    },
                )
                duplicate_response = await client.post(
                    "/api/admin/users",
                    json={
                        "action": "create_user",
                        "email": admin.email,
                        "password": "Strong-pass-3!",
                    },
                )
                duplicate_payload = await duplicate_response.json()

                weak_password_response = await client.post(
                    "/api/admin/users",
                    json={
                        "action": "create_user",
                        "email": "weak@example.com",
                        "password": "short",
                    },
                )
                weak_password_payload = await weak_password_response.json()
            finally:
                await client.close()

        self.assertEqual(duplicate_response.status, 400, duplicate_payload)
        self.assertEqual(weak_password_response.status, 400, weak_password_payload)

    async def test_admin_users_appear_in_state_payload_for_admin_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            store = WebUserStore(store_path)
            admin = store.create_user(
                email="admin@example.com", password="Strong-pass-1!"
            )
            member = store.create_user(
                email="member@example.com",
                password="Strong-pass-2!",
                allowed_assets=["ACS"],
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": admin.email,
                        "password": "Strong-pass-1!",
                        "totp": totp_code(admin.totp_secret),
                    },
                )
                admin_state = await (await client.get("/api/state")).json()
                admin_settings_view = await (
                    await client.get("/api/state?view=settings")
                ).json()
                admin_status_view = await (
                    await client.get("/api/state?view=status")
                ).json()
                await client.get("/logout")

                await client.post(
                    "/login",
                    data={
                        "email": member.email,
                        "password": "Strong-pass-2!",
                        "totp": totp_code(member.totp_secret),
                    },
                )
                member_state = await (await client.get("/api/state")).json()
            finally:
                await client.close()

        self.assertIn(
            admin.email,
            [row["email"] for row in admin_state["admin_users"]],
        )
        self.assertIn(
            admin.email,
            [row["email"] for row in admin_settings_view["admin_users"]],
        )
        # The high-frequency "status" poll view skips the user-store read;
        # admin_users is only computed for the unfiltered or settings view.
        self.assertNotIn("admin_users", admin_status_view)
        self.assertNotIn("admin_users", member_state)

    async def test_admin_users_endpoint_create_update_delete_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            store = WebUserStore(store_path)
            admin = store.create_user(
                email="admin@example.com", password="Strong-pass-1!"
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": admin.email,
                        "password": "Strong-pass-1!",
                        "totp": totp_code(admin.totp_secret),
                    },
                )

                create_response = await client.post(
                    "/api/admin/users",
                    json={
                        "action": "create_user",
                        "email": "trader@example.com",
                        "password": "Strong-pass-3!",
                        "allowed_assets": ["ACS", "BTC"],
                        "preferred_asset": "ACS",
                    },
                )
                create_payload = await create_response.json()

                # Partial update: change only the role; assets must survive untouched.
                role_response = await client.post(
                    "/api/admin/users",
                    json={
                        "action": "update_user",
                        "email": "trader@example.com",
                        "role": "admin",
                    },
                )
                role_payload = await role_response.json()

                # Partial update: change only the preferred asset; the allowed list
                # must be preserved rather than wiped by the omitted field.
                asset_response = await client.post(
                    "/api/admin/users",
                    json={
                        "action": "update_user",
                        "email": "trader@example.com",
                        "preferred_asset": "BTC",
                    },
                )
                asset_payload = await asset_response.json()

                no_op_response = await client.post(
                    "/api/admin/users",
                    json={"action": "update_user", "email": "trader@example.com"},
                )
                no_op_payload = await no_op_response.json()

                delete_response = await client.post(
                    "/api/admin/users",
                    json={"action": "delete_user", "email": "trader@example.com"},
                )
                delete_payload = await delete_response.json()

                list_response = await client.post(
                    "/api/admin/users", json={"action": "list"}
                )
                list_payload = await list_response.json()
            finally:
                await client.close()

        self.assertEqual(create_response.status, 200, create_payload)
        created_row = next(
            row
            for row in create_payload["users"]
            if row["email"] == "trader@example.com"
        )
        self.assertEqual(created_row["allowed_assets"], ["ACS", "BTC"])
        self.assertEqual(created_row["preferred_asset"], "ACS")

        self.assertEqual(role_response.status, 200, role_payload)
        role_row = next(
            row for row in role_payload["users"] if row["email"] == "trader@example.com"
        )
        self.assertEqual(role_row["role"], "admin")
        self.assertEqual(role_row["allowed_assets"], ["ACS", "BTC"])
        self.assertEqual(role_row["preferred_asset"], "ACS")

        self.assertEqual(asset_response.status, 200, asset_payload)
        asset_row = next(
            row
            for row in asset_payload["users"]
            if row["email"] == "trader@example.com"
        )
        self.assertEqual(asset_row["allowed_assets"], ["ACS", "BTC"])
        self.assertEqual(asset_row["preferred_asset"], "BTC")

        self.assertEqual(no_op_response.status, 400, no_op_payload)

        self.assertEqual(delete_response.status, 200, delete_payload)
        self.assertEqual(list_response.status, 200, list_payload)
        self.assertNotIn(
            "trader@example.com",
            [row["email"] for row in list_payload["users"]],
        )

    async def test_email_registration_login_and_password_reset_flow(self) -> None:
        class CapturingEmailSender:
            def __init__(self) -> None:
                self.codes: dict[tuple[str, str], str] = {}

            def configured(self) -> bool:
                return True

            async def send_code(
                self,
                *,
                email: str,
                code: str,
                purpose: str,
            ) -> None:
                self.codes[(email, purpose)] = code

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                    registration_enabled=True,
                    bootstrap_admin_email_env="TEST_BOOTSTRAP_ADMIN_EMAIL",
                    registration_code_env=None,
                    verification_resend_seconds=10,
                ),
            )
            env_patch = patch.dict(
                os.environ,
                {"TEST_BOOTSTRAP_ADMIN_EMAIL": "trader@example.com"},
            )
            env_patch.start()
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            sender = CapturingEmailSender()
            app["verification_email_sender"] = sender
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                denied_code_response = await client.post(
                    "/register/code",
                    data={
                        "email": "attacker@example.com",
                        "username": "attacker01",
                    },
                )
                code_response = await client.post(
                    "/register/code",
                    data={
                        "email": "trader@example.com",
                        "username": "trader01",
                    },
                )
                registration_code = sender.codes[("trader@example.com", "register")]
                register_response = await client.post(
                    "/register",
                    data={
                        "email": "trader@example.com",
                        "username": "trader01",
                        "verification_code": registration_code,
                        "password": "Strong-pass-1!",
                        "password_confirm": "Strong-pass-1!",
                    },
                )
                login_response = await client.post(
                    "/login",
                    data={
                        "username": "trader01",
                        "password": "Strong-pass-1!",
                    },
                )
                logged_in_state = await (await client.get("/api/state")).json()

                reset_code_response = await client.post(
                    "/forgot-password/code",
                    data={"email": "trader@example.com"},
                )
                reset_code = sender.codes[("trader@example.com", "password_reset")]
                reset_response = await client.post(
                    "/reset-password",
                    data={
                        "email": "trader@example.com",
                        "verification_code": reset_code,
                        "password": "Strong-pass-2!",
                        "password_confirm": "Strong-pass-2!",
                    },
                )
                expired_session_response = await client.get("/api/state")
                new_login_response = await client.post(
                    "/login",
                    data={
                        "username": "trader01",
                        "password": "Strong-pass-2!",
                    },
                )
            finally:
                await client.close()
                env_patch.stop()

        self.assertEqual(denied_code_response.status, 403)
        self.assertEqual(code_response.status, 200)
        self.assertEqual(register_response.status, 200)
        self.assertEqual(login_response.status, 200)
        self.assertEqual(logged_in_state["auth"]["username"], "trader01")
        self.assertEqual(reset_code_response.status, 200)
        self.assertEqual(reset_response.status, 200)
        self.assertEqual(expired_session_response.status, 401)
        self.assertEqual(new_login_response.status, 200)

    async def test_user_totp_setup_login_and_disable_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            trade_log_path = data_dir / "trade_events.jsonl"
            store = WebUserStore(store_path)
            user = store.create_user(
                email="secure@example.com",
                username="secure-user",
                password="Strong-pass-1!",
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                    totp_issuer="Test Trading",
                ),
                trade_log=TradeLogConfig(path=str(trade_log_path)),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                unauthorized = await client.get("/security", allow_redirects=False)
                login = await client.post(
                    "/login",
                    data={
                        "username": user.username,
                        "password": "Strong-pass-1!",
                    },
                    allow_redirects=False,
                )
                setup = await client.get("/security")
                setup_html = await setup.text()
                invalid = await client.post(
                    "/security",
                    data={
                        "action": "enable",
                        "password": "Strong-pass-1!",
                        "totp": "123",
                    },
                )
                enabled = await client.post(
                    "/security",
                    data={
                        "action": "enable",
                        "password": "Strong-pass-1!",
                        "totp": totp_code(user.totp_secret),
                    },
                )
                expired_session = await client.get("/api/state")
                no_code_login = await client.post(
                    "/login",
                    data={
                        "username": user.username,
                        "password": "Strong-pass-1!",
                    },
                    allow_redirects=False,
                )
                enabled_user = store.get_user(user.email)
                totp_login = await client.post(
                    "/login",
                    data={
                        "username": user.username,
                        "password": "Strong-pass-1!",
                        "totp": totp_code(enabled_user.totp_secret),
                    },
                    allow_redirects=False,
                )
                enabled_page = await client.get("/security")
                enabled_html = await enabled_page.text()
                disabled = await client.post(
                    "/security",
                    data={
                        "action": "disable",
                        "password": "Strong-pass-1!",
                        "totp": totp_code(enabled_user.totp_secret),
                    },
                )
                password_only_login = await client.post(
                    "/login",
                    data={
                        "username": user.username,
                        "password": "Strong-pass-1!",
                    },
                    allow_redirects=False,
                )
                audit_text = (data_dir / "web_audit_events.jsonl").read_text(
                    encoding="utf-8"
                )
            finally:
                await client.close()

        self.assertEqual(unauthorized.status, 302)
        self.assertEqual(login.status, 302)
        self.assertEqual(setup.status, 200)
        self.assertIn(user.totp_secret, setup_html)
        self.assertIn("Test Trading", setup_html)
        self.assertEqual(setup.headers["Cache-Control"], "no-store")
        self.assertEqual(invalid.status, 400)
        self.assertEqual(enabled.status, 200)
        self.assertEqual(expired_session.status, 401)
        self.assertEqual(no_code_login.status, 401)
        self.assertEqual(totp_login.status, 302)
        self.assertEqual(enabled_page.status, 200)
        self.assertIn("二次验证已启用", enabled_html)
        self.assertEqual(disabled.status, 200)
        self.assertEqual(password_only_login.status, 302)
        self.assertIn('"action": "totp_enable"', audit_text)
        self.assertIn('"action": "totp_disable"', audit_text)
        self.assertNotIn(user.totp_secret, audit_text)

    async def test_user_backtest_api_login_run_and_delete_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            user = user_store.create_user(
                email="researcher@example.com",
                username="researcher01",
                password="Strong-pass-1!",
                allowed_assets=["ACS"],
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                ),
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=str(data_dir / "trade_events.jsonl"),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            workspace = app["user_workspace_store"]
            project = workspace.upsert_project(
                UserProject.from_dict(
                    {
                        "id": "project-backtest",
                        "owner_email": user.email,
                        "name": "ACS Backtest",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            account = workspace.upsert_account(
                UserExchangeAccount.from_dict(
                    {
                        "id": "account-backtest",
                        "owner_email": user.email,
                        "project_id": project.id,
                        "label": "Coinbase Public",
                        "exchange": "coinbase",
                        "market_type": "spot",
                        "symbol": "ACS/USDC",
                    }
                )
            )
            strategy = workspace.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "id": "strategy-backtest",
                        "owner_email": user.email,
                        "project_id": project.id,
                        "name": "ACS DCA Research",
                        "strategy_type": "dca",
                        "account_ids": [account.id],
                        "parameters": {
                            "side": "buy",
                            "total_quote": 20.0,
                            "quote_per_order": 5.0,
                            "interval_seconds": 3600.0,
                            "trigger_price": 1.0,
                            "take_profit_pct": 0.0,
                        },
                    }
                )
            )

            async def fake_history(_account, *, timeframe, limit):
                self.assertEqual(timeframe, "1h")
                start = 1_700_000_000_000
                return [
                    {
                        "timestamp_ms": start + index * 3_600_000,
                        "open": 1.0,
                        "high": 1.02,
                        "low": 0.95,
                        "close": 1.0 - (index % 4) * 0.01,
                        "volume": 100.0,
                    }
                    for index in range(limit)
                ]

            app["user_backtest_service"].fetcher = fake_history
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                unauthorized = await client.get("/api/user-backtests")
                login = await client.post(
                    "/login",
                    data={
                        "username": user.username,
                        "password": "Strong-pass-1!",
                    },
                )
                initial = await client.get("/api/user-backtests")
                create = await client.post(
                    "/api/user-backtests",
                    json={
                        "action": "create",
                        "project_id": project.id,
                        "strategy_id": strategy.id,
                        "account_id": account.id,
                        "timeframe": "1h",
                        "history_bars": 30,
                        "initial_cash": 100.0,
                        "initial_base": 0.0,
                        "fee_bps": 20.0,
                        "slippage_bps": 5.0,
                        "latency_bars": 0,
                    },
                )
                create_payload = await create.json()
                run_id = create_payload["run"]["id"]
                completed_payload = None
                for _ in range(100):
                    response = await client.get(f"/api/user-backtests?run_id={run_id}")
                    completed_payload = await response.json()
                    if completed_payload["selected"]["status"] == "complete":
                        break
                    await asyncio.sleep(0.01)
                delete = await client.post(
                    "/api/user-backtests",
                    json={"action": "delete", "run_id": run_id},
                )
                delete_payload = await delete.json()
            finally:
                await client.close()

        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(login.status, 200)
        self.assertEqual(initial.status, 200)
        self.assertEqual(create.status, 200, create_payload)
        assert completed_payload is not None
        self.assertEqual(completed_payload["selected"]["status"], "complete")
        self.assertEqual(
            completed_payload["selected"]["result"]["data_source"],
            "exchange_ohlcv",
        )
        self.assertFalse(completed_payload["selected"]["live_submit_allowed"])
        self.assertEqual(delete.status, 200, delete_payload)
        self.assertEqual(delete_payload["backtests"]["runs"], [])

    async def test_user_workspace_wallet_signature_api_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            member = user_store.create_user(
                email="wallet@example.com",
                username="wallet01",
                password="Strong-pass-2!",
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                ),
                trade_log=TradeLogConfig(
                    enabled=True,
                    path=str(data_dir / "trade_events.jsonl"),
                ),
            )
            signer = Account.create()
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "username": member.username,
                        "password": "Strong-pass-2!",
                    },
                )
                challenge_response = await client.post(
                    "/api/user-workspace",
                    json={
                        "action": "wallet_challenge",
                        "address": signer.address,
                        "chain_id": 137,
                        "wallet_type": "metamask",
                    },
                )
                challenge_payload = await challenge_response.json()
                challenge = challenge_payload["wallet_challenge"]
                signature = Account.sign_message(
                    encode_defunct(text=challenge["message"]),
                    signer.key,
                ).signature.hex()
                verify_response = await client.post(
                    "/api/user-workspace",
                    json={
                        "action": "verify_wallet",
                        "challenge_id": challenge["challenge_id"],
                        "signature": signature,
                        "label": "MetaMask Main",
                    },
                )
                verify_payload = await verify_response.json()
                with patch(
                    "arbitrage_bot.web.routes.workspace.probe_dex_venue",
                    new=AsyncMock(
                        return_value={
                            "status": "healthy",
                            "venue": "polymarket",
                            "wallet_address": signer.address,
                            "detail": {"position_count": 2},
                            "latency_ms": 15.0,
                            "checked_at": time.time(),
                            "live_trading_authorized": False,
                        }
                    ),
                ):
                    venue_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "test_wallet_venue",
                            "venue": "polymarket",
                            "wallet_id": verify_payload["wallet"]["id"],
                        },
                    )
                    venue_payload = await venue_response.json()
                refresh_check = {
                    "status": "healthy",
                    "venue": "polymarket",
                    "wallet_address": signer.address,
                    "detail": {"position_count": 3},
                    "latency_ms": 9.0,
                    "checked_at": time.time(),
                    "live_trading_authorized": False,
                }
                with patch(
                    "arbitrage_bot.venue_health.probe_dex_venue",
                    new=AsyncMock(return_value=refresh_check),
                ):
                    refresh_venue_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "refresh_venue_connection",
                            "connection_id": venue_payload["venue_connection"]["id"],
                        },
                    )
                    refresh_venue_payload = await refresh_venue_response.json()
                    refresh_all_response = await client.post(
                        "/api/user-workspace",
                        json={"action": "refresh_all_venue_connections"},
                    )
                    refresh_all_payload = await refresh_all_response.json()
                delete_venue_response = await client.post(
                    "/api/user-workspace",
                    json={
                        "action": "delete_venue_connection",
                        "connection_id": venue_payload["venue_connection"]["id"],
                    },
                )
                delete_venue_payload = await delete_venue_response.json()
            finally:
                await client.close()

        self.assertEqual(challenge_response.status, 200, challenge_payload)
        self.assertEqual(verify_response.status, 200, verify_payload)
        self.assertEqual(verify_payload["wallet"]["address"], signer.address)
        self.assertFalse(verify_payload["wallet"]["trading_authorized"])
        self.assertEqual(verify_payload["workspace"]["summary"]["wallet_count"], 1)
        self.assertEqual(venue_response.status, 200, venue_payload)
        self.assertTrue(venue_payload["venue_connection"]["read_only_verified"])
        self.assertFalse(venue_payload["venue_connection"]["trading_authorized"])
        self.assertEqual(
            venue_payload["workspace"]["summary"]["venue_connection_count"],
            1,
        )
        self.assertEqual(refresh_venue_response.status, 200, refresh_venue_payload)
        self.assertEqual(refresh_venue_payload["venue_refresh"]["healthy_count"], 1)
        self.assertEqual(refresh_all_response.status, 200, refresh_all_payload)
        self.assertEqual(refresh_all_payload["venue_refresh"]["refreshed_count"], 1)
        self.assertEqual(delete_venue_response.status, 200, delete_venue_payload)
        self.assertEqual(delete_venue_payload["workspace"]["venue_connections"], [])

    async def test_pending_projects_migrate_to_self_service_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            user_store.create_user(
                email="admin@example.com",
                username="admin01",
                password="Strong-pass-1!",
            )
            member = user_store.create_user(
                email="member@example.com",
                username="member01",
                password="Strong-pass-2!",
            )
            workspace_path = data_dir / "user_workspace.sqlite3"
            workspace = UserWorkspaceStore(workspace_path, master_key_env=None)
            workspace.upsert_project(
                UserProject.from_dict(
                    {
                        "id": "project-pending",
                        "owner_email": member.email,
                        "name": "Legacy Pending Project",
                        "asset": "ACS",
                        "quote_currency": "USDC",
                        "status": "pending",
                    }
                )
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(workspace_path),
                    credential_master_key_env=None,
                ),
                trade_log=TradeLogConfig(
                    enabled=False,
                    path=str(data_dir / "trade_events.jsonl"),
                ),
            )

            app = create_app(cfg, "spot-spread", cfg.poll_seconds)

            migrated = app["user_workspace_store"].get_project("project-pending")
            migrated_user = app["web_user_store"].get_user(member.email)
            self.assertEqual(app["self_service_project_migrations"], ["project-pending"])
            self.assertIsNotNone(migrated)
            self.assertEqual(migrated.status, "active")
            self.assertEqual(migrated_user.allowed_assets, [])

    async def test_user_workspace_self_service_project_and_encrypted_account_flow(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            admin = user_store.create_user(
                email="admin@example.com",
                username="admin01",
                password="Strong-pass-1!",
            )
            member = user_store.create_user(
                email="member@example.com",
                username="member01",
                password="Strong-pass-2!",
            )
            workspace_path = data_dir / "user_workspace.sqlite3"
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(workspace_path),
                    credential_master_key_env="TEST_CREDENTIAL_MASTER_KEY",
                ),
                trade_log=TradeLogConfig(
                    enabled=True,
                    path=str(data_dir / "trade_events.jsonl"),
                ),
            )
            master_key = base64.urlsafe_b64encode(b"m" * 32).decode("ascii")
            with patch.dict(
                os.environ,
                {"TEST_CREDENTIAL_MASTER_KEY": master_key},
                clear=False,
            ):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    await client.post(
                        "/login",
                        data={
                            "username": member.username,
                            "password": "Strong-pass-2!",
                        },
                    )
                    project_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_project",
                            "project": {
                                "name": "ACS Project",
                                "asset": "ACS",
                                "quote_currency": "USDC",
                            },
                        },
                    )
                    project_payload = await project_response.json()
                    project = project_payload["workspace"]["projects"][0]
                    self.assertEqual(project_payload["project"]["id"], project["id"])

                    disable_project_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "disable_project",
                            "project_id": project["id"],
                        },
                    )
                    disable_project_payload = await disable_project_response.json()
                    activate_project_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "activate_project",
                            "project_id": project["id"],
                        },
                    )
                    activate_project_payload = await activate_project_response.json()

                    with patch.object(
                        app["workspace_market_discovery"],
                        "discover",
                        new_callable=AsyncMock,
                    ) as discovery_mock:
                        discovery_mock.return_value = (
                            [
                                {
                                    "symbol": "ACS/USDC",
                                    "base": "ACS",
                                    "quote": "USDC",
                                    "active": True,
                                    "type": "spot",
                                    "cost_min": 1.0,
                                }
                            ],
                            False,
                        )
                        discovery_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "discover_markets",
                                "project_id": project["id"],
                                "exchange": "coinbase",
                                "market_type": "spot",
                                "api_variant": "default",
                            },
                        )
                        discovery_payload = await discovery_response.json()

                    account_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_account",
                            "account": {
                                "project_id": project["id"],
                                "label": "Coinbase Main",
                                "exchange": "coinbase",
                                "market_type": "spot",
                                "enabled": False,
                                "connection_status": "healthy",
                                "withdrawal_disabled_confirmed": True,
                                "credentials": {
                                    "api_key": "test-api-key-value",
                                    "secret": "test-secret-value",
                                },
                            },
                        },
                    )
                    account_payload = await account_response.json()
                    account = account_payload["workspace"]["accounts"][0]
                    self.assertEqual(account_payload["account"]["id"], account["id"])

                    premature_enable_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_account",
                            "account": {"id": account["id"], "enabled": True},
                        },
                    )
                    member_approve_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "approve_project",
                            "project_id": project["id"],
                        },
                    )

                    await client.get("/logout")
                    await client.post(
                        "/login",
                        data={
                            "username": admin.username,
                            "password": "Strong-pass-1!",
                        },
                    )
                    admin_foreign_account_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "test_account",
                            "account_id": account["id"],
                        },
                    )
                    admin_foreign_account_payload = (
                        await admin_foreign_account_response.json()
                    )
                    admin_settings_state = await (
                        await client.get("/api/state?view=settings")
                    ).json()
                    unregistered_owner_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_project",
                            "project": {
                                "owner_email": "missing@example.com",
                                "name": "Missing Owner",
                                "asset": "BTC",
                                "quote_currency": "USDT",
                            },
                        },
                    )
                    unregistered_owner_payload = (
                        await unregistered_owner_response.json()
                    )

                    await client.get("/logout")
                    await client.post(
                        "/login",
                        data={
                            "username": member.username,
                            "password": "Strong-pass-2!",
                        },
                    )
                    untested_enable_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_account",
                            "account": {"id": account["id"], "enabled": True},
                        },
                    )
                    untested_enable_payload = await untested_enable_response.json()
                    with patch.object(
                        app["workspace_account_checker"],
                        "check",
                        new_callable=AsyncMock,
                    ) as check_mock:
                        check_mock.return_value = {
                            "status": "healthy",
                            "checked_at": time.time(),
                            "latency_ms": 12.5,
                            "exchange": "coinbase",
                            "market_type": "spot",
                            "api_variant": "default",
                            "symbol": "ACS/USDC",
                            "market": {"symbol": "ACS/USDC", "active": True},
                            "order_book": {"available": True},
                            "balances": [
                                {"currency": "ACS", "total": 10.0},
                                {"currency": "USDC", "total": 20.0},
                            ],
                            "open_order_count": 0,
                        }
                        connection_test_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "test_account",
                                "account_id": account["id"],
                            },
                        )
                        connection_test_payload = await connection_test_response.json()
                    enable_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_account",
                            "account": {"id": account["id"], "enabled": True},
                        },
                    )
                    enable_payload = await enable_response.json()

                    strategy_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_strategy",
                            "strategy": {
                                "name": "ACS Coinbase MM",
                                "project_id": project["id"],
                                "strategy_type": "market_maker",
                                "account_ids": [account["id"]],
                                "enabled": False,
                            },
                        },
                    )
                    strategy_payload = await strategy_response.json()
                    strategy = strategy_payload["workspace"]["strategies"][0]
                    live_strategy_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_strategy",
                            "strategy": {
                                "id": strategy["id"],
                                "enabled": True,
                            },
                        },
                    )
                    live_strategy_payload = await live_strategy_response.json()
                    account_delete_blocked_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "delete_account",
                            "account_id": account["id"],
                        },
                    )
                    account_delete_blocked_payload = (
                        await account_delete_blocked_response.json()
                    )
                    invalid_strategy_toggle_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "set_strategy_enabled",
                            "strategy_id": strategy["id"],
                            "enabled": "false",
                        },
                    )
                    invalid_strategy_toggle_payload = (
                        await invalid_strategy_toggle_response.json()
                    )
                    pause_strategy_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "set_strategy_enabled",
                            "strategy_id": strategy["id"],
                            "enabled": False,
                        },
                    )
                    pause_strategy_payload = await pause_strategy_response.json()
                    resume_strategy_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "set_strategy_enabled",
                            "strategy_id": strategy["id"],
                            "enabled": True,
                        },
                    )
                    resume_strategy_payload = await resume_strategy_response.json()

                    paper_strategy_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_strategy",
                            "strategy": {
                                "name": "ACS Coinbase DCA",
                                "project_id": project["id"],
                                "strategy_type": "dca",
                                "account_ids": [account["id"]],
                                "enabled": True,
                            },
                        },
                    )
                    paper_strategy_payload = await paper_strategy_response.json()

                    exchange_change_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_account",
                            "account": {
                                "id": account["id"],
                                "exchange": "bybit",
                                "enabled": False,
                            },
                        },
                    )
                    exchange_change_payload = await exchange_change_response.json()

                    scope_change_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_project",
                            "project": {
                                "id": project["id"],
                                "asset": "BTC",
                            },
                        },
                    )
                    scope_change_payload = await scope_change_response.json()
                    settings_state = await (
                        await client.get("/api/state?view=settings")
                    ).json()
                    trading_state = await (
                        await client.get(
                            "/api/state?view=trading&sections=user-market-maker"
                        )
                    ).json()
                finally:
                    await client.close()

            persisted_member = app["web_user_store"].get_user(member.email)
            database_bytes = workspace_path.read_bytes()

        self.assertEqual(project_response.status, 200, project_payload)
        self.assertEqual(project["status"], "active")
        self.assertEqual(
            disable_project_response.status,
            200,
            disable_project_payload,
        )
        self.assertEqual(
            disable_project_payload["workspace"]["projects"][0]["status"],
            "disabled",
        )
        self.assertEqual(activate_project_response.status, 200, activate_project_payload)
        self.assertEqual(
            activate_project_payload["workspace"]["projects"][0]["status"],
            "active",
        )
        self.assertEqual(discovery_response.status, 200, discovery_payload)
        self.assertEqual(discovery_payload["markets"][0]["symbol"], "ACS/USDC")
        self.assertEqual(account_response.status, 200, account_payload)
        self.assertTrue(account["credentials"]["configured"])
        self.assertEqual(account["connection_status"], "unverified")
        self.assertEqual(account["symbol"], "ACS/USDC")
        self.assertNotIn("api_key", account)
        self.assertNotIn("secret", account)
        self.assertEqual(premature_enable_response.status, 400)
        self.assertEqual(member_approve_response.status, 403)
        self.assertEqual(
            admin_settings_state["user_workspace"]["platform_projects"][0]["status"],
            "active",
        )
        self.assertEqual(
            admin_foreign_account_response.status,
            403,
            admin_foreign_account_payload,
        )
        self.assertEqual(admin_settings_state["user_workspace"]["accounts"], [])
        self.assertEqual(
            admin_settings_state["user_workspace"]["platform_projects"][0]["id"],
            project["id"],
        )
        self.assertEqual(
            unregistered_owner_response.status, 403, unregistered_owner_payload
        )
        self.assertIn(
            "resource belongs to another user",
            unregistered_owner_payload["error"],
        )
        self.assertEqual(persisted_member.allowed_assets, [])
        self.assertEqual(settings_state["auth"]["permission_model"], "account_owner_v1")
        self.assertEqual(
            settings_state["auth"]["permissions"]["owner_email"],
            member.email,
        )
        self.assertEqual(
            settings_state["user_workspace"]["permissions"]["scope_source"],
            "owned_api_connections",
        )
        self.assertTrue(
            settings_state["user_workspace"]["strategy_access"]["core_trading"][
                "enabled"
            ]
        )
        self.assertEqual(
            settings_state["user_workspace"]["strategy_access"]["core_trading"][
                "strategy_types"
            ],
            ["auto_buy_sell", "market_maker"],
        )
        self.assertFalse(
            settings_state["user_workspace"]["strategy_access"]["platform_manage"]
        )
        self.assertEqual(
            settings_state["user_workspace"]["strategy_access"]["core_trading"][
                "live_strategy_types"
            ],
            ["auto_buy_sell", "market_maker"],
        )
        self.assertTrue(
            settings_state["user_workspace"]["strategy_access"]["quant"]["enabled"]
        )
        self.assertTrue(
            settings_state["user_workspace"]["strategy_access"]["quant"][
                "live_submit_allowed"
            ]
        )
        self.assertEqual(
            settings_state["user_workspace"]["strategy_access"]["quant"][
                "strategy_types"
            ],
            [
                "market_maker",
                "auto_buy_sell",
                "spot_grid",
                "dca",
                "spot_spread",
                "contract_arbitrage",
                "prediction_arbitrage",
            ],
        )
        self.assertEqual(
            settings_state["user_workspace"]["strategy_access"]["quant"][
                "live_strategy_types"
            ],
            ["market_maker"],
        )
        self.assertEqual(
            trading_state["user_workspace"]["strategy_access"]["scope"],
            "owner",
        )
        self.assertEqual(
            admin_settings_state["user_workspace"]["strategy_access"]["scope"],
            "owner",
        )
        self.assertTrue(
            admin_settings_state["user_workspace"]["strategy_access"][
                "platform_manage"
            ]
        )
        self.assertTrue(
            all(
                row["owner_email"] == member.email
                for row in trading_state["user_workspace"]["strategies"]
            )
        )
        self.assertIn(
            "market_maker",
            {
                row["strategy_type"]
                for row in trading_state["user_workspace"]["strategies"]
            },
        )
        self.assertEqual(untested_enable_response.status, 400, untested_enable_payload)
        self.assertIn("connection test", untested_enable_payload["error"])
        self.assertEqual(connection_test_response.status, 200, connection_test_payload)
        self.assertEqual(
            connection_test_payload["connection_test"]["status"],
            "healthy",
        )
        self.assertEqual(enable_response.status, 200, enable_payload)
        self.assertTrue(enable_payload["workspace"]["accounts"][0]["enabled"])
        self.assertTrue(enable_payload["workspace"]["accounts"][0]["connection_fresh"])
        self.assertEqual(strategy_response.status, 200, strategy_payload)
        self.assertEqual(strategy["status"], "paused")
        self.assertFalse(strategy["effective_enabled"])
        self.assertTrue(strategy["readiness"]["live_submit_allowed"])
        self.assertEqual(strategy["mode"], "live")
        self.assertEqual(live_strategy_response.status, 400, live_strategy_payload)
        self.assertIn("confirm_live", live_strategy_payload["error"])
        self.assertEqual(
            account_delete_blocked_response.status,
            400,
            account_delete_blocked_payload,
        )
        self.assertIn(
            "strategies using this account", account_delete_blocked_payload["error"]
        )
        self.assertEqual(
            invalid_strategy_toggle_response.status,
            400,
            invalid_strategy_toggle_payload,
        )
        self.assertIn(
            "enabled must be true or false",
            invalid_strategy_toggle_payload["error"],
        )
        self.assertEqual(pause_strategy_response.status, 200, pause_strategy_payload)
        self.assertFalse(
            pause_strategy_payload["workspace"]["strategies"][0]["enabled"]
        )
        self.assertEqual(resume_strategy_response.status, 400, resume_strategy_payload)
        self.assertIn("confirm_live", resume_strategy_payload["error"])
        self.assertEqual(
            paper_strategy_response.status,
            200,
            paper_strategy_payload,
        )
        paper_strategy = next(
            row
            for row in paper_strategy_payload["workspace"]["strategies"]
            if row["strategy_type"] == "dca"
        )
        self.assertEqual(paper_strategy["mode"], "paper")
        self.assertTrue(paper_strategy["enabled"])
        self.assertTrue(paper_strategy["effective_enabled"])
        self.assertEqual(paper_strategy["paper_runtime"]["mode"], "paper")
        self.assertEqual(exchange_change_response.status, 400, exchange_change_payload)
        self.assertIn("re-enter API key", exchange_change_payload["error"])
        self.assertEqual(scope_change_response.status, 200, scope_change_payload)
        self.assertEqual(
            scope_change_payload["workspace"]["projects"][0]["status"], "active"
        )
        self.assertFalse(scope_change_payload["workspace"]["accounts"][0]["enabled"])
        self.assertFalse(scope_change_payload["workspace"]["strategies"][0]["enabled"])
        self.assertEqual(
            settings_state["user_workspace"]["summary"]["project_count"], 1
        )
        self.assertNotIn(b"test-api-key-value", database_bytes)
        self.assertNotIn(b"test-secret-value", database_bytes)


    async def test_exchange_account_egress_checks_are_isolated_per_user(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            admin = user_store.create_user(
                email="admin@example.com",
                username="admin01",
                password="Strong-pass-1!",
            )
            member = user_store.create_user(
                email="member@example.com",
                username="member01",
                password="Strong-pass-2!",
            )
            workspace_path = data_dir / "user_workspace.sqlite3"
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(workspace_path),
                    credential_master_key_env="TEST_CREDENTIAL_MASTER_KEY",
                )
            )
            master_key = base64.urlsafe_b64encode(b"e" * 32).decode("ascii")
            with patch.dict(
                os.environ,
                {"TEST_CREDENTIAL_MASTER_KEY": master_key},
                clear=False,
            ):
                workspace = UserWorkspaceStore(
                    workspace_path,
                    master_key_env="TEST_CREDENTIAL_MASTER_KEY",
                )
                existing = workspace.upsert_api_connection(
                    UserApiConnection.from_dict(
                        {
                            "owner_email": admin.email,
                            "label": "Bybit Main",
                            "exchange": "bybit",
                            "withdrawal_disabled_confirmed": True,
                            "trade_permission_confirmed": True,
                        }
                    ),
                    credentials={"api_key": "admin-key", "secret": "admin-secret"},
                )
                existing = workspace.update_api_connection_check(
                    existing.id,
                    status="healthy",
                )
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    await client.post(
                        "/login",
                        data={
                            "username": member.username,
                            "password": "Strong-pass-2!",
                        },
                    )
                    save_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "sync_account",
                            "account": {
                                "label": "Bybit Second",
                                "exchange": "bybit",
                                "withdrawal_disabled_confirmed": True,
                                "trade_permission_confirmed": True,
                                "credentials": {
                                    "api_key": "member-key",
                                    "secret": "member-secret",
                                },
                            },
                        },
                    )
                    save_payload = await save_response.json()
                    same_owner_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "sync_account",
                            "account": {
                                "label": "Bybit Member 2",
                                "exchange": "bybit",
                                "withdrawal_disabled_confirmed": True,
                                "trade_permission_confirmed": True,
                                "credentials": {
                                    "api_key": "member-key-2",
                                    "secret": "member-secret-2",
                                },
                            },
                        },
                    )
                    same_owner_payload = await same_owner_response.json()
                finally:
                    await client.close()

                all_connections = workspace.list_api_connections(
                    owner_email="",
                    is_admin=True,
                )
                existing_blockers = api_connection_egress_blockers(
                    existing,
                    all_connections,
                )

        self.assertEqual(save_response.status, 200, save_payload)
        self.assertEqual(len(save_payload["workspace"]["connections"]), 1)
        self.assertEqual(
            save_payload["workspace"]["connections"][0]["connection_status"],
            "unverified",
        )
        self.assertEqual(save_payload["warnings"], [])
        self.assertEqual(same_owner_response.status, 200, same_owner_payload)
        self.assertEqual(len(same_owner_payload["workspace"]["connections"]), 2)
        self.assertIn("saved as inactive", same_owner_payload["warnings"][0])
        self.assertEqual(existing_blockers, [])

    async def test_api_connection_syncs_all_matching_user_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            member = user_store.create_user(
                email="sync@example.com",
                username="syncuser",
                password="Strong-pass-3!",
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                    credential_master_key_env="TEST_CREDENTIAL_MASTER_KEY",
                )
            )
            master_key = base64.urlsafe_b64encode(b"s" * 32).decode("ascii")
            with patch.dict(
                os.environ,
                {"TEST_CREDENTIAL_MASTER_KEY": master_key},
                clear=False,
            ):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    await client.post(
                        "/login",
                        data={
                            "username": member.username,
                            "password": "Strong-pass-3!",
                        },
                    )
                    for asset, quote in (("ACS", "USDC"), ("BTC", "USD")):
                        response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "upsert_project",
                                "project": {
                                    "name": f"{asset} Project",
                                    "asset": asset,
                                    "quote_currency": quote,
                                },
                            },
                        )
                        self.assertEqual(response.status, 200)

                    async def discover_markets(**kwargs):
                        asset = kwargs["asset"]
                        quote = "USDC" if asset == "ACS" else "USD"
                        return (
                            [
                                {
                                    "symbol": f"{asset}/{quote}",
                                    "base": asset,
                                    "quote": quote,
                                    "active": True,
                                    "type": "spot",
                                }
                            ],
                            False,
                        )

                    with patch.object(
                        app["workspace_market_discovery"],
                        "discover",
                        side_effect=discover_markets,
                    ):
                        sync_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "sync_account",
                                "account": {
                                    "label": "Coinbase Main",
                                    "exchange": "coinbase",
                                    "market_type": "spot",
                                    "api_variant": "default",
                                    "withdrawal_disabled_confirmed": True,
                                    "trade_permission_confirmed": True,
                                    "credentials": {
                                        "api_key": "test-api-key-value",
                                        "secret": "test-secret-value",
                                    },
                                },
                            },
                        )
                    sync_payload = await sync_response.json()
                    self.assertEqual(sync_response.status, 200, sync_payload)
                    self.assertEqual(len(sync_payload["accounts"]), 2)
                    connection_id = sync_payload["connection_id"]
                    self.assertEqual(
                        {row["connection_id"] for row in sync_payload["accounts"]},
                        {connection_id},
                    )
                    self.assertEqual(
                        {row["symbol"] for row in sync_payload["accounts"]},
                        {"ACS/USDC", "BTC/USD"},
                    )
                    self.assertEqual(
                        sync_payload["workspace"]["summary"]["connection_count"],
                        1,
                    )

                    with patch.object(
                        app["workspace_market_discovery"],
                        "discover",
                        side_effect=discover_markets,
                    ):
                        resync_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "sync_account",
                                "account": {
                                    "label": "Coinbase Main",
                                    "exchange": "coinbase",
                                    "market_type": "spot",
                                    "api_variant": "default",
                                    "withdrawal_disabled_confirmed": True,
                                    "trade_permission_confirmed": True,
                                },
                            },
                        )
                    resync_payload = await resync_response.json()
                    self.assertEqual(resync_response.status, 200, resync_payload)
                    self.assertEqual(resync_payload["connection_id"], connection_id)
                    self.assertEqual(len(resync_payload["workspace"]["accounts"]), 2)

                    async def check_account(*, account, **_kwargs):
                        return {
                            "status": "healthy",
                            "checked_at": time.time(),
                            "latency_ms": 9.0,
                            "symbol": account.symbol,
                            "balances": [
                                {
                                    "currency": "USDC",
                                    "free": 98.0,
                                    "used": 2.0,
                                    "total": 100.0,
                                }
                            ],
                            "open_order_count": 2,
                        }

                    with patch.object(
                        app["workspace_account_checker"],
                        "check",
                        side_effect=check_account,
                    ):
                        test_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "test_connection",
                                "connection_id": connection_id,
                            },
                        )
                    test_payload = await test_response.json()
                    self.assertEqual(test_response.status, 200, test_payload)
                    self.assertEqual(
                        test_payload["connection_test"]["healthy_count"],
                        2,
                    )
                    tested_connection = test_payload["workspace"]["connections"][0]
                    self.assertEqual(tested_connection["balances"][0]["total"], 100.0)
                    self.assertEqual(tested_connection["open_order_count"], 4)
                    self.assertEqual(tested_connection["latency_ms"], 9.0)
                    self.assertEqual(tested_connection["enabled_count"], 2)
                    self.assertTrue(tested_connection["live_enabled"])
                    trading_state = await (
                        await client.get("/api/state?view=trading")
                    ).json()
                    self.assertEqual(
                        trading_state["user_workspace"]["connections"][0]["id"],
                        connection_id,
                    )
                    merged_balances = trading_state["account_balances"]
                    self.assertEqual(merged_balances["total_account_count"], 1)
                    self.assertEqual(
                        merged_balances["accounts"][0]["workspace_connection_id"],
                        connection_id,
                    )
                    self.assertTrue(merged_balances["accounts"][0]["live_enabled"])
                    self.assertEqual(merged_balances["totals"][0]["total"], 100.0)

                    delete_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "delete_connection",
                            "connection_id": connection_id,
                        },
                    )
                    delete_payload = await delete_response.json()
                    self.assertEqual(delete_response.status, 200, delete_payload)
                    self.assertEqual(delete_payload["deleted_count"], 2)
                    self.assertEqual(delete_payload["workspace"]["connections"], [])
                finally:
                    await client.close()

    async def test_global_api_connection_can_precede_projects_and_auto_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            member = user_store.create_user(
                email="global-api@example.com",
                username="globalapi",
                password="Strong-pass-4!",
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                    credential_master_key_env="TEST_CREDENTIAL_MASTER_KEY",
                )
            )
            master_key = base64.urlsafe_b64encode(b"g" * 32).decode("ascii")
            with patch.dict(
                os.environ,
                {"TEST_CREDENTIAL_MASTER_KEY": master_key},
                clear=False,
            ):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    await client.post(
                        "/login",
                        data={
                            "username": member.username,
                            "password": "Strong-pass-4!",
                        },
                    )
                    save_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "sync_account",
                            "account": {
                                "label": "Coinbase Global",
                                "exchange": "coinbase",
                                "market_type": "spot",
                                "api_variant": "default",
                                "withdrawal_disabled_confirmed": True,
                                "trade_permission_confirmed": True,
                                "credentials": {
                                    "api_key": "global-api-key",
                                    "secret": "global-api-secret",
                                },
                            },
                        },
                    )
                    save_payload = await save_response.json()
                    connection_id = save_payload["connection_id"]

                    with patch.object(
                        app["workspace_account_checker"],
                        "check_api_connection",
                        new_callable=AsyncMock,
                    ) as check_mock:
                        check_mock.return_value = {
                            "status": "healthy",
                            "checked_at": time.time(),
                            "latency_ms": 7.0,
                            "balances": [
                                {
                                    "currency": "USDC",
                                    "free": 100.0,
                                    "used": 0.0,
                                    "total": 100.0,
                                }
                            ],
                            "open_order_count": 0,
                            "market_count": 100,
                        }
                        test_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "test_connection",
                                "connection_id": connection_id,
                            },
                        )
                    test_payload = await test_response.json()
                    trading_state = await (
                        await client.get("/api/state?view=trading")
                    ).json()

                    with patch.object(
                        app["workspace_market_discovery"],
                        "discover",
                        new_callable=AsyncMock,
                    ) as discovery_mock:
                        discovery_mock.return_value = (
                            [
                                {
                                    "symbol": "ACS/USDC",
                                    "base": "ACS",
                                    "quote": "USDC",
                                    "active": True,
                                    "type": "spot",
                                }
                            ],
                            False,
                        )
                        project_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "upsert_project",
                                "project": {
                                    "name": "ACS Project",
                                    "asset": "ACS",
                                    "quote_currency": "USDC",
                                },
                            },
                        )
                    project_payload = await project_response.json()
                finally:
                    await client.close()

            database_bytes = (data_dir / "user_workspace.sqlite3").read_bytes()

        self.assertEqual(save_response.status, 200, save_payload)
        self.assertEqual(save_payload["accounts"], [])
        self.assertEqual(len(save_payload["workspace"]["connections"]), 1)
        self.assertTrue(
            save_payload["workspace"]["connections"][0]["credentials_configured"]
        )
        self.assertEqual(test_response.status, 200, test_payload)
        self.assertEqual(test_payload["connection_test"]["status"], "healthy")
        self.assertEqual(trading_state["account_balances"]["total_account_count"], 1)
        self.assertEqual(
            trading_state["account_balances"]["totals"][0]["currency"],
            "USDC",
        )
        self.assertEqual(project_response.status, 200, project_payload)
        self.assertEqual(len(project_payload["workspace"]["accounts"]), 1)
        bound_account = project_payload["workspace"]["accounts"][0]
        self.assertEqual(bound_account["connection_id"], connection_id)
        self.assertEqual(bound_account["symbol"], "ACS/USDC")
        self.assertTrue(bound_account["enabled"])
        self.assertEqual(
            project_payload["workspace"]["connections"][0]["markets"][0][
                "symbol"
            ],
            "ACS/USDC",
        )
        self.assertNotIn(b"global-api-key", database_bytes)
        self.assertNotIn(b"global-api-secret", database_bytes)

    async def test_unified_api_account_manages_spot_and_swap_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_store_path = data_dir / "web_users.json"
            user_store = WebUserStore(user_store_path)
            member = user_store.create_user(
                email="unified@example.com",
                username="unified",
                password="Strong-pass-5!",
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(user_store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                    credential_master_key_env="TEST_CREDENTIAL_MASTER_KEY",
                )
            )
            master_key = base64.urlsafe_b64encode(b"u" * 32).decode("ascii")
            with patch.dict(
                os.environ,
                {"TEST_CREDENTIAL_MASTER_KEY": master_key},
                clear=False,
            ):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    await client.post(
                        "/login",
                        data={
                            "username": member.username,
                            "password": "Strong-pass-5!",
                        },
                    )

                    async def discover_markets(**kwargs):
                        market_type = kwargs["market_type"]
                        symbol = (
                            "ACS/USDT"
                            if market_type == "spot"
                            else "ACS/USDT:USDT"
                        )
                        return (
                            [
                                {
                                    "symbol": symbol,
                                    "base": "ACS",
                                    "quote": "USDT",
                                    "settle": "USDT" if market_type == "swap" else "",
                                    "active": True,
                                    "type": market_type,
                                }
                            ],
                            False,
                        )

                    with patch.object(
                        app["workspace_market_discovery"],
                        "discover",
                        side_effect=discover_markets,
                    ):
                        discovery_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "discover_markets",
                                "exchange": "bybit",
                                "assets": ["ACS"],
                            },
                        )
                        save_response = await client.post(
                            "/api/user-workspace",
                            json={
                                "action": "sync_account",
                                "account": {
                                    "label": "Bybit Main",
                                    "exchange": "bybit",
                                    "withdrawal_disabled_confirmed": True,
                                    "trade_permission_confirmed": True,
                                    "replace_markets": True,
                                    "markets": [
                                        {
                                            "market_type": "spot",
                                            "symbol": "ACS/USDT",
                                        },
                                        {
                                            "market_type": "swap",
                                            "symbol": "ACS/USDT:USDT",
                                        },
                                    ],
                                    "credentials": {
                                        "api_key": "unified-api-key",
                                        "secret": "unified-api-secret",
                                    },
                                },
                            },
                        )
                    discovery_payload = await discovery_response.json()
                    save_payload = await save_response.json()
                finally:
                    await client.close()

            database_bytes = (data_dir / "user_workspace.sqlite3").read_bytes()

        self.assertEqual(discovery_response.status, 200, discovery_payload)
        self.assertEqual(
            {
                (row["market_type"], row["symbol"])
                for row in discovery_payload["markets"]
            },
            {("spot", "ACS/USDT"), ("swap", "ACS/USDT:USDT")},
        )
        self.assertEqual(save_response.status, 200, save_payload)
        self.assertEqual(len(save_payload["accounts"]), 2)
        connection = save_payload["workspace"]["connections"][0]
        self.assertEqual(connection["market_types"], ["spot", "swap"])
        self.assertEqual(
            {
                (row["market_type"], row["symbol"])
                for row in connection["markets"]
            },
            {("spot", "ACS/USDT"), ("swap", "ACS/USDT:USDT")},
        )
        self.assertEqual(len(save_payload["workspace"]["projects"]), 1)
        self.assertNotIn(b"unified-api-key", database_bytes)
        self.assertNotIn(b"unified-api-secret", database_bytes)


if __name__ == "__main__":
    unittest.main()
