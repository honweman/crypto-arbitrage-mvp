from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from arbitrage_bot.config import (
    AlertConfig,
    AssetLedgerConfig,
    AssetPosition,
    BacktestConfig,
    CashAndCarryPair,
    CrossExchangeRebalanceConfig,
    DcaConfig,
    ExchangeConfig,
    ExecutionAlgoConfig,
    MarketMakerConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
    SpotMarketConfig,
    StrategyTimelineConfig,
    TradeLogConfig,
    WebSecurityConfig,
)
from arbitrage_bot.asset_ledger import AssetLedgerStore
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.pnl import build_portfolio_pnl
from arbitrage_bot.order_reconciliation import _monitor_reconciliation_streak
from arbitrage_bot.trade_log import (
    _read_recent_event_lines,
    normalize_trade_event,
    write_trade_event,
)
from arbitrage_bot.web import (
    APP_JS,
    HTML as INDEX_HTML,
    MonitorState,
    SECURITY_HEADERS,
    STYLES_CSS,
    _add_security_headers,
    _filter_state_payload_for_user,
    _market_maker_force_replace_reason,
    _monitor_auto_stop_decision,
    _monitor_reconciliation_warmup_active,
    _owner_live_market_maker_order_activity,
    _owner_live_trading_console,
    _market_maker_order_sync_delta,
    _market_maker_overrides_from_payload,
    _cash_and_carry_pairs_from_payload,
    _backtest_overrides_from_payload,
    _dca_overrides_from_payload,
    _exchange_balance_symbols,
    _execution_algo_overrides_from_payload,
    _risk_overrides_from_payload,
    _slow_execution_overrides_from_payload,
    _spot_grid_overrides_from_payload,
    _spot_markets_from_payload,
    _daily_report_due,
    _global_scan_health_warnings,
    _require_admin_user,
    _require_user_assets,
    _client_ip,
    _ip_allowed,
    _make_session_token,
    _merge_workspace_account_balances,
    _session_identity,
    _session_valid,
    _sync_portfolio_with_account_balances,
    build_daily_report_message,
    default_web_audit_path,
    build_order_attribution_map,
    build_order_reconciliation_payload,
    build_market_maker_payload,
    build_market_maker_quality_payload,
    build_market_rows,
    build_backtest_payload,
    build_dca_payload,
    build_operations_payload,
    build_readiness_payload,
    build_slow_execution_payload,
    build_spot_grid_payload,
    build_execution_algo_payload,
    build_synced_portfolio_pnl,
    build_trading_console_payload,
    default_web_user_store_path,
    default_strategy_center_path,
    enrich_recent_trades_with_pnl,
    read_recent_web_audit_events,
    slow_execution_accounts,
    write_web_audit_event,
)
from arbitrage_bot.web.render_payloads import state_payload_for_view
from arbitrage_bot.web.routes import register_routes
from arbitrage_bot.web.loops import (
    _market_maker_fill_rows,
    _market_maker_runtime_open_orders,
)
from arbitrage_bot.web.state import MonitorState as SplitMonitorState
from arbitrage_bot.web.users import WebUserStore
from arbitrage_bot.web.background.monitor import (
    _ASSET_CHECKPOINT_WRITTEN_AT,
    _checkpoint_asset_state,
)
from arbitrage_bot.web_config import (
    cross_exchange_rebalance_config_from_payload,
    market_maker_config_from_payload,
    market_maker_configs_from_payload,
    strategy_universe_to_dict,
)
from arbitrage_bot.strategy_timeline import write_strategy_timeline_from_payload
from tests.web_test_support import make_config


HTML = f"{INDEX_HTML}\n{APP_JS}"


class WebMonitorTest(unittest.TestCase):
    def test_portfolio_performance_is_attached_between_ledger_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_cfg = AssetLedgerConfig(
                enabled=True,
                path=str(Path(temp_dir) / "asset-ledger.sqlite3"),
                checkpoint_interval_seconds=3600.0,
            )
            cfg = replace(make_config(), asset_ledger=ledger_cfg)
            balances = {
                "status": "ok",
                "accounts": [
                    {
                        "exchange": "coinbase-spot",
                        "status": "ok",
                        "errors": [],
                        "balance": {
                            "checked": True,
                            "currencies": [
                                {"currency": "ACS", "total": 100.0},
                                {"currency": "USDC", "total": 100.0},
                            ],
                        },
                    }
                ],
                "totals": [],
                "errors": [],
            }
            activity = {
                "status": "ok",
                "accounts": [],
                "open_orders": [],
                "recent_trades": [],
                "errors": [],
            }

            def portfolio(mark_price: float) -> dict:
                position_value = 100.0 * mark_price
                return {
                    "status": "ok",
                    "quote_currency": "USD",
                    "total_asset_currency": "USD",
                    "total_asset_value": 100.0 + position_value,
                    "positions": [
                        {
                            "asset": "ACS",
                            "position_base": 100.0,
                            "mark_price": mark_price,
                            "position_value": position_value,
                        }
                    ],
                    "position_value": position_value,
                    "cash_balances": {"USDC": 100.0},
                    "cash_balances_common": {"USDC": 100.0},
                    "cash_value": 100.0,
                    "position_missing_marks": [],
                    "cash_missing_rates": [],
                    "total_asset_missing_rates": [],
                }

            ledger_key = str(Path(ledger_cfg.path).resolve())
            _ASSET_CHECKPOINT_WRITTEN_AT.pop(ledger_key, None)
            initial = portfolio(0.2)
            _checkpoint_asset_state(cfg, balances, activity, initial)
            self.assertEqual(initial["performance"]["since_inception"]["pnl"], 0.0)

            refreshed = portfolio(0.25)
            _checkpoint_asset_state(cfg, balances, activity, refreshed)
            self.assertAlmostEqual(
                refreshed["performance"]["since_inception"]["pnl"],
                5.0,
            )
            self.assertAlmostEqual(
                refreshed["performance"]["rolling_24h"]["pnl"],
                5.0,
            )
            self.assertAlmostEqual(refreshed["rolling_24h_pnl"], 5.0)
            self.assertAlmostEqual(refreshed["daily_total_pnl"], 5.0)

    def test_market_maker_runtime_orders_keep_real_public_order_details(self) -> None:
        exchange = ExchangeConfig(
            id="bybit",
            label="workspace:connection-1:spot",
            display_label="Bybit Main",
        )
        maker = MarketMakerConfig(
            id="user-mm-123",
            exchange=exchange.key,
            symbol="ACS/USDT",
        )
        rows = _market_maker_runtime_open_orders(
            make_config(spot_exchanges=[exchange]),
            maker,
            {
                "open_orders": [
                    {
                        "id": "active-order",
                        "symbol": "ACS/USDT",
                        "side": "buy",
                        "type": "limit",
                        "status": "open",
                        "price": 0.102,
                        "amount": 25.0,
                        "filled": 0.0,
                        "remaining": 25.0,
                        "timestamp": 1_787_199_700_000,
                        "info": {"apiSecret": "must-not-leak"},
                    },
                    {
                        "id": "untracked-order",
                        "symbol": "ACS/USDT",
                        "price": 0.2,
                    },
                ]
            },
            ["active-order"],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "active-order")
        self.assertEqual(rows[0]["price"], 0.102)
        self.assertEqual(rows[0]["timestamp"], 1_787_199_700_000)
        self.assertEqual(rows[0]["label"], "Bybit Main")
        self.assertNotIn("info", rows[0])

    def test_user_market_maker_fill_rows_require_strategy_attribution(self) -> None:
        exchange = ExchangeConfig(
            id="bybit",
            label="workspace:connection-1:spot",
            display_label="Bybit Main",
        )
        maker = MarketMakerConfig(
            id="user-mm-123",
            exchange=exchange.key,
            symbol="ACS/USDT",
            client_order_prefix="arb-umm-strategy123",
        )
        raw_trades = [
            {
                "id": "fill-prefix",
                "order": "order-prefix",
                "symbol": "ACS/USDT",
                "side": "buy",
                "price": 0.1,
                "amount": 10.0,
                "cost": 1.0,
                "timestamp": 1000,
                "info": {
                    "orderLinkId": "arb-umm-strategy123-user-abc",
                },
            },
            {
                "id": "fill-order",
                "order": "known-order",
                "symbol": "ACS/USDT",
                "side": "sell",
                "price": 0.11,
                "amount": 10.0,
                "cost": 1.1,
                "timestamp": 2000,
                "info": {},
            },
            {
                "id": "fill-other",
                "order": "other-order",
                "symbol": "ACS/USDT",
                "side": "sell",
                "price": 0.12,
                "amount": 10.0,
                "cost": 1.2,
                "timestamp": 3000,
                "info": {"orderLinkId": "another-strategy"},
            },
        ]

        rows = _market_maker_fill_rows(
            exchange,
            maker,
            raw_trades,
            known_order_ids={"known-order"},
        )

        self.assertEqual([row["id"] for row in rows], ["fill-order", "fill-prefix"])
        self.assertTrue(all(row["source"] == "market_maker" for row in rows))
        self.assertTrue(
            all(row["strategy_instance_id"] == "user-mm-123" for row in rows)
        )

    def test_owner_market_maker_activity_reads_only_owner_strategy_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_cfg = AssetLedgerConfig(
                enabled=True,
                path=str(Path(temp_dir) / "asset-ledger.sqlite3"),
            )
            store = AssetLedgerStore(ledger_cfg)
            for instance_id in ("user-mm-owner", "user-mm-other"):
                store.record_fills(
                    account_key=f"workspace:{instance_id}:spot",
                    trades=[
                        {
                            "exchange": f"workspace:{instance_id}:spot",
                            "label": instance_id,
                            "id": f"fill-{instance_id}",
                            "order_id": f"order-{instance_id}",
                            "symbol": "ACS/USDT",
                            "side": "buy",
                            "price": 0.1,
                            "amount": 10.0,
                            "cost": 1.0,
                            "timestamp": 1000,
                            "source": "market_maker",
                        }
                    ],
                    source=f"market-maker:{instance_id}",
                    observed_at=1.0,
                )
            cfg = replace(make_config(), asset_ledger=ledger_cfg)
            payload = _owner_live_market_maker_order_activity(
                cfg,
                {
                    "strategies": [
                        {
                            "name": "Owner MM",
                            "strategy_type": "market_maker",
                            "mode": "live",
                            "runtime_instance_id": "user-mm-owner",
                            "live_runtime": {
                                "config": {
                                    "exchange": "workspace:user-mm-owner:spot",
                                    "symbol": "ACS/USDT",
                                },
                                "open_order_ids": ["open-owner"],
                                "open_orders": [
                                    {
                                        "exchange": "workspace:user-mm-owner:spot",
                                        "label": "Bybit Main",
                                        "id": "open-owner",
                                        "client_order_id": "mm-owner-1",
                                        "symbol": "ACS/USDT",
                                        "side": "sell",
                                        "type": "limit",
                                        "status": "open",
                                        "price": 0.11,
                                        "amount": 20.0,
                                        "filled": 0.0,
                                        "remaining": 20.0,
                                        "cost": 2.2,
                                        "fee": None,
                                        "timestamp": 1_787_199_700_000,
                                        "datetime": "2026-08-20T04:21:40Z",
                                    }
                                ],
                                "open_order_details_complete": True,
                                "updated_at": 2.0,
                            },
                        }
                    ]
                },
            )

        self.assertTrue(payload["owner_scoped"])
        self.assertEqual(payload["open_order_count"], 1)
        self.assertEqual(payload["open_orders"][0]["price"], 0.11)
        self.assertEqual(payload["open_orders"][0]["side"], "sell")
        self.assertEqual(
            payload["open_orders"][0]["timestamp"],
            1_787_199_700_000,
        )
        self.assertEqual(payload["recent_trade_count"], 1)
        self.assertEqual(payload["recent_trades"][0]["id"], "fill-user-mm-owner")

    def test_owner_trading_console_includes_mm_and_auto_buy_sell(self) -> None:
        exchange = ExchangeConfig(
            id="bybit",
            label="workspace:connection-1:spot",
            display_label="Bybit Main",
        )
        cfg = make_config(
            spot_exchanges=[exchange],
            risk=RiskConfig(allow_live_trading=True),
        )
        order_activity = {
            "open_orders": [{"exchange": exchange.key, "id": "order-1"}],
            "open_order_count": 1,
            "recent_trade_count": 2,
        }
        payload = _owner_live_trading_console(
            cfg,
            {
                "strategies": [
                    {
                        "id": "strategy-1",
                        "name": "ACS MM",
                        "strategy_type": "market_maker",
                        "enabled": True,
                        "effective_enabled": True,
                        "accounts": [
                            {
                                "exchange": "bybit",
                                "label": "Bybit Main",
                                "symbol": "ACS/USDT",
                            }
                        ],
                        "live_runtime": {
                            "status": "unchanged",
                            "mode": "live",
                            "config": {
                                "exchange": exchange.key,
                                "symbol": "ACS/USDT",
                            },
                        },
                    }
                ]
            },
            order_activity,
            {
                "tasks": {
                    "tasks": [
                        {
                            "id": "auto-1",
                            "status": "waiting_for_interval",
                            "config": {
                                "exchange": exchange.key,
                                "symbol": "ACS/USDT",
                            },
                        },
                        {
                            "id": "auto-complete",
                            "status": "complete",
                            "config": {
                                "exchange": exchange.key,
                                "symbol": "ACS/USDT",
                            },
                        },
                    ]
                }
            },
        )

        self.assertTrue(payload["owner_scoped"])
        self.assertTrue(payload["cancel_allowed"])
        self.assertEqual(payload["cancel_scope"], "owner")
        self.assertTrue(payload["live_trading"])
        self.assertEqual(payload["accounts"][0]["open_order_count"], 1)
        self.assertEqual(len(payload["strategies"]), 2)
        self.assertEqual(
            payload["strategies"][0]["owner_strategy_id"],
            "strategy-1",
        )
        self.assertEqual(
            payload["strategies"][1]["owner_auto_task_id"],
            "auto-1",
        )

    def test_non_admin_preserves_only_explicit_owner_console_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(
                email="admin@example.com",
                password="Strong-pass-1!",
            )
            user = store.create_user(
                email="owner@example.com",
                password="Strong-pass-1!",
            )
        cfg = make_config()
        owner_activity = {
            "status": "ok",
            "owner_scoped": True,
            "accounts": [],
            "open_orders": [{"symbol": "ACS/USDT", "id": "owner-order"}],
            "closed_orders": [],
            "recent_trades": [{"symbol": "ACS/USDT", "id": "owner-fill"}],
            "open_order_count": 1,
            "recent_trade_count": 1,
        }
        owner_console = {
            "status": "ok",
            "owner_scoped": True,
            "accounts": [],
            "strategies": [{"id": "owner-mm", "symbol": "ACS/USDT"}],
        }

        filtered = _filter_state_payload_for_user(
            {
                "config": {},
                "order_activity": owner_activity,
                "trading_console": owner_console,
            },
            cfg=cfg,
            user=user,
        )
        private = _filter_state_payload_for_user(
            {
                "config": {},
                "order_activity": {
                    "open_orders": [{"symbol": "ACS/USDT", "id": "platform"}]
                },
                "trading_console": {
                    "strategies": [{"id": "platform", "symbol": "ACS/USDT"}]
                },
            },
            cfg=cfg,
            user=user,
        )

        self.assertEqual(filtered["order_activity"]["open_order_count"], 1)
        self.assertEqual(filtered["trading_console"]["status"], "ok")
        self.assertEqual(private["order_activity"]["status"], "private")
        self.assertEqual(private["trading_console"]["status"], "private")

    def test_non_admin_does_not_receive_platform_strategy_risk_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            store.create_user(
                email="admin@example.com",
                password="Strong-pass-1!",
            )
            user = store.create_user(
                email="owner@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS"],
            )

        filtered = _filter_state_payload_for_user(
            {
                "status": "degraded",
                "warnings": [
                    "Market maker Coinbase ACS: blocked_by_risk (max exposure)",
                    "Account balances: administrator account unavailable",
                ],
                "program": {
                    "running": True,
                    "stop_reason": "Coinbase MM risk limit",
                },
                "config": {"risk": {"allow_live_trading": True}},
                "market_maker": {
                    "status": "blocked_by_risk",
                    "error": "Coinbase MM risk limit",
                    "instances": [
                        {
                            "config": {"symbol": "ACS/USDC"},
                            "status_reason": "max exposure",
                        }
                    ],
                },
                "strategy_lifecycle": {
                    "instances": [
                        {
                            "strategy_id": "market_maker",
                            "symbol": "ACS/USDC",
                            "converged": False,
                            "convergence_state": "blocked",
                            "reason": "administrator max exposure",
                        }
                    ]
                },
            },
            cfg=make_config(),
            user=user,
        )

        self.assertEqual(filtered["status"], "running")
        self.assertEqual(filtered["warnings"], [])
        self.assertEqual(filtered["program"]["stop_reason"], "")
        self.assertEqual(filtered["market_maker"]["status"], "platform_managed")
        self.assertEqual(filtered["strategy_lifecycle"]["status"], "private")
        self.assertNotIn("Coinbase", json.dumps(filtered))

    def test_page_builds_non_admin_risk_status_from_owner_workspace(self) -> None:
        self.assertIn('data.auth?.role !== "admin"', APP_JS)
        self.assertIn("data.user_workspace?.strategies || []", APP_JS)
        self.assertIn('strategy?.strategy_type !== "market_maker"', APP_JS)
        self.assertIn('runtime.status_reason || runtime.last_error', APP_JS)
        self.assertIn('stateStatus === "running" && statusIssueRows(data).length', APP_JS)

    def test_compact_account_balances_keep_platform_and_workspace_totals(self) -> None:
        merged = _merge_workspace_account_balances(
            {
                "status": "ok",
                "totals": [
                    {
                        "currency": "USDC",
                        "free": 50.0,
                        "used": 0.0,
                        "total": 50.0,
                    }
                ],
                "checked_account_count": 3,
                "total_account_count": 3,
                "last_finished": 100.0,
            },
            {
                "connections": [
                    {
                        "id": "bybit-main",
                        "label": "Bybit Main",
                        "exchange": "bybit",
                        "status": "healthy",
                        "checked_at": 200.0,
                        "credentials_configured": True,
                        "live_enabled": True,
                        "balances": [
                            {
                                "currency": "ACS",
                                "free": 110_000_000.0,
                                "used": 0.0,
                                "total": 110_000_000.0,
                                "wallet": "funding",
                                "tradable": False,
                            }
                        ],
                        "markets": [
                            {
                                "symbol": "ACS/USDT",
                                "connection_status": "healthy",
                            }
                        ],
                    }
                ]
            },
        )

        self.assertEqual(merged["checked_account_count"], 4)
        self.assertEqual(merged["total_account_count"], 4)
        self.assertEqual(
            {row["currency"]: row["total"] for row in merged["totals"]},
            {"ACS": 110_000_000.0, "USDC": 50.0},
        )

    def test_imported_workspace_balance_replaces_linked_platform_account(self) -> None:
        merged = _merge_workspace_account_balances(
            {
                "status": "ok",
                "accounts": [
                    {
                        "exchange": "bybit-spot",
                        "label": "bybit-spot",
                        "id": "bybit",
                        "status": "ok",
                        "balance": {
                            "checked": True,
                            "currencies": [{"currency": "USDT", "total": 100.0}],
                        },
                    },
                    {
                        "exchange": "coinbase-spot",
                        "label": "coinbase-spot",
                        "id": "coinbase",
                        "status": "ok",
                        "balance": {
                            "checked": True,
                            "currencies": [{"currency": "USDC", "total": 50.0}],
                        },
                    },
                ],
            },
            {
                "connections": [
                    {
                        "id": "bybit-main",
                        "label": "Bybit Main",
                        "exchange": "bybit",
                        "runtime_keys": ["bybit-spot"],
                        "status": "healthy",
                        "checked_at": 200.0,
                        "credentials_configured": True,
                        "balances": [{"currency": "USDT", "total": 100.0}],
                        "markets": [],
                    }
                ]
            },
        )

        self.assertEqual(merged["total_account_count"], 2)
        self.assertEqual(
            {row["label"] for row in merged["accounts"]},
            {"Bybit Main", "coinbase-spot"},
        )
        self.assertEqual(
            {row["currency"]: row["total"] for row in merged["totals"]},
            {"USDT": 100.0, "USDC": 50.0},
        )

    def test_workspace_balance_deduplicates_dynamic_account_runtime_key(self) -> None:
        merged = _merge_workspace_account_balances(
            {
                "status": "ok",
                "accounts": [
                    {
                        "exchange": "workspace:connection-gate-main:spot",
                        "label": "Gate.io Main - SPOT",
                        "id": "gateio",
                        "status": "ok",
                        "balance": {
                            "checked": True,
                            "currencies": [
                                {"currency": "ACS", "total": 1_000.0},
                                {"currency": "USDT", "total": 50.0},
                            ],
                        },
                    }
                ],
            },
            {
                "connections": [
                    {
                        "id": "connection-gate-main",
                        "label": "Gate.io Main",
                        "exchange": "gateio",
                        "runtime_keys": [],
                        "status": "healthy",
                        "checked_at": 200.0,
                        "credentials_configured": True,
                        "balances": [
                            {"currency": "ACS", "total": 1_000.0},
                            {"currency": "USDT", "total": 50.0},
                            {"currency": "GT", "total": 25.0},
                        ],
                        "markets": [],
                    }
                ]
            },
        )

        self.assertEqual(merged["total_account_count"], 1)
        self.assertEqual(merged["accounts"][0]["label"], "Gate.io Main")
        self.assertEqual(
            merged["accounts"][0]["workspace_connection_id"],
            "connection-gate-main",
        )
        self.assertEqual(
            {row["currency"]: row["total"] for row in merged["totals"]},
            {"ACS": 1_000.0, "GT": 25.0, "USDT": 50.0},
        )

    def test_workspace_balance_updates_top_portfolio_and_account_breakdown(
        self,
    ) -> None:
        account_balances = {
            "status": "ok",
            "last_finished": 200.0,
            "totals": [
                {"currency": "ACS", "total": 110_000_000.0},
                {"currency": "USDC", "total": 50.0},
            ],
            "accounts": [
                {
                    "label": "Bybit Main",
                    "id": "bybit",
                    "balance": {
                        "currencies": [
                            {
                                "currency": "ACS",
                                "total": 110_000_000.0,
                                "wallet": "funding",
                                "tradable": False,
                            }
                        ]
                    },
                }
            ],
        }
        portfolio = _sync_portfolio_with_account_balances(
            {
                "status": "ok",
                "asset": "ACS",
                "quote_currency": "USD",
                "position_base": 1.0,
                "position_value": 0.2,
                "positions": [
                    {
                        "asset": "ACS",
                        "position_base": 1.0,
                        "mark_price": 0.2,
                        "position_value": 0.2,
                    }
                ],
                "cash_balances": {},
                "cash_balances_common": {},
            },
            account_balances,
            quote_rates={"USDC": 1.0},
        )

        self.assertEqual(portfolio["position_base"], 110_000_000.0)
        self.assertEqual(portfolio["position_value"], 22_000_000.0)
        self.assertEqual(portfolio["cash_value"], 50.0)
        self.assertEqual(portfolio["total_asset_value"], 22_000_050.0)
        self.assertEqual(portfolio["total_asset_currency"], "USD")
        self.assertEqual(portfolio["total_asset_missing_rates"], [])
        self.assertEqual(
            portfolio["positions"][0]["account_breakdown"][0]["account"],
            "Bybit Main",
        )
        self.assertEqual(
            portfolio["positions"][0]["account_breakdown"][0]["wallet"],
            "funding",
        )

    def test_workspace_dynamic_balance_price_is_included_in_total_assets(self) -> None:
        account_balances = {
            "status": "ok",
            "last_finished": 200.0,
            "totals": [
                {"currency": "BNB", "total": 9.99250003},
                {"currency": "USDT", "total": 100.0},
            ],
            "accounts": [
                {
                    "label": "Binance Main",
                    "id": "binance",
                    "balance": {
                        "currencies": [
                            {
                                "currency": "BNB",
                                "total": 9.99250003,
                                "valuation_price": 600.0,
                                "valuation_quote": "USDT",
                            },
                            {"currency": "USDT", "total": 100.0},
                        ]
                    },
                }
            ],
        }

        portfolio = _sync_portfolio_with_account_balances(
            {
                "status": "private",
                "quote_currency": "USD",
                "positions": [],
                "cash_balances": {},
                "cash_balances_common": {},
            },
            account_balances,
            quote_rates={"USDT": 1.0},
        )

        self.assertAlmostEqual(portfolio["total_asset_value"], 6_095.500018)
        self.assertEqual(portfolio["total_asset_missing_rates"], [])
        self.assertAlmostEqual(
            account_balances["accounts"][0]["balance"]["currencies"][0]["value_common"],
            5_995.500018,
        )

    def test_page_uses_auto_buy_sell_label(self) -> None:
        self.assertIn(
            '<script src="/static/app.js?v=20260828-owner-risk1" defer></script>',
            INDEX_HTML,
        )
        self.assertIn(
            '<script src="/static/i18n.js?v=20260828-owner-risk1" defer></script>',
            INDEX_HTML,
        )
        self.assertIn(
            'id="user-workspace-notice" class="subtle" role="status"',
            INDEX_HTML,
        )
        self.assertIn('id="user-setup-readiness"', INDEX_HTML)

    def test_market_maker_status_shows_scheduled_recovery_countdown(self) -> None:
        self.assertIn("runtime.auto_recovery || {}", APP_JS)
        self.assertIn('autoRecovery.status === "waiting"', APP_JS)
        self.assertIn('uiText("Automatic recheck pending")', APP_JS)

    def test_page_exposes_explicit_perpetual_auto_buy_sell_actions(self) -> None:
        self.assertIn('<select id="slow-instrument-type">', INDEX_HTML)
        self.assertIn('<select id="slow-contract-action">', INDEX_HTML)
        self.assertIn(
            '<option value="open_long">Open / Increase Long</option>', INDEX_HTML
        )
        self.assertIn(
            '<option value="close_long">Close Long (Reduce Only)</option>', INDEX_HTML
        )
        self.assertIn(
            '<option value="open_short">Open / Increase Short</option>', INDEX_HTML
        )
        self.assertIn(
            '<option value="close_short">Close Short (Reduce Only)</option>', INDEX_HTML
        )
        self.assertNotIn('id="slow-position-effect"', INDEX_HTML)
        self.assertNotIn('id="slow-position-side"', INDEX_HTML)

    def test_balance_account_filter_defaults_to_all_after_page_reload(self) -> None:
        self.assertIn(
            "function accountBalanceFilterKey(account)",
            APP_JS,
        )
        self.assertIn(
            'const previous = select.value || "";',
            APP_JS,
        )
        self.assertIn(
            "renderProfileAccounts(data.account_balances);",
            APP_JS,
        )
        self.assertNotIn(
            'localStorage.getItem("profile-account")',
            APP_JS,
        )
        self.assertNotIn(
            'localStorage.setItem("profile-account"',
            APP_JS,
        )
        summary_body = APP_JS.split(
            "function renderAccountBalanceSummary(accountBalances)", 1
        )[1].split("function renderAccountBalances(accountBalances)", 1)[0]
        self.assertNotIn("accountBalancesForProfile", summary_body)
        self.assertIn("BALANCE_MIN_QUANTITY = 1", APP_JS)
        self.assertIn("BALANCE_MIN_VALUE_USDT = 10", APP_JS)
        self.assertIn(
            "function meetsBalanceDisplayThreshold(amount, valueCommon)", APP_JS
        )
        self.assertIn("if (valueCommon == null) return false;", APP_JS)
        self.assertIn("USD_STABLE_CURRENCIES.has(currency)", APP_JS)
        self.assertIn("function visibleBalanceRows(rows)", APP_JS)
        self.assertIn('function displayExchange(exchange, explicitLabel = "")', APP_JS)
        self.assertIn("friendlyAccountMessage(value)", APP_JS)
        self.assertIn('uiText("All accounts")', summary_body)
        self.assertIn('id="user-exchange-test"', INDEX_HTML)
        self.assertIn('id="user-exchange-save-test"', INDEX_HTML)
        self.assertIn('id="user-exchange-egress-mode"', INDEX_HTML)
        self.assertIn('id="user-exchange-source-ip"', INDEX_HTML)
        self.assertIn('id="user-exchange-expected-ip"', INDEX_HTML)
        self.assertIn('id="user-exchange-proxy-url"', INDEX_HTML)
        self.assertIn(
            "Another account already uses this exchange.",
            APP_JS,
        )
        self.assertIn("<strong>Unified Access</strong>", INDEX_HTML)
        self.assertIn(
            "without separate project approval",
            INDEX_HTML,
        )
        self.assertIn("<summary>Exchange Accounts</summary>", INDEX_HTML)
        self.assertIn("<summary>Project Management</summary>", INDEX_HTML)
        self.assertIn('id="profile-account"', INDEX_HTML)
        self.assertNotIn('id="user-account-monitor-rows"', INDEX_HTML)
        self.assertNotIn('id="user-account-trading-rows"', INDEX_HTML)
        self.assertNotIn('id="user-account-quant-rows"', INDEX_HTML)
        self.assertIn('id="user-exchange-project"></select>', INDEX_HTML)
        self.assertNotIn('id="user-exchange-project" required', INDEX_HTML)
        self.assertNotIn('id="user-exchange-symbol" required', INDEX_HTML)
        self.assertIn("<summary>Advanced Risk Profile</summary>", INDEX_HTML)
        self.assertIn('id="user-strategy-lab"', INDEX_HTML)
        self.assertNotIn(
            'id="user-project-name" type="text" maxlength="80" placeholder="ACS Trading" required',
            INDEX_HTML,
        )
        self.assertIn('id="backtest-section"', INDEX_HTML)
        self.assertIn('id="backtest-run"', INDEX_HTML)
        self.assertIn("Uses public historical candles", INDEX_HTML)
        self.assertNotIn(
            'data-ui-feature="backtest" data-ui-hidden-default="true"',
            INDEX_HTML,
        )

    def test_market_maker_keeps_core_controls_visible_and_advanced_controls_collapsed(
        self,
    ) -> None:
        self.assertIn('<div class="form-divider">Core Settings</div>', INDEX_HTML)
        self.assertIn('<div class="form-divider">Per-MM Limits</div>', INDEX_HTML)
        self.assertEqual(INDEX_HTML.count('<details class="mm-advanced">'), 3)
        self.assertIn("<summary>Advanced Ladder &amp; Execution</summary>", INDEX_HTML)
        self.assertIn("<summary>Advanced Risk</summary>", INDEX_HTML)
        self.assertIn("<summary>Inventory Control</summary>", INDEX_HTML)
        self.assertNotIn('<details class="mm-advanced" open>', INDEX_HTML)
        for field_id in (
            "mm-max-order",
            "mm-max-cycle",
            "mm-max-open-orders",
            "mm-max-cancels",
            "mm-max-slippage",
            "mm-max-gap",
            "mm-max-book-age",
        ):
            self.assertEqual(INDEX_HTML.count(f'id="{field_id}"'), 1)

    def test_page_supports_korean_language_option(self) -> None:
        i18n_js = Path("src/arbitrage_bot/web/static/i18n.js").read_text(
            encoding="utf-8",
        )
        self.assertIn('<option value="ko">한국어</option>', INDEX_HTML)
        self.assertIn('"ko"', i18n_js)
        self.assertIn('"Language": "언어"', i18n_js)
        self.assertIn('"Account / Project / Exchange / Pair"', i18n_js)
        self.assertIn('"Continue Setup": "설정 계속"', i18n_js)
        self.assertIn('"ko-KR"', i18n_js)

    def test_settings_support_verified_browser_wallets_and_dex_venues(self) -> None:
        for element_id in (
            "wallet-provider-select",
            "wallet-connect",
            "wallet-open-imtoken",
            "wallet-link-select",
            "wallet-venue-select",
            "wallet-venue-test",
            "wallet-venue-refresh-all",
            "wallet-connections",
            "wallet-venue-connections",
        ):
            self.assertIn(f'id="{element_id}"', INDEX_HTML)
        self.assertIn("eip6963:requestProvider", APP_JS)
        self.assertIn("eth_requestAccounts", APP_JS)
        self.assertIn('method: "personal_sign"', APP_JS)
        self.assertIn('action: "wallet_challenge"', APP_JS)
        self.assertIn('action: "verify_wallet"', APP_JS)
        self.assertIn('action: "test_wallet_venue"', APP_JS)
        self.assertIn('action: "refresh_venue_connection"', APP_JS)
        self.assertIn('action: "refresh_all_venue_connections"', APP_JS)
        self.assertIn('action: "delete_venue_connection"', APP_JS)

    def test_core_strategy_forms_use_review_start_workflows(self) -> None:
        for element_id in (
            "mm-workflow",
            "mm-start",
            "mm-stop",
            "slow-workflow",
            "slow-create-task",
            "rebalance-readiness",
            "rebalance-stop",
            "spot-workflow",
        ):
            self.assertIn(f'id="{element_id}"', INDEX_HTML)
        self.assertIn(">Save Defaults</button>", INDEX_HTML)
        self.assertIn(">Review &amp; Start</button>", INDEX_HTML)
        self.assertIn(">Review &amp; Start Live</button>", INDEX_HTML)
        self.assertIn('class="check-field strategy-internal-state" hidden', INDEX_HTML)
        self.assertIn("function coreLiveRiskReadiness", APP_JS)
        self.assertIn("function strategyLifecycleRows", APP_JS)
        self.assertIn("function lifecycleWorkflowStep", APP_JS)
        self.assertIn("data?.strategy_lifecycle?.instances", APP_JS)
        self.assertIn("function startMarketMaker", APP_JS)
        self.assertIn("function stopCrossExchangeRebalance", APP_JS)
        self.assertIn("function applyMarketMakerMutationResult", APP_JS)
        self.assertIn("function scheduleMutationRefresh", APP_JS)
        self.assertIn("LIVE_AUTO_BUY_SELL_CONFIRMATION", APP_JS)
        self.assertIn("LIVE_MARKET_MAKER_CONFIRMATION", APP_JS)
        self.assertIn(".strategy-internal-state[hidden]", STYLES_CSS)

    def test_page_includes_persistent_dark_mode_toggle(self) -> None:
        theme_js = Path("src/arbitrage_bot/web/static/theme.js").read_text(
            encoding="utf-8",
        )
        self.assertIn('id="theme-toggle"', INDEX_HTML)
        self.assertIn('title="Dark mode"', INDEX_HTML)
        self.assertIn(
            '<script src="/static/theme.js?v=20260821-status-detail1"></script>',
            INDEX_HTML,
        )
        self.assertLess(
            INDEX_HTML.index("/static/theme.js?v=20260821-status-detail1"),
            INDEX_HTML.index("/static/styles.css?v=20260828-owner-risk1"),
        )
        self.assertIn('const STORAGE_KEY = "cryptoArbTheme"', theme_js)
        self.assertIn("root.dataset.theme = theme", theme_js)
        self.assertIn(':root[data-theme="dark"]', STYLES_CSS)
        self.assertIn(":root:not([data-theme])", STYLES_CSS)

    def test_page_includes_owner_live_market_maker_controls(self) -> None:
        self.assertIn('id="user-strategy-form"', INDEX_HTML)
        self.assertIn('id="user-strategy-accounts"', INDEX_HTML)
        self.assertIn('id="user-strategy-risk-order"', INDEX_HTML)
        self.assertIn('id="user-strategy-risk-total"', INDEX_HTML)
        self.assertIn('id="user-strategy-risk-fee"', INDEX_HTML)
        self.assertIn('id="user-strategy-mm-reprice"', INDEX_HTML)
        self.assertIn('id="user-strategy-mm-inventory-enabled"', INDEX_HTML)
        self.assertIn('id="user-strategies"', INDEX_HTML)
        strategy_form = INDEX_HTML.split('id="user-strategy-form"', 1)[1].split(
            "</form>",
            1,
        )[0]
        self.assertIn("Live Enabled", strategy_form)
        self.assertIn("Live orders use only the selected account", strategy_form)
        self.assertNotIn("Paper simulation only", strategy_form)

    def test_market_maker_payload_keeps_multiple_instances(self) -> None:
        coinbase = MarketMakerConfig(
            id="coinbase-acs",
            enabled=True,
            exchange="coinbase-spot",
            symbol="ACS/USDC",
            levels=1,
            quote_per_level=1.0,
        )
        upbit = MarketMakerConfig(
            id="upbit-acs",
            enabled=True,
            exchange="upbit-spot",
            symbol="ACS/USDT",
            levels=1,
            quote_per_level=1.0,
        )
        cfg = make_config(
            market_maker=coinbase,
            market_makers=[coinbase, upbit],
            spot_exchanges=[
                ExchangeConfig(
                    id="coinbase", label="coinbase-spot", market_type="spot"
                ),
                ExchangeConfig(id="upbit", label="upbit-spot", market_type="spot"),
            ],
        )
        books = {
            ("coinbase-spot", "ACS/USDC"): OrderBookSnapshot(
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                bids=[BookLevel(price=0.2, amount=100.0)],
                asks=[BookLevel(price=0.22, amount=100.0)],
            ),
            ("upbit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="upbit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.21, amount=100.0)],
                asks=[BookLevel(price=0.23, amount=100.0)],
            ),
        }

        payload = build_market_maker_payload(cfg, books)

        self.assertEqual(payload["instance_count"], 2)
        self.assertEqual(
            [item["config"]["id"] for item in payload["instances"]],
            ["coinbase-acs", "upbit-acs"],
        )
        self.assertEqual(payload["instances"][0]["plan"]["symbol"], "ACS/USDC")
        self.assertEqual(payload["instances"][1]["plan"]["symbol"], "ACS/USDT")

    def test_order_reconciliation_tracks_market_maker_instances(self) -> None:
        payload = build_order_reconciliation_payload(
            {
                "open_orders": [
                    {"exchange": "coinbase-spot", "symbol": "ACS/USDC", "id": "a"},
                    {"exchange": "upbit-spot", "symbol": "ACS/USDT", "id": "b"},
                ],
                "closed_orders": [],
                "recent_trades": [],
            },
            market_maker_runtime={
                "instances": [
                    {
                        "id": "coinbase-acs",
                        "open_order_exchange": "coinbase-spot",
                        "open_order_symbol": "ACS/USDC",
                        "open_order_ids": ["a"],
                    },
                    {
                        "id": "upbit-acs",
                        "open_order_exchange": "upbit-spot",
                        "open_order_symbol": "ACS/USDT",
                        "open_order_ids": ["b"],
                    },
                ],
            },
        )

        self.assertEqual(payload["tracked_order_count"], 2)
        self.assertEqual(payload["matched_open_count"], 2)
        self.assertEqual(payload["issue_count"], 0)
        self.assertIn(
            '<link rel="stylesheet" href="/static/styles.css?v=20260828-owner-risk1">',
            INDEX_HTML,
        )
        self.assertIn("Auto Buy/Sell", HTML)
        self.assertIn("/api/auto-buy-sell", HTML)
        self.assertIn("/api/auto-buy-sell/tasks", HTML)
        self.assertIn('id="slow-create-task"', HTML)
        self.assertIn('id="slow-clear-terminal"', HTML)
        self.assertIn('id="slow-config-status"', HTML)
        self.assertIn('id="slow-cleanup-preview"', HTML)
        self.assertIn('id="slow-tasks"', HTML)
        self.assertIn('id="slow-start-price"', HTML)
        self.assertIn('id="slow-total-base-label"', HTML)
        self.assertIn('id="slow-total-quote-label"', HTML)
        self.assertIn('id="slow-slice-min-label"', HTML)
        self.assertIn('id="slow-slice-max-label"', HTML)
        self.assertIn('id="slow-start-price-label"', HTML)
        self.assertIn('id="slow-stop-price-label"', HTML)
        self.assertIn("Cleanup preview", APP_JS)
        self.assertIn("Same as default", APP_JS)
        self.assertIn("config-diff-details", APP_JS)
        self.assertIn("config-diff-grid", APP_JS)
        self.assertIn("AutoBuy start: Ask <=", APP_JS)
        self.assertIn("AutoSell start: Bid >=", APP_JS)
        self.assertIn("AutoBuy stop: Ask >=", APP_JS)
        self.assertIn("Ask >=", APP_JS)
        self.assertIn("AutoBuy stops before each execution", APP_JS)
        self.assertIn("AutoBuy stop when Ask >= price", APP_JS)
        self.assertNotIn("Slow Execution", HTML)

    def test_market_maker_runtime_surfaces_problem_instance_reason(self) -> None:
        async def run() -> None:
            cfg = make_config(
                market_makers=[
                    MarketMakerConfig(
                        id="coinbase-acs",
                        exchange="coinbase-spot",
                        symbol="ACS/USDC",
                        enabled=True,
                        live_enabled=True,
                    ),
                    MarketMakerConfig(
                        id="bybit-acs",
                        exchange="bybit-spot",
                        symbol="ACS/USDT",
                        enabled=True,
                        live_enabled=True,
                    ),
                    MarketMakerConfig(
                        id="upbit-acs",
                        exchange="upbit-spot",
                        symbol="ACS/USDT",
                        enabled=True,
                        live_enabled=True,
                    ),
                ],
            )
            state = MonitorState(cfg, 1.0)

            await state.set_market_maker_instance_runtime(
                "coinbase-acs",
                {
                    "status": "unchanged",
                    "mode": "live",
                    "open_order_count": 40,
                    "placed_count": 40,
                    "canceled_count": 0,
                },
            )
            await state.set_market_maker_instance_runtime(
                "bybit-acs",
                {
                    "status": "open_order_sync_error",
                    "mode": "live",
                    "open_order_sync_error": 'AuthenticationError: bybit requires "apiKey" credential',
                },
            )
            await state.set_market_maker_instance_runtime(
                "upbit-acs",
                {
                    "status": "blocked_by_risk",
                    "mode": "live",
                    "last_risk": {
                        "reasons": [
                            "order book gap 6648.98 bps exceeds max_order_book_gap_bps 5000.00"
                        ]
                    },
                },
            )

            runtime = await state.market_maker_runtime()
            self.assertEqual(runtime["status"], "open_order_sync_error")
            self.assertEqual(runtime["problem_instance_count"], 2)
            self.assertIn("apiKey", runtime["status_reason"])
            bybit = next(
                item for item in runtime["instances"] if item["id"] == "bybit-acs"
            )
            upbit = next(
                item for item in runtime["instances"] if item["id"] == "upbit-acs"
            )
            self.assertIn("apiKey", bybit["status_reason"])
            self.assertIn("order book gap", upbit["status_reason"])

        asyncio.run(run())

    def test_web_package_exposes_split_modules(self) -> None:
        self.assertIs(SplitMonitorState, MonitorState)
        self.assertTrue(callable(register_routes))
        self.assertEqual(
            state_payload_for_view({"status": "running"}, None),
            {"status": "running"},
        )

    def test_state_payload_can_be_limited_to_open_sections(self) -> None:
        payload = {
            "status": "running",
            "config": {"notional_quote": 1, "strategy_universe": {"assets": ["ACS"]}},
            "operations": {
                "risk": {"max_order_quote": 5},
                "web_audit": {"recent_events": [{"id": "audit"}]},
            },
            "order_activity": {
                "open_order_count": 1,
                "open_orders": [{"id": "order"}],
            },
            "market_maker": {
                "status": "planned",
                "instances": [
                    {
                        "config": {"id": "coinbase"},
                        "plan": {"orders": [{"id": "mm-order"}], "mid_price": 1.0},
                        "runtime": {
                            "last_plan": {
                                "orders": [{"id": "runtime-order"}],
                                "mid_price": 1.0,
                            }
                        },
                    }
                ],
            },
            "strategy_center": {
                "summary": {"strategy_count": 1},
                "strategy_instances": [{"id": "mm"}],
            },
            "funding_basis": {"status": "ok", "rows": [{"id": "basis"}]},
            "options_arbitrage": {"status": "ok", "rows": [{"id": "option"}]},
            "contract_strategies": {"status": "ok", "rows": [{"id": "contract"}]},
            "derivatives": {"status": "ok", "positions": [{"id": "position"}]},
            "account_balances": {
                "status": "ok",
                "totals": [{"currency": "USDC", "total": 10}],
                "accounts": [{"id": "coinbase"}],
            },
            "markets": [{"exchange": "coinbase-spot"}],
            "quote_rates": {"USD": 1.0},
            "readiness": {"actions": [{"id": "risk"}]},
            "onchain": {
                "holders": [{"rank": 1}],
                "history": {"events": [{"id": "wallet"}]},
            },
        }
        status_overview = state_payload_for_view(payload, "status", sections="overview")
        balance_detail = state_payload_for_view(
            payload,
            "balances",
            sections="account-balances",
        )
        quant_overview = state_payload_for_view(
            payload, "quant", sections="backtest-points"
        )
        quant_derivatives = state_payload_for_view(
            payload,
            "quant",
            sections="derivatives-risk,funding-basis,contract-strategies,options-arbitrage",
        )
        settings = state_payload_for_view(payload, "settings", sections="risk-form")
        records = state_payload_for_view(
            payload, "records", sections="console-strategies"
        )
        records_open_orders = state_payload_for_view(
            payload,
            "records",
            sections="console-open-orders",
        )
        status_holders = state_payload_for_view(
            payload,
            "status",
            sections="holders",
        )
        records_holders = state_payload_for_view(
            payload,
            "records",
            sections="holder-changes",
        )

        self.assertEqual(status_overview["markets"], [])
        self.assertEqual(status_overview["quote_rates"], {"USD": 1.0})
        self.assertEqual(status_overview["readiness"], {})
        self.assertIn("totals", status_overview["account_balances"])
        self.assertNotIn("accounts", status_overview["account_balances"])
        self.assertEqual(balance_detail["status"], "running")
        self.assertEqual(
            balance_detail["account_balances"]["accounts"],
            [{"id": "coinbase"}],
        )
        self.assertEqual(balance_detail["quote_rates"], {"USD": 1.0})
        self.assertNotIn("market_maker", balance_detail)
        self.assertNotIn("operations", balance_detail)
        self.assertNotIn("derivatives", status_overview)
        self.assertNotIn(
            "orders",
            status_overview["market_maker"]["instances"][0]["plan"],
        )
        self.assertNotIn(
            "orders",
            status_overview["market_maker"]["instances"][0]["runtime"]["last_plan"],
        )
        self.assertNotIn("rows", status_overview["funding_basis"])
        self.assertNotIn("rows", status_overview["options_arbitrage"])
        self.assertNotIn("rows", status_overview["contract_strategies"])
        self.assertNotIn("holders", status_overview["onchain"])
        self.assertNotIn("positions", quant_overview["derivatives"])
        self.assertIn("positions", quant_derivatives["derivatives"])
        self.assertIn("rows", quant_derivatives["funding_basis"])
        self.assertIn("rows", quant_derivatives["options_arbitrage"])
        self.assertIn("rows", quant_derivatives["contract_strategies"])
        self.assertNotIn("strategy_universe", settings["config"])
        self.assertNotIn("strategy_instances", settings["strategy_center"])
        self.assertIn("open_orders", records["order_activity"])
        self.assertIn("open_orders", records_open_orders["order_activity"])
        self.assertNotIn("web_audit", records["operations"])
        self.assertNotIn("events", records["onchain"].get("history", {}))
        self.assertEqual(status_holders["onchain"]["holders"], [{"rank": 1}])
        self.assertEqual(
            records_holders["onchain"]["history"]["events"],
            [{"id": "wallet"}],
        )

    def test_monitor_state_caches_view_payloads_and_invalidates_on_update(self) -> None:
        cfg = make_config()

        async def run() -> None:
            state = SplitMonitorState(cfg, cfg.poll_seconds)
            with patch(
                "arbitrage_bot.web.state.state_payload_for_view",
                wraps=state_payload_for_view,
            ) as mocked_payload_for_view:
                first = await state.get(view="status", sections="overview")
                second = await state.get(view="status", sections="overview")
                await state.set_order_activity(
                    {
                        "status": "ok",
                        "open_order_count": 2,
                        "open_orders": [],
                    }
                )
                third = await state.get(view="status", sections="overview")

            self.assertEqual(first["status"], "starting")
            self.assertEqual(second["status"], "starting")
            self.assertEqual(third["order_activity"]["open_order_count"], 2)
            self.assertEqual(mocked_payload_for_view.call_count, 2)

        asyncio.run(run())

    def test_page_uses_generic_dashboard_title(self) -> None:
        self.assertIn("Crypto Trading Dashboard", HTML)
        self.assertIn("Multi-asset arbitrage", HTML)
        self.assertNotIn("ACS Arbitrage Monitor", HTML)

    def test_page_includes_user_profile_asset_switcher(self) -> None:
        self.assertIn('id="user-profile"', HTML)
        self.assertIn('id="user-email"', HTML)
        self.assertIn('id="profile-asset"', HTML)
        self.assertIn("/api/profile", HTML)
        self.assertIn("function renderAuthProfile", HTML)

    def test_page_includes_strategy_center_controls(self) -> None:
        self.assertIn("Strategy Center", HTML)
        self.assertIn("User API Accounts", HTML)
        self.assertIn("Funding Arbitrage", HTML)
        self.assertIn("Signal Bot", HTML)
        self.assertIn("/api/strategy-center", HTML)
        self.assertIn("/api/signal/tradingview", HTML)
        self.assertIn('id="strategy-center-form"', HTML)
        self.assertIn('id="api-account-form"', HTML)
        self.assertIn('id="funding-arb-form"', HTML)
        self.assertIn('id="signal-bot-form"', HTML)
        self.assertIn('id="strategy-instance-exchange"', HTML)
        self.assertIn('id="strategy-instance-symbol"', HTML)
        self.assertIn("renderStrategyInstanceMarketOptions", APP_JS)

    def test_page_separates_core_trading_and_quant_modules(self) -> None:
        self.assertIn('id="overview" data-page="status"', HTML)
        self.assertIn('id="mm-section" data-page="trading"', HTML)
        self.assertIn('id="slow-section" data-page="trading"', HTML)
        self.assertIn('id="spot-arbitrage-section" data-page="trading"', HTML)
        self.assertIn('id="rebalance-section" data-page="trading"', HTML)
        self.assertIn('id="cash-carry-section" data-page="quant"', HTML)
        self.assertIn('id="derivatives-section" data-page="quant"', HTML)
        self.assertIn('id="funding-arbitrage-section" data-page="quant"', HTML)
        self.assertIn('id="signal-bot-section" data-page="quant"', HTML)
        self.assertIn('id="options-arbitrage-section" data-page="quant"', HTML)
        self.assertIn('id="contract-strategies-section" data-page="quant"', HTML)
        self.assertIn('id="spot-grid-section" data-page="quant"', HTML)
        self.assertIn('id="dca-section" data-page="quant"', HTML)
        self.assertIn('id="execution-section" data-page="quant"', HTML)
        self.assertIn('id="backtest-section" data-page="quant"', HTML)
        self.assertIn('id="user-quant-strategies-section" data-page="quant"', HTML)
        self.assertNotIn(
            'id="user-quant-strategies-section" data-page="quant" class="compact-section section-open" data-platform-only',
            HTML,
        )
        self.assertIn('id="user-market-maker-section" data-page="trading"', HTML)
        self.assertIn("data-owner-only", HTML)
        self.assertIn('id="user-strategy-lab"', HTML)
        self.assertIn("function applyRoleVisibility", APP_JS)
        self.assertIn('sectionId === "risk-section"', APP_JS)
        self.assertIn(
            'resolvedSectionId = ownerRisk ? "user-workspace-section"', APP_JS
        )
        self.assertIn("function renderUserQuantStrategies", APP_JS)
        self.assertIn("function renderUserMarketMakerStrategies", APP_JS)
        self.assertIn("function existingMarketMakerForAccounts", APP_JS)
        self.assertIn(
            'userStrategyViewFilter = ownerMarketMaker ? "market_maker" : ""',
            APP_JS,
        )
        self.assertIn("data-platform-only", HTML)
        self.assertIn('data-ui-feature="readiness" data-ui-hidden-default="true"', HTML)
        self.assertIn(
            'data-ui-feature="scan_status" data-ui-hidden-default="true"', HTML
        )
        self.assertIn(
            'data-ui-feature="orders_detail" data-ui-hidden-default="true"', HTML
        )
        self.assertIn(
            'data-ui-feature="strategy_timeline" data-ui-hidden-default="true"', HTML
        )
        self.assertIn(
            'data-ui-feature="audit_trail" data-ui-hidden-default="true"', HTML
        )
        self.assertIn(
            'data-ui-feature="quote_rates" data-ui-hidden-default="true"', HTML
        )
        self.assertIn('data-ui-feature="onchain_monitor"', HTML)
        self.assertIn('data-ui-feature="onchain_history"', HTML)
        self.assertIn('id="onchain-monitor-section" data-page="status"', HTML)
        self.assertIn('id="onchain-history-section" data-page="records"', HTML)
        self.assertIn('page === "status"', APP_JS)
        self.assertIn('overview.insertAdjacentElement("afterend", onchain)', APP_JS)
        self.assertIn(
            'class="compact-section" data-ui-feature="onchain_monitor"',
            HTML,
        )
        self.assertIn(
            'class="compact-section" data-ui-feature="onchain_history"',
            HTML,
        )
        self.assertNotIn(
            'data-ui-feature="onchain_monitor" data-ui-hidden-default="true"', HTML
        )
        self.assertNotIn(
            'data-ui-feature="onchain_history" data-ui-hidden-default="true"', HTML
        )
        self.assertNotIn('"onchain_monitor",', APP_JS)
        self.assertNotIn('"onchain_history",', APP_JS)
        self.assertIn("const HIDDEN_UI_FEATURES = new Set", APP_JS)
        self.assertIn("function applyFeatureVisibility", APP_JS)
        self.assertIn("status: [", APP_JS)
        self.assertIn("trading: [", APP_JS)
        self.assertIn("quant: [", APP_JS)
        self.assertIn(".ui-feature-hidden", STYLES_CSS)
        self.assertIn("[data-page].ui-feature-hidden", STYLES_CSS)
        self.assertIn(".statusbar[data-page].ui-feature-hidden", STYLES_CSS)

    def test_page_has_monitor_trading_quant_settings_and_records_views(self) -> None:
        self.assertIn('data-view-tab="status"', HTML)
        self.assertIn('data-view-tab="trading"', HTML)
        self.assertIn('data-view-tab="quant"', HTML)
        self.assertIn('data-view-tab="settings"', HTML)
        self.assertIn('data-view-tab="records"', HTML)
        self.assertIn('href="#status"', HTML)
        self.assertIn('href="#trading"', HTML)
        self.assertIn('href="#quant"', HTML)
        self.assertIn('href="#settings"', HTML)
        self.assertIn('href="#records"', HTML)
        self.assertIn(
            'const PAGE_IDS = new Set(["status", "trading", "quant", "settings", "records"])',
            HTML,
        )
        self.assertIn('if (hashPage === "monitor") return "status";', HTML)
        self.assertIn('if (hashPage === "control") return "trading";', HTML)
        self.assertIn("new URLSearchParams", HTML)
        self.assertIn("/api/state?${params.toString()}", HTML)
        self.assertEqual(
            APP_JS.count('params.set("sections", sectionIds.join(","));'),
            2,
        )
        self.assertIn("pageStateCache", HTML)

    def test_page_softens_initial_state_fetch_failure(self) -> None:
        self.assertIn("let refreshHadSuccess = false", HTML)
        self.assertIn("let refreshInFlight = false", HTML)
        self.assertIn("STATE_FETCH_TIMEOUT_MS", HTML)
        self.assertIn("AbortController", HTML)
        self.assertIn("if (res.status === 401)", HTML)
        self.assertIn('setHeaderStatus("degraded", "Retrying",', HTML)
        self.assertNotIn('status.className = "pill error";', HTML)

    def test_page_includes_market_config_controls(self) -> None:
        self.assertIn("Markets", HTML)
        self.assertIn("/api/markets", HTML)
        self.assertIn('id="markets-form"', HTML)
        self.assertIn('id="market-symbol"', HTML)
        self.assertIn('id="markets-config"', HTML)

    def test_account_symbol_selector_preserves_configured_symbol(self) -> None:
        self.assertIn(
            "preferredSymbol && !symbols.includes(preferredSymbol)",
            HTML,
        )
        self.assertIn("symbols.unshift(preferredSymbol)", HTML)

    def test_page_includes_cash_and_carry_config_controls(self) -> None:
        self.assertIn("Cash & Carry Pairs", HTML)
        self.assertIn("/api/cash-and-carry-pairs", HTML)
        self.assertIn('id="carry-form"', HTML)
        self.assertIn('id="carry-derivative-symbol"', HTML)
        self.assertIn('id="carry-config"', HTML)

    def test_page_includes_account_balances(self) -> None:
        self.assertIn("Account Balances", HTML)
        self.assertIn('id="account-balances"', HTML)
        self.assertIn('id="account-balances-head"', HTML)
        self.assertIn('id="account-balances-foot"', HTML)
        self.assertIn("balance-matrix-table", HTML)
        self.assertNotIn('id="account-balance-cards"', HTML)
        self.assertIn('id="balance-currency-filter"', HTML)
        self.assertIn('id="account-balances-refresh"', HTML)
        self.assertIn("balanceAccountHeader", HTML)
        self.assertIn("aggregateBalanceCurrencies", HTML)
        matrix_head = HTML.split('head.innerHTML = `', 1)[1].split('`;', 1)[0]
        self.assertLess(matrix_head.index('uiText("Total")'), matrix_head.index("accounts.map"))
        self.assertLess(matrix_head.index('uiText("Price")'), matrix_head.index("accounts.map"))
        self.assertLess(matrix_head.index('uiText("Value")'), matrix_head.index("accounts.map"))
        self.assertIn("loadAccountBalanceDetails", HTML)
        self.assertIn('view=balances&sections=account-balances', HTML)
        self.assertIn("status: 1000", HTML)

    def test_page_includes_derivatives_risk_panel(self) -> None:
        self.assertIn("Derivatives Risk", HTML)
        self.assertIn('id="derivatives-risk"', HTML)
        self.assertIn('id="derivatives-risk-meta"', HTML)
        self.assertIn("Funding / Basis", HTML)
        self.assertIn('id="funding-basis"', HTML)
        self.assertIn('id="funding-basis-meta"', HTML)
        self.assertIn("Contract Strategies", HTML)
        self.assertIn('id="contract-strategies"', HTML)
        self.assertIn('id="contract-strategies-meta"', HTML)
        self.assertIn('id="contract-strategies-summary"', HTML)
        self.assertIn("renderContractStrategies", HTML)
        self.assertIn("Options Arbitrage", HTML)
        self.assertIn('id="options-arbitrage"', HTML)
        self.assertIn('id="options-arbitrage-meta"', HTML)
        self.assertIn('id="options-risk-summary"', HTML)
        self.assertIn('id="options-chain"', HTML)
        self.assertIn("renderOptionsRiskSummary", HTML)
        self.assertIn("renderOptionsChain", HTML)

    def test_page_position_summary_includes_asset_price(self) -> None:
        self.assertIn('id="portfolio-total-assets-detail"', HTML)
        self.assertIn("function formatPositionPrice", HTML)
        self.assertIn("formatPositionValue", HTML)
        self.assertIn("price $", HTML)

    def test_page_includes_readiness_panel(self) -> None:
        self.assertIn("Readiness", HTML)
        self.assertIn('id="readiness-status"', HTML)
        self.assertIn('id="readiness-actions"', HTML)
        self.assertIn('id="readiness-accounts"', HTML)
        self.assertIn('id="readiness-strategies"', HTML)

    def test_page_uses_collapsible_sections(self) -> None:
        self.assertIn('class="compact-section', HTML)
        self.assertIn("function setupCompactSections()", HTML)
        self.assertIn("section-open", HTML)
        self.assertIn("aria-expanded", HTML)
        self.assertIn("renderOpenSection", HTML)
        self.assertIn("PAGE_REFRESH_INTERVAL_MS", HTML)
        self.assertIn("REFRESH_FAILURE_BACKOFF_MS", HTML)
        self.assertIn("scheduleNextRefresh", HTML)
        self.assertNotIn("setInterval(() =>", HTML)
        self.assertIn("document.hidden", HTML)
        self.assertIn("visibilitychange", HTML)
        self.assertIn(".strategy-overview[data-page].active-page", STYLES_CSS)
        self.assertIn('renderOpenSection("risk-form"', HTML)
        self.assertIn('renderOpenSection("strategy-instances"', HTML)
        self.assertIn('renderOpenSection("console-strategies"', HTML)
        self.assertIn("function renderRiskEvents", HTML)
        self.assertIn("function renderAuditTrail", HTML)

    def test_page_includes_persisted_onchain_change_log(self) -> None:
        self.assertIn("Holder Change Log", HTML)
        self.assertIn("Since Online", HTML)
        self.assertIn('id="holder-changes"', HTML)
        self.assertIn('id="onchain-history-meta"', HTML)

    def test_page_includes_orders_and_fills(self) -> None:
        self.assertIn("Orders & Fills", HTML)
        self.assertIn("/api/orders/cancel", HTML)
        self.assertIn('id="open-orders"', HTML)
        self.assertIn('id="recent-fills"', HTML)
        self.assertIn('id="order-reconciliation"', HTML)
        self.assertIn("Reconciliation OK", HTML)
        self.assertIn("Order Qty", INDEX_HTML)
        self.assertIn("Open Value", INDEX_HTML)
        self.assertIn("function finiteOrderNumber(value)", APP_JS)
        self.assertIn("function orderOpenNotional(order)", APP_JS)
        self.assertIn("formatOrderNumber(orderOpenNotional(order), quote)", APP_JS)

    def test_page_includes_live_trading_console(self) -> None:
        self.assertIn("Live Trading Console", HTML)
        self.assertIn("/api/orders/cancel-bulk", HTML)
        self.assertIn("/api/strategies/control", HTML)
        self.assertIn('id="console-open-orders"', HTML)
        self.assertIn('id="console-recent-fills"', HTML)

    def test_page_includes_market_maker_controls(self) -> None:
        self.assertIn("Market Maker", HTML)
        self.assertIn("/api/market-maker", HTML)
        self.assertIn('id="mm-form"', HTML)
        self.assertIn('id="mm-live-enabled"', HTML)
        self.assertIn('id="mm-accounts"', HTML)
        self.assertIn('id="mm-safety-status"', HTML)
        self.assertIn('id="mm-safety-budget"', HTML)
        self.assertIn('id="mm-inventory-enabled"', HTML)
        self.assertIn('id="mm-inventory-target"', HTML)
        self.assertIn('id="mm-adaptive-reprice"', HTML)
        self.assertIn('id="mm-adaptive-spread"', HTML)
        self.assertIn('id="mm-quality-inventory"', HTML)
        self.assertIn('id="mm-quality-fills"', HTML)
        self.assertIn('id="mm-quality-spread"', HTML)

    def test_page_includes_cross_exchange_rebalance_controls(self) -> None:
        self.assertIn("Cross-Exchange Rebalance", HTML)
        self.assertIn("/api/cross-exchange-rebalance", HTML)
        self.assertIn('id="rebalance-form"', HTML)
        self.assertIn('id="rebalance-buy-accounts"', HTML)
        self.assertIn('id="rebalance-sell-accounts"', HTML)
        self.assertIn('id="rebalance-coordinate-mm"', HTML)
        self.assertIn('id="rebalance-coordination-timeout"', HTML)
        self.assertIn('id="rebalance-live-confirm"', HTML)
        self.assertIn(
            'aria-pressed="false">Review &amp; Start Live</button>',
            HTML,
        )
        self.assertIn('id="rebalance-stop"', HTML)
        self.assertNotIn('placeholder="ENABLE LIVE REBALANCE"', HTML)
        self.assertIn('id="rebalance-readiness"', HTML)
        self.assertIn('id="rebalance-feedback"', HTML)
        self.assertIn('id="rebalance-open-risk"', HTML)
        self.assertIn('id="rebalance-reset"', HTML)
        self.assertIn('id="rebalance-acknowledge-exposure"', HTML)
        self.assertIn('id="rebalance-stop-release"', HTML)
        self.assertIn("RESET REBALANCE", APP_JS)
        self.assertIn("ACKNOWLEDGE RESIDUAL EXPOSURE", APP_JS)
        self.assertIn("STOP REBALANCE AND RELEASE MM", APP_JS)
        self.assertIn("acknowledgeRebalanceExposure", APP_JS)
        self.assertIn("stopRebalanceAndReleaseMm", APP_JS)
        self.assertIn("liveRebalanceValidationError", APP_JS)
        self.assertIn("confirmLiveRebalance", APP_JS)
        self.assertIn("lastState?.operations?.risk", APP_JS)
        self.assertNotIn("window.prompt", APP_JS)
        self.assertIn("Previous task is complete.", APP_JS)

    def test_cross_exchange_rebalance_config_requires_matching_assets(self) -> None:
        base = CrossExchangeRebalanceConfig()
        accounts = {"bithumb-spot", "coinbase-spot"}
        symbols = {
            "bithumb-spot": ["ACS/KRW"],
            "coinbase-spot": ["ACS/USDC", "BTC/USDC"],
        }

        configured = cross_exchange_rebalance_config_from_payload(
            {
                "enabled": True,
                "buy_exchange": "bithumb-spot",
                "buy_symbol": "ACS/KRW",
                "sell_exchange": "coinbase-spot",
                "sell_symbol": "ACS/USDC",
                "total_quote_common": 100,
                "quote_per_cycle_common": 10,
                "interval_seconds": 30,
            },
            base_config=base,
            allowed_exchanges=accounts,
            symbols_by_exchange=symbols,
        )

        self.assertTrue(configured.enabled)
        self.assertEqual(configured.buy_symbol, "ACS/KRW")
        self.assertEqual(configured.sell_symbol, "ACS/USDC")
        disabled = cross_exchange_rebalance_config_from_payload(
            {
                "enabled": False,
                "live_enabled": False,
                "buy_exchange": "",
                "buy_symbol": "",
                "sell_exchange": "",
                "sell_symbol": "",
                "total_quote_common": 0,
                "quote_per_cycle_common": 0,
                "interval_seconds": 30,
            },
            base_config=base,
            allowed_exchanges=accounts,
            symbols_by_exchange=symbols,
        )
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.buy_exchange, "")
        self.assertEqual(disabled.total_quote_common, 0.0)
        with self.assertRaisesRegex(ValueError, "same base asset"):
            cross_exchange_rebalance_config_from_payload(
                {
                    "enabled": True,
                    "buy_exchange": "bithumb-spot",
                    "buy_symbol": "ACS/KRW",
                    "sell_exchange": "coinbase-spot",
                    "sell_symbol": "BTC/USDC",
                    "total_quote_common": 100,
                    "quote_per_cycle_common": 10,
                },
                base_config=base,
                allowed_exchanges=accounts,
                symbols_by_exchange=symbols,
            )

    def test_rebalance_routes_are_included_in_web_balance_symbols(self) -> None:
        cfg = make_config(
            cross_exchange_rebalance=CrossExchangeRebalanceConfig(
                buy_exchange="bithumb-spot",
                buy_symbol="ACS/KRW",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
            )
        )

        symbols = _exchange_balance_symbols(cfg)

        self.assertEqual(symbols["bithumb-spot"], ["ACS/KRW"])
        self.assertEqual(symbols["coinbase-spot"], ["ACS/USDC"])

    def test_page_includes_spot_grid_and_dca_controls(self) -> None:
        self.assertIn("Spot Grid", HTML)
        self.assertIn("/api/spot-grid", HTML)
        self.assertIn('id="grid-form"', HTML)
        self.assertIn('id="grid-lower"', HTML)
        self.assertIn('id="grid-upper"', HTML)
        self.assertIn('id="grid-spacing"', HTML)
        self.assertIn('id="grid-auto-rebuild"', HTML)
        self.assertIn('id="grid-orders"', HTML)
        self.assertIn("data-account-selector", APP_JS)
        self.assertIn("data-project-selector", APP_JS)
        self.assertIn("exchangeSelector", APP_JS)
        self.assertIn("data-symbol-selector", APP_JS)
        self.assertIn("Account / Currency / Exchange / Pair", HTML)
        self.assertIn('id="strategy-settings-section"', HTML)
        self.assertIn('id="strategy-settings-cards"', HTML)
        self.assertIn('id="status-reasons-section"', HTML)
        self.assertIn("renderStrategySettingCards", APP_JS)
        self.assertIn("renderStatusReasons", APP_JS)
        self.assertIn("applyMobileTableLabels", APP_JS)
        self.assertIn("dirty-badge", STYLES_CSS)
        self.assertIn("mobile-card-table", HTML)
        self.assertIn("Confirm cancel open orders?", APP_JS)
        self.assertIn("DCA Bot", HTML)
        self.assertIn("/api/dca", HTML)
        self.assertIn('id="dca-form"', HTML)
        self.assertIn('id="dca-trigger"', HTML)
        self.assertIn('id="dca-multiplier"', HTML)
        self.assertIn('id="dca-average-entry"', HTML)
        self.assertIn('id="dca-orders"', HTML)

    def test_page_includes_execution_algo_and_backtest_controls(self) -> None:
        self.assertIn("TWAP / VWAP / POV", HTML)
        self.assertIn("/api/execution-algo", HTML)
        self.assertIn('id="exec-form"', HTML)
        self.assertIn('id="exec-algo"', HTML)
        self.assertIn('id="exec-total-quote"', HTML)
        self.assertIn('id="exec-schedule"', HTML)
        self.assertIn("Historical Backtest", HTML)
        self.assertIn("/api/user-backtests", APP_JS)
        self.assertIn('id="backtest-form"', HTML)
        self.assertIn('id="backtest-strategy"', HTML)
        self.assertIn('id="backtest-return"', HTML)
        self.assertIn('id="backtest-points"', HTML)

    def test_page_includes_risk_controls(self) -> None:
        self.assertIn("Risk Controls", HTML)
        self.assertIn("/api/risk", HTML)
        self.assertIn('id="risk-allow-live"', HTML)
        self.assertIn('id="risk-accounts"', HTML)
        self.assertIn('id="risk-strategies"', HTML)
        self.assertIn('id="risk-max-order"', HTML)
        self.assertIn('id="risk-max-cycle"', HTML)
        self.assertIn('id="risk-max-orders-cycle"', HTML)
        self.assertIn('id="risk-max-exposure"', HTML)
        self.assertIn('id="risk-min-book-depth"', HTML)
        self.assertIn('id="risk-max-slippage"', HTML)
        self.assertIn('id="risk-max-derivative-leverage"', HTML)
        self.assertIn('id="risk-min-liquidation-buffer"', HTML)
        self.assertIn('id="risk-max-margin-usage"', HTML)

    def test_page_includes_audit_trail(self) -> None:
        self.assertIn("Audit Trail", HTML)
        self.assertIn('id="audit-events"', HTML)
        self.assertIn('id="audit-meta"', HTML)

    def test_page_includes_strategy_timeline(self) -> None:
        self.assertIn('id="strategy-timeline"', HTML)
        self.assertIn("strategy_timeline", HTML)
        self.assertIn("No strategy timeline events yet.", HTML)

    def test_spot_markets_payload_sanitizes_new_market(self) -> None:
        markets = _spot_markets_from_payload(
            {
                "spot_markets": [
                    {
                        "asset": "btc",
                        "exchange": "bybit-spot",
                        "symbol": "btc/usdt",
                    }
                ]
            },
            allowed_exchanges={"bybit-spot"},
        )

        self.assertEqual(markets[0].asset, "BTC")
        self.assertEqual(markets[0].exchange, "bybit-spot")
        self.assertEqual(markets[0].symbol, "BTC/USDT")
        self.assertEqual(markets[0].quote_currency, "USDT")

    def test_spot_markets_payload_rejects_unknown_account(self) -> None:
        with self.assertRaises(ValueError):
            _spot_markets_from_payload(
                {
                    "spot_markets": [
                        {
                            "asset": "BTC",
                            "exchange": "missing",
                            "symbol": "BTC/USDT",
                        }
                    ]
                },
                allowed_exchanges={"bybit-spot"},
            )

    def test_spot_markets_payload_rejects_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            _spot_markets_from_payload(
                {
                    "spot_markets": [
                        {
                            "asset": "BTC",
                            "exchange": "bybit-spot",
                            "symbol": "BTC/USDT",
                        },
                        {
                            "asset": "BTC",
                            "exchange": "bybit-spot",
                            "symbol": "BTC/USDT",
                        },
                    ]
                },
                allowed_exchanges={"bybit-spot"},
            )

    def test_cash_and_carry_payload_sanitizes_pair(self) -> None:
        pairs = _cash_and_carry_pairs_from_payload(
            {
                "cash_and_carry_pairs": [
                    {
                        "spot_symbol": "btc/usdt",
                        "derivative_symbol": "btc/usdt:usdt",
                    }
                ]
            }
        )

        self.assertEqual(pairs[0].spot_symbol, "BTC/USDT")
        self.assertEqual(pairs[0].derivative_symbol, "BTC/USDT:USDT")

    def test_cash_and_carry_payload_rejects_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            _cash_and_carry_pairs_from_payload(
                {
                    "cash_and_carry_pairs": [
                        {
                            "spot_symbol": "BTC/USDT",
                            "derivative_symbol": "BTC/USDT:USDT",
                        },
                        {
                            "spot_symbol": "btc/usdt",
                            "derivative_symbol": "btc/usdt:usdt",
                        },
                    ]
                }
            )

    def test_cash_and_carry_payload_rejects_base_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "base must match"):
            _cash_and_carry_pairs_from_payload(
                {
                    "cash_and_carry_pairs": [
                        {
                            "spot_symbol": "BTC/USDT",
                            "derivative_symbol": "ETH/USDT:USDT",
                        }
                    ]
                }
            )

    def test_trading_console_payload_reports_live_and_paused_strategies(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                live_enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            slow_execution=SlowExecutionConfig(
                enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
            ),
            spot_grid=SpotGridConfig(
                enabled=True,
                live_enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            dca=DcaConfig(
                enabled=True,
                live_enabled=False,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
            ),
            execution_algo=ExecutionAlgoConfig(
                enabled=True,
                live_enabled=True,
                exchange="coinbase-spot",
                symbol="ACS/USDC",
            ),
            backtest=BacktestConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            risk=RiskConfig(
                allow_live_trading=True,
                allow_market_maker=True,
                allow_slow_execution=True,
                strategy_enabled={
                    "spot_grid": True,
                    "dca": True,
                    "execution_algo": True,
                    "backtest": True,
                },
            ),
        )

        payload = build_trading_console_payload(
            cfg,
            strategy_paused={"slow_execution": True},
            order_activity={
                "open_orders": [
                    {"exchange": "bybit-spot"},
                    {"exchange": "coinbase-spot"},
                    {"exchange": "coinbase-spot"},
                ],
                "recent_trade_count": 5,
            },
        )

        strategies = {row["id"]: row for row in payload["strategies"]}
        accounts = {row["key"]: row for row in payload["accounts"]}
        self.assertTrue(strategies["market_maker"]["live"])
        self.assertTrue(strategies["slow_execution"]["paused"])
        self.assertFalse(strategies["slow_execution"]["live"])
        self.assertTrue(strategies["spot_grid"]["live"])
        self.assertFalse(strategies["dca"]["live"])
        self.assertFalse(strategies["dca"]["live_ready"])
        self.assertTrue(strategies["execution_algo"]["live"])
        self.assertFalse(strategies["backtest"]["live"])
        self.assertEqual(strategies["backtest"]["mode"], "research")
        self.assertEqual(accounts["coinbase-spot"]["open_order_count"], 2)
        self.assertEqual(payload["recent_trade_count"], 5)

    def test_market_maker_requires_explicit_live_enabled_for_live_console(self) -> None:
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

        payload = build_trading_console_payload(cfg)

        strategies = {row["id"]: row for row in payload["strategies"]}
        self.assertFalse(strategies["market_maker"]["live"])
        self.assertFalse(strategies["market_maker"]["live_ready"])

    def test_trading_console_payload_uses_auto_buy_sell_tasks(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(enabled=False),
            spot_exchanges=[ExchangeConfig(id="coinbase", label="coinbase-spot")],
            risk=RiskConfig(allow_live_trading=True),
        )

        payload = build_trading_console_payload(
            cfg,
            auto_buy_sell_tasks={
                "tasks": [
                    {
                        "status": "running",
                        "config": {
                            "exchange": "coinbase-spot",
                            "symbol": "ACS/USDC",
                        },
                    }
                ]
            },
        )

        strategies = {row["id"]: row for row in payload["strategies"]}
        self.assertTrue(strategies["slow_execution"]["configured"])
        self.assertEqual(strategies["slow_execution"]["exchange"], "coinbase-spot")
        self.assertEqual(strategies["slow_execution"]["symbol"], "ACS/USDC")

    def test_readiness_payload_reports_account_blockers(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                live_enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
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
            risk=RiskConfig(
                allow_live_trading=True,
                allow_market_maker=True,
                account_enabled={"bybit-spot": False},
            ),
        )

        with patch.dict(
            os.environ,
            {"BYBIT_API_KEY": "key", "BYBIT_SECRET": "secret"},
            clear=True,
        ):
            payload = build_readiness_payload(
                cfg,
                account_balances={
                    "status": "ok",
                    "accounts": [
                        {
                            "exchange": "coinbase-spot",
                            "status": "warning",
                            "warnings": [
                                "one or more configured API env vars are not set"
                            ],
                            "balance": {
                                "skipped_reason": "api env vars missing",
                            },
                        },
                        {"exchange": "bybit-spot", "status": "ok"},
                    ],
                },
                order_activity={
                    "status": "ok",
                    "accounts": [
                        {"exchange": "coinbase-spot", "status": "warning"},
                        {"exchange": "bybit-spot", "status": "ok"},
                    ],
                    "reconciliation": {"status": "ok", "issue_count": 0},
                },
                trading_console=build_trading_console_payload(cfg),
            )

        accounts = {row["key"]: row for row in payload["accounts"]}
        strategies = {row["id"]: row for row in payload["strategies"]}
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(accounts["coinbase-spot"]["status"], "blocked")
        self.assertIn(
            "one or more API env vars are not set",
            accounts["coinbase-spot"]["reasons"],
        )
        self.assertEqual(
            [
                reason
                for reason in accounts["coinbase-spot"]["reasons"]
                if "api env" in reason.lower()
            ],
            ["one or more API env vars are not set"],
        )
        self.assertEqual(accounts["bybit-spot"]["status"], "blocked")
        self.assertIn("account disabled by risk", accounts["bybit-spot"]["reasons"])
        self.assertEqual(strategies["market_maker"]["status"], "blocked")
        self.assertIn(
            "account disabled by risk",
            strategies["market_maker"]["reasons"],
        )
        actions = {row["action"] for row in payload["next_actions"]}
        self.assertIn("Configure API environment variables", actions)
        self.assertIn("Enable account in Risk Controls", actions)
        self.assertEqual(
            payload["summary"]["action_count"], len(payload["next_actions"])
        )

    def test_readiness_payload_reports_checking_before_health_cache_is_ready(
        self,
    ) -> None:
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
                    api_key_env="COINBASE_API_KEY",
                    secret_env="COINBASE_SECRET",
                )
            ],
            risk=RiskConfig(allow_live_trading=True),
        )

        with patch.dict(
            os.environ,
            {"COINBASE_API_KEY": "key", "COINBASE_SECRET": "secret"},
            clear=True,
        ):
            payload = build_readiness_payload(
                cfg,
                account_balances={"status": "starting", "accounts": []},
                order_activity={
                    "status": "starting",
                    "accounts": [],
                    "reconciliation": {"status": "starting", "issue_count": 0},
                },
                trading_console=build_trading_console_payload(cfg),
            )

        self.assertEqual(payload["status"], "checking")
        self.assertEqual(payload["accounts"][0]["status"], "checking")
        self.assertEqual(payload["summary"]["checking_accounts"], 1)

    def test_readiness_payload_ignores_reconciliation_notices(self) -> None:
        cfg = make_config(risk=RiskConfig(allow_live_trading=True))

        payload = build_readiness_payload(
            cfg,
            account_balances={"status": "ok", "accounts": []},
            order_activity={
                "status": "ok",
                "accounts": [],
                "reconciliation": {
                    "status": "ok",
                    "issue_count": 0,
                    "notice_count": 20,
                },
            },
            trading_console=build_trading_console_payload(cfg),
        )

        actions = {row["action"] for row in payload["next_actions"]}
        self.assertNotIn("Review order/fill attribution", actions)
        self.assertEqual(
            payload["order_checks"]["reconciliation_notice_count"],
            20,
        )

    def test_readiness_payload_treats_risk_disabled_strategy_as_disabled(self) -> None:
        cfg = make_config(risk=RiskConfig(allow_live_trading=True))
        payload = build_readiness_payload(
            cfg,
            account_balances={"status": "ok", "accounts": []},
            order_activity={
                "status": "ok",
                "accounts": [],
                "reconciliation": {"status": "ok", "issue_count": 0},
            },
            trading_console={
                "strategies": [
                    {
                        "id": "funding_bot",
                        "label": "Funding Bot",
                        "configured": True,
                        "mode": "paper",
                        "live": False,
                        "paused": False,
                        "strategy_allowed": False,
                    }
                ]
            },
        )

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["strategies"][0]["status"], "disabled")
        self.assertEqual(payload["summary"]["blocked_strategies"], 0)
        self.assertEqual(payload["next_actions"], [])

    def test_readiness_payload_reports_execution_protection_blockers(self) -> None:
        cfg = make_config(risk=RiskConfig(allow_live_trading=True))

        payload = build_readiness_payload(
            cfg,
            account_balances={"status": "ok", "accounts": []},
            order_activity={
                "status": "ok",
                "accounts": [],
                "reconciliation": {"status": "ok", "issue_count": 0},
            },
            trading_console=build_trading_console_payload(cfg),
            execution_protection={
                "status": "blocked",
                "blocked_count": 1,
                "warning_count": 1,
                "manual_review_count": 1,
                "top_reasons": ["slippage exceeds configured limit"],
            },
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["summary"]["execution_protection_blocked_count"], 1)
        self.assertEqual(payload["summary"]["execution_protection_warning_count"], 1)
        self.assertEqual(
            payload["summary"]["execution_protection_manual_review_count"],
            1,
        )
        self.assertEqual(payload["summary"]["blocked_count"], 1)
        self.assertEqual(payload["summary"]["warning_count"], 2)
        actions = {row["scope"]: row for row in payload["next_actions"]}
        self.assertEqual(
            actions["Execution Protection"]["action"],
            "Review multi-leg paper protection",
        )
        self.assertEqual(actions["Execution Protection"]["priority"], "high")
        self.assertIn(
            "slippage",
            actions["Execution Protection"]["detail"],
        )

    def test_readiness_payload_reports_derivatives_risk_blockers(self) -> None:
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
            risk=RiskConfig(allow_live_trading=True),
        )

        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "key", "BINANCE_SECRET": "secret"},
            clear=True,
        ):
            payload = build_readiness_payload(
                cfg,
                account_balances={
                    "status": "ok",
                    "accounts": [{"exchange": "binance-swap", "status": "ok"}],
                },
                order_activity={
                    "status": "ok",
                    "accounts": [{"exchange": "binance-swap", "status": "ok"}],
                    "reconciliation": {"status": "ok", "issue_count": 0},
                },
                derivatives={
                    "status": "blocked",
                    "position_count": 1,
                    "accounts": [
                        {
                            "exchange": "binance-swap",
                            "label": "Binance Futures",
                            "status": "blocked",
                            "risk_reasons": ["margin usage 80% > 70%"],
                            "summary": {
                                "risk_reasons": ["margin usage 80% > 70%"],
                            },
                        }
                    ],
                },
                trading_console=build_trading_console_payload(cfg),
            )

        accounts = {row["key"]: row for row in payload["accounts"]}
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(accounts["binance-swap"]["status"], "blocked")
        self.assertEqual(
            accounts["binance-swap"]["derivatives_status"],
            "blocked",
        )
        self.assertEqual(payload["summary"]["derivative_blocked_account_count"], 1)
        self.assertEqual(payload["summary"]["derivative_position_count"], 1)
        self.assertEqual(payload["summary"]["blocked_count"], 1)
        actions = {row["scope"]: row for row in payload["next_actions"]}
        self.assertEqual(
            actions["Derivatives Risk"]["action"],
            "Review margin and liquidation risk",
        )
        self.assertEqual(actions["Derivatives Risk"]["priority"], "high")
        self.assertIn("margin usage", actions["Derivatives Risk"]["detail"])

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
        self.assertEqual(payload["safety"]["order_count"], 20)
        self.assertAlmostEqual(payload["safety"]["total_quote_notional"], 20.0)
        self.assertEqual(payload["safety"]["limits"]["max_cycle_quote"], 25.0)
        self.assertIn("risk.allow_live_trading is false", payload["safety"]["reasons"])

    def test_market_maker_safety_uses_instance_gap_override(self) -> None:
        cfg = make_config(
            market_maker=MarketMakerConfig(
                enabled=True,
                exchange="upbit-spot",
                symbol="ACS/USDT",
                levels=1,
                price_band_pct=1.0,
                quote_per_level=1.0,
                max_order_quote=3.0,
                max_cycle_quote=12.0,
                max_open_orders=44,
                max_cancels_per_cycle=22,
                max_slippage_bps=75.0,
                max_order_book_gap_bps=10_000.0,
                max_order_book_age_seconds=4.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                max_order_quote=1.0,
                max_cycle_quote=2.0,
                max_open_orders=5,
                max_cancels_per_cycle=5,
                max_slippage_bps=10.0,
                max_order_book_gap_bps=5_000.0,
                max_order_book_age_seconds=10.0,
            ),
            quote_rates={"USD": 1.0, "USDT": 1.0},
        )
        books = {
            ("upbit-spot", "ACS/USDT"): OrderBookSnapshot(
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
        }

        payload = build_market_maker_payload(cfg, books)

        self.assertTrue(payload["safety"]["approved"])
        self.assertEqual(
            payload["safety"]["limits"]["max_order_book_gap_bps"],
            10_000.0,
        )
        self.assertEqual(payload["safety"]["limits"]["max_order_quote"], 3.0)
        self.assertEqual(payload["safety"]["limits"]["max_cycle_quote"], 12.0)
        self.assertEqual(payload["safety"]["limits"]["max_open_orders"], 44)
        self.assertEqual(payload["safety"]["limits"]["max_cancels_per_cycle"], 22)
        self.assertEqual(payload["safety"]["limits"]["max_slippage_bps"], 75.0)
        self.assertEqual(
            payload["safety"]["limits"]["max_order_book_age_seconds"],
            4.0,
        )
        self.assertGreater(payload["safety"]["market"]["max_level_gap_bps"], 5_000.0)
        self.assertEqual(payload["safety"]["reasons"], [])

    def test_build_market_maker_quality_payload_summarizes_recent_fills(self) -> None:
        payload = build_market_maker_quality_payload(
            {
                "recent_trades": [
                    {
                        "source": "market_maker",
                        "side": "buy",
                        "amount": 100.0,
                        "notional_common": 9.0,
                        "fee_common": 0.01,
                        "realized_pnl_common": -0.01,
                    },
                    {
                        "source": "market_maker",
                        "side": "sell",
                        "amount": 100.0,
                        "notional_common": 11.0,
                        "fee_common": 0.01,
                        "realized_pnl_common": 1.99,
                    },
                    {
                        "source": "slow_execution",
                        "side": "sell",
                        "amount": 100.0,
                        "notional_common": 12.0,
                    },
                ]
            },
            {
                "plan": {
                    "symbol": "ACS/USDC",
                    "mid_price": 0.1,
                    "inventory_base": 1_200.0,
                    "inventory_target_base": 1_000.0,
                    "inventory_deviation_base": 200.0,
                    "inventory_buy_multiplier": 0.5,
                    "inventory_sell_multiplier": 1.5,
                    "inventory_control_active": True,
                }
            },
        )

        self.assertEqual(payload["trade_count"], 2)
        self.assertEqual(payload["buy"]["trade_count"], 1)
        self.assertEqual(payload["sell"]["trade_count"], 1)
        self.assertAlmostEqual(payload["buy"]["average_price"], 0.09)
        self.assertAlmostEqual(payload["sell"]["average_price"], 0.11)
        self.assertAlmostEqual(payload["realized_spread_bps"], 2000.0)
        self.assertAlmostEqual(payload["total_fees"], 0.02)
        self.assertAlmostEqual(payload["realized_pnl"], 1.98)
        self.assertAlmostEqual(payload["inventory"]["base"], 1_200.0)
        self.assertTrue(payload["inventory"]["active"])

    def test_build_market_maker_quality_payload_falls_back_to_daily_pnl(self) -> None:
        payload = build_market_maker_quality_payload(
            {
                "recent_trades": [],
                "daily_pnl": {
                    "enabled": True,
                    "day": "2026-06-19",
                    "currency": "USD",
                    "updated_at": 1234.0,
                    "sources": {
                        "market_maker": {
                            "trade_count": 5,
                            "notional_common": 1200.0,
                            "fees_common": 1.5,
                            "realized_pnl": 8.25,
                        }
                    },
                },
            },
            {"plan": {"symbol": "ACS/USDC", "mid_price": 0.1}},
        )

        self.assertEqual(payload["window"], "daily_pnl")
        self.assertEqual(payload["recent_trade_count"], 0)
        self.assertEqual(payload["trade_count"], 5)
        self.assertAlmostEqual(payload["total_notional"], 1200.0)
        self.assertAlmostEqual(payload["total_fees"], 1.5)
        self.assertAlmostEqual(payload["realized_pnl"], 8.25)
        self.assertEqual(payload["daily"]["day"], "2026-06-19")
        self.assertEqual(payload["daily"]["currency"], "USD")

    def test_build_slow_execution_payload_returns_best_bid_sell_order(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                side="sell",
                total_base=10_000.0,
                slice_base=1_000.0,
                interval_seconds=30.0,
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

        payload = build_slow_execution_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["plan"]["side"], "sell")
        self.assertAlmostEqual(payload["plan"]["mid_price"], 0.00015)
        self.assertAlmostEqual(payload["plan"]["order"]["price"], 0.00014)
        self.assertAlmostEqual(payload["plan"]["order"]["amount"], 1_000.0)
        self.assertAlmostEqual(payload["plan"]["order"]["quote_notional"], 0.14)

    def test_slow_execution_payload_uses_range_config(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
                side="buy",
                total_base=10_000.0,
                slice_base_min=1_000.0,
                slice_base_max=2_000.0,
                randomize_slice=False,
                interval_seconds=30.0,
                order_ttl_seconds=5.0,
                stop_price=0.0002,
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

        payload = build_slow_execution_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["config"]["slice_base_min"], 1_000.0)
        self.assertEqual(payload["config"]["slice_base_max"], 2_000.0)
        self.assertEqual(payload["config"]["order_ttl_seconds"], 5.0)
        self.assertEqual(payload["plan"]["order"]["amount"], 1_000.0)

    def test_slow_execution_payload_includes_configured_accounts(self) -> None:
        cfg = make_config(
            slow_execution=SlowExecutionConfig(exchange="bybit-spot"),
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            spot_markets=[
                SpotMarketConfig(
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    asset="ACS",
                    quote_currency="USDT",
                ),
                SpotMarketConfig(
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    asset="ACS",
                    quote_currency="USDC",
                ),
            ],
        )

        payload = build_slow_execution_payload(cfg, {})

        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual(payload["accounts"][0]["key"], "bybit-spot")
        self.assertEqual(payload["accounts"][0]["symbol"], "ACS/USDT")
        self.assertEqual(payload["accounts"][0]["symbols"], ["ACS/USDT"])
        self.assertEqual(payload["accounts"][0]["projects"], ["ACS"])
        self.assertEqual(payload["accounts"][0]["markets"][0]["quote_currency"], "USDT")
        self.assertEqual(payload["accounts"][1]["key"], "coinbase-spot")
        self.assertEqual(payload["accounts"][1]["symbol"], "ACS/USDC")
        self.assertEqual(payload["accounts"][1]["symbols"], ["ACS/USDC"])
        self.assertEqual(payload["accounts"][1]["projects"], ["ACS"])
        self.assertEqual(payload["accounts"][1]["markets"][0]["quote_currency"], "USDC")

    def test_slow_execution_accounts_uses_key_fallback(self) -> None:
        accounts = slow_execution_accounts([ExchangeConfig(id="bybit")])

        self.assertEqual(accounts[0]["key"], "bybit:spot")
        self.assertEqual(accounts[0]["label"], "bybit:spot")
        self.assertEqual(accounts[0]["symbol"], "")
        self.assertEqual(accounts[0]["symbols"], [])
        self.assertEqual(accounts[0]["projects"], [])
        self.assertEqual(accounts[0]["markets"], [])

    def test_slow_execution_accounts_include_market_selector_metadata(self) -> None:
        accounts = slow_execution_accounts(
            [ExchangeConfig(id="coinbase", label="coinbase-spot")],
            {"coinbase-spot": ["ACS/USDC", "BTC/USDC"]},
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
        )

        self.assertEqual(accounts[0]["projects"], ["ACS", "BTC"])
        self.assertEqual(accounts[0]["markets"][0]["asset"], "ACS")
        self.assertEqual(accounts[0]["markets"][0]["exchange_id"], "coinbase")
        self.assertEqual(accounts[0]["markets"][0]["symbol"], "ACS/USDC")
        self.assertEqual(accounts[0]["markets"][1]["asset"], "BTC")

    def test_slow_execution_accounts_marks_unbound_owner_api_as_unrestricted(
        self,
    ) -> None:
        accounts = slow_execution_accounts(
            [
                ExchangeConfig(
                    id="binanceusdm",
                    label="workspace:connection-binance:swap",
                    display_label="Binance Main · SWAP",
                    market_type="swap",
                    credential_connection_id="connection-binance",
                    credential_owner_email="trader@example.com",
                )
            ]
        )

        self.assertEqual(accounts[0]["account_source"], "user_api")
        self.assertEqual(accounts[0]["market_scope"], "all_supported_markets")
        self.assertEqual(
            accounts[0]["workspace_connection_id"],
            "connection-binance",
        )
        self.assertEqual(accounts[0]["symbols"], [])

    def test_strategy_universe_lists_selectable_markets(self) -> None:
        cfg = make_config(
            spot_exchanges=[
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
                ExchangeConfig(id="binance", label="binance-spot"),
            ],
            derivative_exchanges=[
                ExchangeConfig(
                    id="binanceusdm",
                    label="binance-swap",
                    market_type="swap",
                )
            ],
            spot_markets=[
                SpotMarketConfig(
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    asset="ACS",
                    quote_currency="USDC",
                ),
                SpotMarketConfig(
                    exchange="binance-spot",
                    symbol="BTC/USDT",
                    asset="BTC",
                    quote_currency="USDT",
                ),
            ],
            cash_and_carry_pairs=[
                CashAndCarryPair(
                    spot_symbol="BTC/USDT",
                    derivative_symbol="BTC/USDT:USDT",
                )
            ],
            spot_grid=SpotGridConfig(
                exchange="binance-spot",
                symbol="ETH/USDT",
            ),
        )

        universe = strategy_universe_to_dict(cfg)
        grid_accounts = {row["key"]: row for row in universe["grid"]["accounts"]}
        all_accounts = {row["key"]: row for row in universe["all"]["accounts"]}

        self.assertIn("ACS", universe["assets"])
        self.assertIn("BTC", universe["assets"])
        self.assertIn("ETH/USDT", grid_accounts["binance-spot"]["symbols"])
        self.assertIn("BTC/USDT:USDT", all_accounts["binance-swap"]["symbols"])

    def test_build_spot_grid_payload_returns_plan_and_safety(self) -> None:
        cfg = make_config(
            spot_grid=SpotGridConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USD",
                lower_price=90.0,
                upper_price=110.0,
                grid_count=4,
                quote_per_grid=5.0,
                max_open_orders=4,
                min_grid_step_bps=1.0,
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                max_order_quote=10.0,
                max_cycle_quote=25.0,
                max_open_orders=10,
            ),
        )
        books = {
            ("bybit-spot", "ACS/USD"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USD",
                bids=[BookLevel(price=99.0, amount=100_000)],
                asks=[BookLevel(price=101.0, amount=100_000)],
            )
        }

        payload = build_spot_grid_payload(cfg, books)

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["config"]["grid_count"], 4)
        self.assertEqual(len(payload["plan"]["orders"]), 4)
        self.assertTrue(payload["safety"]["approved"])
        self.assertEqual(payload["safety"]["order_count"], 4)

    def test_build_dca_payload_returns_ready_plan_and_safety(self) -> None:
        cfg = make_config(
            dca=DcaConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USD",
                side="buy",
                trigger_price=102.0,
                quote_per_order=5.0,
                size_multiplier=2.0,
                max_orders=3,
                interval_seconds=60.0,
                price_mode="maker",
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                max_order_quote=10.0,
                max_cycle_quote=25.0,
                max_open_orders=10,
            ),
        )
        books = {
            ("bybit-spot", "ACS/USD"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USD",
                bids=[BookLevel(price=99.0, amount=100_000)],
                asks=[BookLevel(price=101.0, amount=100_000)],
            )
        }

        payload = build_dca_payload(cfg, books)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["plan"]["next_order"]["side"], "buy")
        self.assertEqual(
            [row["quote_notional"] for row in payload["plan"]["order_schedule"]],
            [5.0, 10.0, 20.0],
        )
        self.assertTrue(payload["safety"]["approved"])

    def test_build_execution_algo_payload_returns_plan_and_safety(self) -> None:
        cfg = make_config(
            execution_algo=ExecutionAlgoConfig(
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USD",
                side="buy",
                algo="twap",
                total_quote=12.0,
                slice_count=3,
                duration_seconds=900.0,
                interval_seconds=300.0,
                price_mode="taker",
            ),
            risk=RiskConfig(
                allow_live_trading=True,
                require_post_only=False,
                max_order_quote=10.0,
                max_cycle_quote=25.0,
                max_open_orders=10,
            ),
        )
        books = {
            ("bybit-spot", "ACS/USD"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USD",
                bids=[BookLevel(price=99.0, amount=100_000)],
                asks=[BookLevel(price=101.0, amount=100_000)],
            )
        }

        payload = build_execution_algo_payload(cfg, books)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["plan"]["algo"], "twap")
        self.assertEqual(len(payload["plan"]["schedule"]), 3)
        self.assertEqual(payload["plan"]["next_slice"]["quote_notional"], 4.0)
        self.assertTrue(payload["safety"]["approved"])

    def test_build_backtest_payload_returns_result(self) -> None:
        cfg = make_config(
            spot_grid=SpotGridConfig(
                enabled=True,
                symbol="ACS/USD",
                lower_price=90.0,
                upper_price=110.0,
                grid_count=4,
                quote_per_grid=5.0,
            ),
            backtest=BacktestConfig(
                enabled=True,
                strategy="spot_grid",
                exchange="bybit-spot",
                symbol="ACS/USD",
                initial_cash=100.0,
                price_start=90.0,
                price_end=110.0,
                step_count=20,
            ),
        )

        payload = build_backtest_payload(cfg, {})

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "research")
        self.assertEqual(payload["result"]["strategy"], "spot_grid")
        self.assertIn("max_drawdown_pct", payload["result"])

    def test_slow_execution_update_payload_is_sanitized(self) -> None:
        overrides = _slow_execution_overrides_from_payload(
            {
                "enabled": True,
                "exchange": "bybit-spot",
                "side": "buy",
                "total_base": "1000",
                "total_quote": "5",
                "slice_base_min": "10",
                "slice_base_max": "20",
                "randomize_slice": True,
                "interval_seconds": "5",
                "order_ttl_seconds": "2",
                "start_price": "0.02",
                "stop_price": "0.01",
                "price_mode": "maker",
                "price_offset_bps": "1",
                "unlimited_total": True,
                "slice_mode": "top_level",
                "instrument_type": "perpetual",
                "position_effect": "open",
                "position_side": "short",
                "position_mode": "one_way",
                "margin_mode": "cross",
                "leverage": "2",
                "max_position_quote": "500",
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["side"], "buy")
        self.assertEqual(overrides["total_quote"], 5.0)
        self.assertEqual(overrides["start_price"], 0.02)
        self.assertEqual(overrides["stop_price"], 0.01)
        self.assertEqual(overrides["price_mode"], "maker")
        self.assertEqual(overrides["price_offset_bps"], 1.0)
        self.assertTrue(overrides["unlimited_total"])
        self.assertEqual(overrides["slice_mode"], "top_level")
        self.assertEqual(overrides["slice_base"], 0.0)
        self.assertEqual(overrides["slice_quote"], 0.0)
        self.assertEqual(overrides["slice_base_min"], 10.0)
        self.assertEqual(overrides["slice_base_max"], 20.0)
        self.assertTrue(overrides["randomize_slice"])
        self.assertEqual(overrides["instrument_type"], "perpetual")
        self.assertEqual(overrides["position_effect"], "open")
        self.assertEqual(overrides["position_side"], "short")
        self.assertEqual(overrides["position_mode"], "one_way")
        self.assertEqual(overrides["margin_mode"], "cross")
        self.assertEqual(overrides["leverage"], 2.0)
        self.assertEqual(overrides["max_position_quote"], 500.0)

    def test_slow_execution_update_payload_rejects_unknown_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown exchange account"):
            _slow_execution_overrides_from_payload(
                {"exchange": "coinbase-spot"},
                allowed_exchanges={"bybit-spot"},
            )

    def test_slow_execution_update_payload_maps_account_symbol(self) -> None:
        overrides = _slow_execution_overrides_from_payload(
            {"exchange": "coinbase-spot"},
            allowed_exchanges={"coinbase-spot"},
            symbols_by_exchange={"coinbase-spot": ["ACS/USDC"]},
        )

        self.assertEqual(overrides["exchange"], "coinbase-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDC")

    def test_slow_execution_update_payload_rejects_wrong_account_symbol(self) -> None:
        with self.assertRaisesRegex(ValueError, "symbol is not configured"):
            _slow_execution_overrides_from_payload(
                {"exchange": "coinbase-spot", "symbol": "ACS/USDT"},
                allowed_exchanges={"coinbase-spot"},
                symbols_by_exchange={"coinbase-spot": ["ACS/USDC"]},
            )

    def test_slow_execution_update_payload_allows_custom_perpetual_symbol(
        self,
    ) -> None:
        overrides = _slow_execution_overrides_from_payload(
            {
                "exchange": "bybit-perp",
                "symbol": "btc/usdc:usdc",
                "instrument_type": "perpetual",
            },
            allowed_exchanges={"bybit-perp"},
            symbols_by_exchange={"bybit-perp": ["BTC/USDT:USDT"]},
        )

        self.assertEqual(overrides["symbol"], "BTC/USDC:USDC")
        self.assertEqual(overrides["instrument_type"], "perpetual")

    def test_spot_grid_update_payload_is_sanitized(self) -> None:
        overrides = _spot_grid_overrides_from_payload(
            {
                "enabled": True,
                "live_enabled": False,
                "exchange": "bybit-spot",
                "lower_price": "0.0001",
                "upper_price": "0.0002",
                "grid_count": "20",
                "spacing": "geometric",
                "quote_per_grid": "1.5",
                "take_profit_price": "0.00025",
                "stop_loss_price": "0.00008",
                "auto_rebuild": True,
                "max_position_base": "1000000",
                "max_open_orders": "30",
                "min_grid_step_bps": "5",
                "cancel_retry_attempts": "4",
                "post_only": True,
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertFalse(overrides["live_enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["spacing"], "geometric")
        self.assertEqual(overrides["grid_count"], 20)
        self.assertEqual(overrides["quote_per_grid"], 1.5)
        self.assertTrue(overrides["auto_rebuild"])
        self.assertEqual(overrides["cancel_retry_attempts"], 4)
        self.assertTrue(overrides["post_only"])

    def test_dca_update_payload_is_sanitized(self) -> None:
        overrides = _dca_overrides_from_payload(
            {
                "enabled": True,
                "live_enabled": False,
                "exchange": "bybit-spot",
                "side": "sell",
                "trigger_price": "0.0002",
                "interval_seconds": "30",
                "quote_per_order": "2",
                "size_multiplier": "1.5",
                "max_orders": "6",
                "average_entry_price": "0.00012",
                "take_profit_price": "0.00022",
                "max_position_base": "2000000",
                "max_loss_quote": "20",
                "price_mode": "maker",
                "price_offset_bps": "2",
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["side"], "sell")
        self.assertEqual(overrides["interval_seconds"], 30.0)
        self.assertEqual(overrides["quote_per_order"], 2.0)
        self.assertEqual(overrides["size_multiplier"], 1.5)
        self.assertEqual(overrides["max_orders"], 6)
        self.assertEqual(overrides["price_mode"], "maker")
        self.assertEqual(overrides["price_offset_bps"], 2.0)

    def test_execution_algo_update_payload_is_sanitized(self) -> None:
        overrides = _execution_algo_overrides_from_payload(
            {
                "enabled": True,
                "live_enabled": False,
                "exchange": "bybit-spot",
                "side": "buy",
                "algo": "pov",
                "total_quote": "25",
                "total_base": "0",
                "duration_seconds": "600",
                "slice_count": "5",
                "interval_seconds": "120",
                "participation_rate": "0.05",
                "volume_lookback_seconds": "300",
                "min_slice_quote": "1",
                "max_slice_quote": "10",
                "price_mode": "taker",
                "price_offset_bps": "1",
                "start_price": "0.1",
                "stop_price": "0.2",
                "max_slippage_bps": "20",
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["algo"], "pov")
        self.assertEqual(overrides["slice_count"], 5)
        self.assertEqual(overrides["participation_rate"], 0.05)
        self.assertEqual(overrides["max_slippage_bps"], 20.0)

    def test_backtest_update_payload_is_sanitized(self) -> None:
        overrides = _backtest_overrides_from_payload(
            {
                "enabled": True,
                "exchange": "bybit-spot",
                "strategy": "execution_algo",
                "initial_cash": "100",
                "initial_base": "5",
                "fee_bps": "10",
                "slippage_bps": "2",
                "price_start": "0.1",
                "price_end": "0.2",
                "step_count": "50",
                "volatility_bps": "100",
                "trend_bps": "-50",
                "max_recent_points": "25",
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["strategy"], "execution_algo")
        self.assertEqual(overrides["step_count"], 50)
        self.assertEqual(overrides["trend_bps"], -50.0)

    def test_grid_and_dca_update_payloads_reject_bad_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "spacing"):
            _spot_grid_overrides_from_payload({"spacing": "random"})
        with self.assertRaisesRegex(ValueError, "size_multiplier"):
            _dca_overrides_from_payload({"size_multiplier": "0.5"})
        with self.assertRaisesRegex(ValueError, "participation_rate"):
            _execution_algo_overrides_from_payload({"participation_rate": "1.5"})
        with self.assertRaisesRegex(ValueError, "strategy"):
            _backtest_overrides_from_payload({"strategy": "unknown"})

    def test_market_maker_update_payload_is_sanitized(self) -> None:
        overrides = _market_maker_overrides_from_payload(
            {
                "enabled": True,
                "live_enabled": False,
                "exchange": "bybit-spot",
                "levels": "6",
                "price_band_pct": "4.5",
                "quote_per_level": "2",
                "depth_shape": "linear",
                "min_order_quote": "0.5",
                "min_distance_bps": "20",
                "reprice_threshold_bps": "2.5",
                "reprice_hysteresis_bps": "3",
                "full_reprice_threshold_bps": "25",
                "adaptive_reprice_enabled": True,
                "adaptive_reprice_spread_fraction": "0.05",
                "max_order_quote": "3.5",
                "max_cycle_quote": "70",
                "max_open_orders": "40",
                "max_cancels_per_cycle": "12",
                "max_slippage_bps": "15",
                "max_order_book_gap_bps": "10000",
                "max_order_book_age_seconds": "3",
                "poll_seconds": "1",
                "inventory_control_enabled": True,
                "inventory_target_base": "100000",
                "inventory_band_base": "5000",
                "inventory_max_deviation_base": "20000",
                "post_only": True,
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertFalse(overrides["live_enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["levels"], 6)
        self.assertEqual(overrides["price_band_pct"], 4.5)
        self.assertEqual(overrides["quote_per_level"], 2.0)
        self.assertEqual(overrides["depth_shape"], "linear")
        self.assertEqual(overrides["min_order_quote"], 0.5)
        self.assertEqual(overrides["min_distance_bps"], 20.0)
        self.assertEqual(overrides["reprice_threshold_bps"], 2.5)
        self.assertEqual(overrides["reprice_hysteresis_bps"], 3.0)
        self.assertEqual(overrides["full_reprice_threshold_bps"], 25.0)
        self.assertTrue(overrides["adaptive_reprice_enabled"])
        self.assertEqual(overrides["adaptive_reprice_spread_fraction"], 0.05)
        self.assertEqual(overrides["max_order_quote"], 3.5)
        self.assertEqual(overrides["max_cycle_quote"], 70.0)
        self.assertEqual(overrides["max_open_orders"], 40)
        self.assertEqual(overrides["max_cancels_per_cycle"], 12)
        self.assertEqual(overrides["max_slippage_bps"], 15.0)
        self.assertEqual(overrides["max_order_book_gap_bps"], 10000.0)
        self.assertEqual(overrides["max_order_book_age_seconds"], 3.0)
        self.assertEqual(overrides["poll_seconds"], 1.0)
        self.assertTrue(overrides["inventory_control_enabled"])
        self.assertEqual(overrides["inventory_target_base"], 100000.0)
        self.assertEqual(overrides["inventory_band_base"], 5000.0)
        self.assertEqual(overrides["inventory_max_deviation_base"], 20000.0)
        self.assertTrue(overrides["post_only"])

        with self.assertRaisesRegex(ValueError, "inventory_max_deviation_base"):
            market_maker_config_from_payload(
                {
                    "inventory_control_enabled": True,
                    "inventory_band_base": 0,
                    "inventory_max_deviation_base": 0,
                }
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            market_maker_config_from_payload({"adaptive_reprice_spread_fraction": 1.1})

    def test_market_maker_update_repairs_stale_market_identity_id(self) -> None:
        base = MarketMakerConfig(
            id="bybit-spot-acs-usdt",
            enabled=True,
            exchange="bybit-spot",
            symbol="ACS/USDT",
            levels=20,
        )

        updated = market_maker_config_from_payload(
            {
                "id": "bybit-spot-acs-usdt",
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "levels": 20,
            },
            base_config=base,
            allowed_exchanges={"bybit-spot", "coinbase-spot"},
            symbols_by_exchange={
                "bybit-spot": ["ACS/USDT"],
                "coinbase-spot": ["ACS/USDC"],
            },
            repair_stale_identity_id=True,
        )

        self.assertEqual(updated.exchange, "coinbase-spot")
        self.assertEqual(updated.symbol, "ACS/USDC")
        self.assertEqual(updated.id, "coinbase-spot-acs-usdc")

    def test_market_maker_update_keeps_existing_id_when_market_identity_unchanged(
        self,
    ) -> None:
        base = MarketMakerConfig(
            id="upbit-spot-acs-usdt-mr0dsmi7",
            enabled=True,
            exchange="upbit-spot",
            symbol="ACS/USDT",
            levels=20,
        )

        updated = market_maker_config_from_payload(
            {
                "id": "upbit-spot-acs-usdt-mr0dsmi7",
                "levels": 10,
                "quote_per_level": 40,
            },
            base_config=base,
            allowed_exchanges={"upbit-spot"},
            symbols_by_exchange={"upbit-spot": ["ACS/USDT"]},
            repair_stale_identity_id=True,
        )

        self.assertEqual(updated.id, "upbit-spot-acs-usdt-mr0dsmi7")
        self.assertEqual(updated.exchange, "upbit-spot")
        self.assertEqual(updated.symbol, "ACS/USDT")
        self.assertEqual(updated.levels, 10)
        self.assertEqual(updated.quote_per_level, 40.0)

    def test_market_maker_restart_normalizes_stale_instance_id(self) -> None:
        base = MarketMakerConfig(
            id="coinbase-spot-acs-usdc-old",
            enabled=False,
            live_enabled=False,
            exchange="coinbase-spot",
            symbol="ACS/USDC",
        )

        updated = market_maker_config_from_payload(
            {
                "id": base.id,
                "enabled": True,
                "live_enabled": True,
                "exchange": base.exchange,
                "symbol": base.symbol,
            },
            base_config=base,
            allowed_exchanges={"coinbase-spot"},
            symbols_by_exchange={"coinbase-spot": ["ACS/USDC"]},
            repair_stale_identity_id=True,
            normalize_identity_id=True,
        )

        self.assertEqual(updated.id, "coinbase-spot-acs-usdc")

    def test_market_maker_replace_list_repairs_only_changed_market_id(self) -> None:
        base_configs = [
            MarketMakerConfig(
                id="bybit-spot-acs-usdt",
                enabled=True,
                exchange="bybit-spot",
                symbol="ACS/USDT",
            ),
            MarketMakerConfig(
                id="upbit-spot-acs-usdt-mr0dsmi7",
                enabled=True,
                exchange="upbit-spot",
                symbol="ACS/USDT",
            ),
        ]

        updated = market_maker_configs_from_payload(
            [
                {
                    "id": "bybit-spot-acs-usdt",
                    "enabled": True,
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                },
                {
                    "id": "upbit-spot-acs-usdt-mr0dsmi7",
                    "enabled": True,
                    "exchange": "upbit-spot",
                    "symbol": "ACS/USDT",
                },
            ],
            base_configs=base_configs,
            allowed_exchanges={"bybit-spot", "coinbase-spot", "upbit-spot"},
            symbols_by_exchange={
                "bybit-spot": ["ACS/USDT"],
                "coinbase-spot": ["ACS/USDC"],
                "upbit-spot": ["ACS/USDT"],
            },
            repair_stale_identity_id=True,
        )

        self.assertEqual(
            [(config.id, config.exchange, config.symbol) for config in updated],
            [
                ("coinbase-spot-acs-usdc", "coinbase-spot", "ACS/USDC"),
                ("upbit-spot-acs-usdt-mr0dsmi7", "upbit-spot", "ACS/USDT"),
            ],
        )

    def test_market_maker_update_payload_rejects_wrong_symbol(self) -> None:
        with self.assertRaisesRegex(ValueError, "symbol is not configured"):
            _market_maker_overrides_from_payload(
                {"exchange": "coinbase-spot", "symbol": "ACS/USDT"},
                allowed_exchanges={"coinbase-spot"},
                symbols_by_exchange={"coinbase-spot": ["ACS/USDC"]},
            )

    def test_market_maker_update_payload_rejects_unknown_depth_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "depth_shape"):
            _market_maker_overrides_from_payload(
                {"exchange": "bybit-spot", "depth_shape": "random"},
                allowed_exchanges={"bybit-spot"},
                symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
            )

    def test_risk_update_payload_is_sanitized(self) -> None:
        overrides = _risk_overrides_from_payload(
            {
                "allow_live_trading": True,
                "account_enabled": {"coinbase-spot": True, "bybit-spot": False},
                "strategy_enabled": {"market_maker": True, "slow_execution": False},
                "strategy_overrides": {
                    "market_maker": {
                        "max_order_quote": "25",
                        "max_open_orders": "80",
                    }
                },
                "max_order_quote": "5.5",
                "max_cycle_quote": "25",
                "max_exposure_quote": "250",
                "max_daily_loss_quote": "10",
                "max_orders_per_cycle": "8",
                "max_open_orders": "12",
                "max_cancels_per_cycle": "4",
                "min_seconds_between_cancels": "1.5",
                "min_order_book_depth_quote": "100",
                "max_slippage_bps": "12.5",
                "max_order_book_age_seconds": "60",
                "max_order_book_gap_bps": "250",
                "max_price_jump_bps": "80",
                "max_derivative_leverage": "3",
                "min_liquidation_buffer_pct": "20",
                "max_margin_usage_pct": "40",
            },
            allowed_accounts={"coinbase-spot", "bybit-spot"},
            allowed_strategies={"market_maker", "slow_execution"},
        )

        self.assertTrue(overrides["allow_live_trading"])
        self.assertFalse(overrides["account_enabled"]["bybit-spot"])
        self.assertFalse(overrides["strategy_enabled"]["slow_execution"])
        self.assertEqual(
            overrides["strategy_overrides"]["market_maker"]["max_order_quote"],
            25.0,
        )
        self.assertEqual(
            overrides["strategy_overrides"]["market_maker"]["max_open_orders"],
            80,
        )
        self.assertEqual(overrides["max_order_quote"], 5.5)
        self.assertEqual(overrides["max_cycle_quote"], 25.0)
        self.assertEqual(overrides["max_exposure_quote"], 250.0)
        self.assertEqual(overrides["max_daily_loss_quote"], 10.0)
        self.assertEqual(overrides["max_orders_per_cycle"], 8)
        self.assertEqual(overrides["max_open_orders"], 12)
        self.assertEqual(overrides["max_cancels_per_cycle"], 4)
        self.assertEqual(overrides["min_seconds_between_cancels"], 1.5)
        self.assertEqual(overrides["min_order_book_depth_quote"], 100.0)
        self.assertEqual(overrides["max_slippage_bps"], 12.5)
        self.assertEqual(overrides["max_order_book_age_seconds"], 60.0)
        self.assertEqual(overrides["max_order_book_gap_bps"], 250.0)
        self.assertEqual(overrides["max_price_jump_bps"], 80.0)
        self.assertEqual(overrides["max_derivative_leverage"], 3.0)
        self.assertEqual(overrides["min_liquidation_buffer_pct"], 20.0)
        self.assertEqual(overrides["max_margin_usage_pct"], 40.0)

    def test_risk_update_payload_rejects_unknown_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown exchange account"):
            _risk_overrides_from_payload(
                {"account_enabled": {"coinbase-spot": True}},
                allowed_accounts={"bybit-spot"},
                allowed_strategies={"market_maker"},
            )

    def test_risk_update_payload_rejects_unknown_strategy(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown strategy"):
            _risk_overrides_from_payload(
                {"strategy_enabled": {"unknown": True}},
                allowed_accounts={"bybit-spot"},
                allowed_strategies={"market_maker"},
            )

    def test_risk_update_payload_rejects_fractional_integer_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_open_orders must be an integer"):
            _risk_overrides_from_payload(
                {"max_open_orders": "1.5"},
                allowed_accounts={"bybit-spot"},
                allowed_strategies={"market_maker"},
            )

    def test_security_helpers_validate_session_and_ip(self) -> None:
        cfg = make_config()
        token = _make_session_token(cfg)
        user_token = _make_session_token(cfg, "trader@example.com")

        self.assertTrue(_session_valid(cfg, token))
        self.assertEqual(
            _session_identity(cfg, user_token), (True, "trader@example.com")
        )
        self.assertFalse(_session_valid(cfg, token + "bad"))
        self.assertTrue(_ip_allowed("66.96.212.97", ["66.96.212.97"]))
        self.assertTrue(_ip_allowed("66.96.212.97", ["66.96.212.0/24"]))
        self.assertFalse(_ip_allowed("66.96.213.1", ["66.96.212.0/24"]))

    def test_client_ip_trusts_real_ip_over_spoofable_forwarded_for(self) -> None:
        cfg = make_config(web_security=WebSecurityConfig(trust_proxy_headers=True))
        request = make_mocked_request(
            "GET",
            "/api/state",
            headers={
                "X-Forwarded-For": "203.0.113.7, 10.0.0.5",
                "X-Real-IP": "10.0.0.5",
            },
        )
        self.assertEqual(_client_ip(request, cfg), "10.0.0.5")

    def test_client_ip_uses_nearest_forwarded_for_hop_not_client_supplied_prefix(
        self,
    ) -> None:
        cfg = make_config(web_security=WebSecurityConfig(trust_proxy_headers=True))
        request = make_mocked_request(
            "GET",
            "/api/state",
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.5"},
        )
        self.assertEqual(_client_ip(request, cfg), "10.0.0.5")

    def test_client_ip_ignores_proxy_headers_when_not_trusted(self) -> None:
        cfg = make_config(web_security=WebSecurityConfig(trust_proxy_headers=False))
        request = make_mocked_request(
            "GET",
            "/api/state",
            headers={"X-Forwarded-For": "203.0.113.7", "X-Real-IP": "203.0.113.7"},
        )
        self.assertEqual(_client_ip(request, cfg), request.remote or "")

    def test_default_web_user_store_path_uses_security_config(self) -> None:
        cfg = make_config(
            web_security=WebSecurityConfig(user_store_path="data/users/web_users.json")
        )

        self.assertEqual(default_web_user_store_path(cfg), "data/users/web_users.json")

    def test_default_strategy_center_path_uses_config(self) -> None:
        cfg = make_config()

        self.assertEqual(
            default_strategy_center_path(cfg), "data/strategy_center.sqlite3"
        )

    def test_user_role_and_asset_permission_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebUserStore(Path(tmp) / "users.json")
            admin = store.create_user(
                email="admin@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS"],
            )
            user = store.create_user(
                email="user@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS"],
            )
            unassigned_user = store.create_user(
                email="unassigned@example.com",
                password="Strong-pass-1!",
            )

        self.assertEqual(admin.role, "admin")
        self.assertEqual(user.role, "user")
        _require_admin_user(admin)
        _require_user_assets(user, ["ACS"])
        _require_user_assets(admin, ["BTC"])
        with self.assertRaisesRegex(PermissionError, "admin role"):
            _require_admin_user(user)
        with self.assertRaisesRegex(PermissionError, "BTC"):
            _require_user_assets(user, ["BTC"])
        with self.assertRaisesRegex(PermissionError, "ACS"):
            _require_user_assets(unassigned_user, ["ACS"])

    def test_state_payload_includes_all_allowed_assets_despite_preference(self) -> None:
        cfg = make_config(
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                ),
                SpotMarketConfig(
                    asset="BTC",
                    exchange="binance-spot",
                    symbol="BTC/USDT",
                    quote_currency="USDT",
                ),
            ],
            portfolio=PortfolioConfig(
                enabled=True,
                positions=[
                    AssetPosition(asset="ACS", position_base=100.0),
                    AssetPosition(asset="BTC", position_base=1.0),
                ],
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            user = WebUserStore(Path(tmp) / "users.json").create_user(
                email="trader@example.com",
                password="Strong-pass-1!",
                allowed_assets=["ACS", "BTC"],
                preferred_asset="ACS",
            )
        payload = {
            "markets": [
                {"asset": "ACS", "symbol": "ACS/USDC"},
                {"asset": "BTC", "symbol": "BTC/USDT"},
            ],
            "config": {
                "spot_markets": [
                    {"asset": "ACS", "symbol": "ACS/USDC"},
                    {"asset": "BTC", "symbol": "BTC/USDT"},
                ],
            },
            "opportunities": [
                {"metadata": {"asset": "ACS"}},
                {"metadata": {"asset": "BTC"}},
            ],
            "recent_opportunities": [
                {"asset": "ACS"},
                {"asset": "BTC"},
            ],
            "portfolio": {
                "positions": [
                    {"asset": "ACS", "position_base": 100.0},
                    {"asset": "BTC", "position_base": 1.0},
                ],
            },
            "market_maker": {
                "status": "planned",
                "config": {"symbol": "BTC/USDT"},
                "plan": {"symbol": "BTC/USDT"},
                "accounts": [{"key": "binance-spot", "symbols": ["BTC/USDT"]}],
            },
            "slow_execution": {
                "status": "planned",
                "config": {"symbol": "ACS/USDC"},
                "plan": {"symbol": "ACS/USDC"},
                "tasks": {
                    "tasks": [
                        {
                            "id": "acs-task",
                            "status": "running",
                            "config": {"symbol": "ACS/USDC"},
                        },
                        {
                            "id": "btc-task",
                            "status": "running",
                            "config": {"symbol": "BTC/USDT"},
                        },
                    ]
                },
            },
            "cross_exchange_rebalance": {
                "status": "planned",
                "config": {
                    "buy_symbol": "BTC/USDT",
                    "sell_symbol": "BTC/USDT",
                },
                "plan": {
                    "buy_symbol": "BTC/USDT",
                    "sell_symbol": "BTC/USDT",
                },
                "accounts": [{"key": "binance-spot", "symbols": ["BTC/USDT"]}],
            },
            "order_activity": {
                "accounts": [
                    {
                        "exchange": "coinbase-spot",
                        "symbols": ["ACS/USDC", "BTC/USDT"],
                        "open_orders": [
                            {"symbol": "ACS/USDC", "id": "acs-order"},
                            {"symbol": "BTC/USDT", "id": "btc-order"},
                        ],
                    }
                ],
                "open_orders": [
                    {"symbol": "ACS/USDC", "id": "acs-order"},
                    {"symbol": "BTC/USDT", "id": "btc-order"},
                ],
                "closed_orders": [],
                "recent_trades": [
                    {"symbol": "ACS/USDC", "id": "acs-fill"},
                    {"symbol": "BTC/USDT", "id": "btc-fill"},
                ],
                "pnl_summary": {"currency": "USD"},
                "reconciliation": {
                    "issues": [
                        {"symbol": "ACS/USDC", "level": "warning"},
                        {"symbol": "BTC/USDT", "level": "error"},
                    ]
                },
            },
            "account_balances": {
                "accounts": [
                    {
                        "exchange": "coinbase-spot",
                        "symbols": ["ACS/USDC", "BTC/USDT"],
                        "balance": {
                            "currencies": [
                                {"currency": "ACS", "total": 100.0},
                                {"currency": "BTC", "total": 1.0},
                                {"currency": "USDC", "total": 50.0},
                            ]
                        },
                    }
                ],
                "totals": [
                    {"currency": "ACS", "total": 100.0},
                    {"currency": "BTC", "total": 1.0},
                    {"currency": "USDC", "total": 50.0},
                ],
            },
            "trading_console": {
                "accounts": [{"key": "coinbase-spot"}],
                "strategies": [
                    {"id": "market_maker", "symbol": "BTC/USDT"},
                    {"id": "slow_execution", "symbol": "ACS/USDC"},
                    {"id": "spot_spread", "symbol": "ACS,BTC"},
                ],
            },
            "strategy_lifecycle": {
                "status": "blocked",
                "instances": [
                    {
                        "key": "slow_execution:acs-task",
                        "strategy_id": "slow_execution",
                        "symbol": "ACS/USDC",
                        "converged": True,
                        "convergence_state": "in_sync",
                    },
                    {
                        "key": "market_maker:btc",
                        "strategy_id": "market_maker",
                        "symbol": "BTC/USDT",
                        "converged": False,
                        "convergence_state": "blocked",
                    },
                    {
                        "key": "spot_spread:default",
                        "strategy_id": "spot_spread",
                        "symbol": "ACS,BTC",
                        "converged": True,
                        "convergence_state": "in_sync",
                    },
                ],
            },
        }

        filtered = _filter_state_payload_for_user(payload, cfg=cfg, user=user)

        self.assertEqual(
            [row["asset"] for row in filtered["markets"]],
            ["ACS", "BTC"],
        )
        self.assertEqual(
            [row["asset"] for row in filtered["config"]["spot_markets"]],
            ["ACS", "BTC"],
        )
        self.assertEqual(
            [row["asset"] for row in filtered["portfolio"]["positions"]],
            ["ACS", "BTC"],
        )
        self.assertEqual(len(filtered["opportunities"]), 2)
        self.assertEqual(filtered["market_maker"]["status"], "planned")
        self.assertEqual(filtered["cross_exchange_rebalance"]["status"], "planned")
        self.assertEqual(
            [task["id"] for task in filtered["slow_execution"]["tasks"]["tasks"]],
            ["acs-task", "btc-task"],
        )
        self.assertEqual(
            [row["id"] for row in filtered["order_activity"]["open_orders"]],
            ["acs-order", "btc-order"],
        )
        self.assertEqual(
            [row["id"] for row in filtered["order_activity"]["recent_trades"]],
            ["acs-fill", "btc-fill"],
        )
        self.assertEqual(
            [
                row["currency"]
                for row in filtered["account_balances"]["accounts"][0]["balance"][
                    "currencies"
                ]
            ],
            ["ACS", "BTC", "USDC"],
        )
        self.assertEqual(
            [row["id"] for row in filtered["trading_console"]["strategies"]],
            ["market_maker", "slow_execution", "spot_spread"],
        )
        self.assertEqual(
            filtered["trading_console"]["strategies"][2]["symbol"], "ACS,BTC"
        )
        self.assertEqual(
            [row["key"] for row in filtered["strategy_lifecycle"]["instances"]],
            [
                "slow_execution:acs-task",
                "market_maker:btc",
                "spot_spread:default",
            ],
        )
        self.assertEqual(
            filtered["strategy_lifecycle"]["instances"][2]["symbol"],
            "ACS,BTC",
        )
        self.assertEqual(
            filtered["strategy_lifecycle"]["summary"]["attention_count"],
            1,
        )
        self.assertEqual(filtered["auth"]["mode"], "user")
        self.assertEqual(filtered["auth"]["email"], "trader@example.com")
        self.assertEqual(filtered["auth"]["asset_scope"], ["ACS", "BTC"])

    def test_add_security_headers_preserves_existing_values(self) -> None:
        response = web.Response()
        response.headers["X-Frame-Options"] = "SAMEORIGIN"

        _add_security_headers(response)

        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(
            response.headers["X-Content-Type-Options"],
            SECURITY_HEADERS["X-Content-Type-Options"],
        )
        self.assertEqual(
            response.headers["Referrer-Policy"],
            SECURITY_HEADERS["Referrer-Policy"],
        )
        self.assertEqual(
            response.headers["Strict-Transport-Security"],
            SECURITY_HEADERS["Strict-Transport-Security"],
        )

    def test_daily_report_due_and_message(self) -> None:
        cfg = make_config(
            alerts=AlertConfig(
                daily_report_enabled=True,
                daily_report_time="00:00",
            )
        )

        previous_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            # 2024-01-01 12:00:00 UTC and one minute later. Both checks land on
            # the same local day, so the second one must not re-trigger the
            # daily report. Pinning TZ keeps the result host-timezone agnostic.
            due, day = _daily_report_due(
                cfg,
                last_report_day=None,
                now=1_704_110_400,
            )
            not_due, _ = _daily_report_due(
                cfg,
                last_report_day=day,
                now=1_704_110_460,
            )
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()

        message = build_daily_report_message(
            cfg,
            scan_count=12,
            order_activity={
                "daily_pnl": {
                    "total_realized_pnl": 1.25,
                    "trade_count": 2,
                    "sources": {
                        "auto_buy_sell": {
                            "realized_pnl": 1.25,
                            "trade_count": 2,
                        }
                    },
                },
                "open_order_count": 1,
                "recent_trade_count": 2,
            },
            account_balances={"checked_account_count": 1, "total_account_count": 2},
            trading_console={"live_trading": False},
            auto_buy_sell_tasks={"active_count": 1, "task_count": 1},
            warnings=["warning"],
        )

        self.assertTrue(due)
        self.assertFalse(not_due)
        self.assertIn("Daily P/L: 1.25000000 USD", message)
        self.assertIn("Auto Buy/Sell tasks: 1 active / 1 total", message)

    def test_operations_payload_includes_risk_and_recent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_operations_payload(
                make_config(
                    trade_log=TradeLogConfig(
                        enabled=False,
                        path=os.path.join(tmp, "trade_events.jsonl"),
                    ),
                    strategy_timeline=StrategyTimelineConfig(
                        enabled=False,
                        path=os.path.join(tmp, "strategy_timeline.jsonl"),
                    ),
                )
            )

        self.assertIn("risk", payload)
        self.assertIn("trade_log", payload)
        self.assertIn("strategy_timeline", payload)
        self.assertIn("web_audit", payload)
        self.assertIn("alerts", payload)
        self.assertFalse(payload["risk"]["allow_live_trading"])
        self.assertEqual(payload["trade_log"]["recent_events"], [])
        self.assertEqual(payload["trade_log"]["recent_entries"], [])
        self.assertEqual(payload["trade_log"]["summary"]["event_count"], 0)
        self.assertEqual(payload["strategy_timeline"]["recent_events"], [])
        self.assertEqual(payload["strategy_timeline"]["summary"]["event_count"], 0)
        self.assertEqual(payload["web_audit"]["recent_events"], [])

    def test_operations_payload_compacts_trade_log_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                trade_log=TradeLogConfig(
                    enabled=True,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                    max_recent_events=10,
                )
            )
            write_trade_event(
                cfg.trade_log,
                {
                    "type": "market_maker",
                    "strategy": "market_maker",
                    "mode": "live",
                    "status": "placed",
                    "plan": {
                        "exchange": "coinbase",
                        "symbol": "ACS/USDC",
                        "orders": [
                            {
                                "side": "buy",
                                "price": 0.00012,
                                "amount": 1000,
                                "debug_blob": "x" * 200_000,
                            }
                        ],
                    },
                    "risk": {
                        "level": "ok",
                        "approved": True,
                        "order_count": 1,
                        "total_quote_notional": 1.0,
                    },
                    "execution": {
                        "placed_count": 1,
                        "canceled_count": 0,
                        "placed_order_ids": ["order-mm-1"],
                        "raw_response": {"debug_blob": "y" * 200_000},
                    },
                    "market_data": {"debug_blob": "z" * 200_000},
                },
            )

            operations = build_operations_payload(cfg)

        row = operations["trade_log"]["recent_entries"][0]
        self.assertEqual(row["strategy"], "market_maker")
        self.assertEqual(row["exchange"], "coinbase")
        self.assertEqual(row["symbol"], "ACS/USDC")
        self.assertEqual(row["side"], "buy")
        self.assertNotIn("raw", row)
        self.assertNotIn("placed_order_ids", row)
        self.assertEqual(operations["trade_log"]["recent_events"], [row])
        self.assertLess(len(json.dumps(operations["trade_log"])), 5000)

    def test_operations_payload_compacts_strategy_timeline_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                strategy_timeline=StrategyTimelineConfig(
                    enabled=True,
                    path=os.path.join(tmp, "strategy_timeline.jsonl"),
                    max_recent_events=10,
                )
            )
            write_strategy_timeline_from_payload(
                cfg.strategy_timeline,
                {
                    "type": "spot_spread_execution",
                    "strategy": "spot_spread",
                    "mode": "live",
                    "status": "blocked_by_risk",
                    "plan": {
                        "exchange": "multi",
                        "symbol": "ACS",
                        "orders": [
                            {
                                "exchange": "coinbase-spot",
                                "symbol": "ACS/USDC",
                                "side": "buy",
                                "slippage_bps": 12.5,
                            }
                        ],
                    },
                    "risk": {
                        "level": "blocked",
                        "approved": False,
                        "reasons": ["risk.allow_live_trading is false"],
                    },
                    "timing": {"opportunity_age_ms": 88.0},
                },
                source="test",
            )

            operations = build_operations_payload(cfg)

        row = operations["strategy_timeline"]["recent_entries"][0]
        self.assertEqual(row["action"], "blocked")
        self.assertEqual(row["accounts"], ["coinbase-spot"])
        self.assertIn("ACS/USDC", row["symbols"])
        self.assertEqual(row["reason"], "risk.allow_live_trading is false")
        self.assertNotIn("raw", row)
        self.assertEqual(operations["strategy_timeline"]["summary"]["blocked_count"], 1)

    def test_trade_log_tail_reader_returns_recent_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trade_events.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for index in range(20):
                    handle.write(
                        json.dumps(
                            {
                                "type": "market_maker",
                                "status": f"event-{index}",
                                "payload": "x" * 5000,
                            },
                            sort_keys=True,
                        )
                    )
                    handle.write("\n")

            lines = _read_recent_event_lines(Path(path), 3)

        statuses = [json.loads(line)["status"] for line in lines]
        self.assertEqual(statuses, ["event-17", "event-18", "event-19"])

    def test_web_audit_events_round_trip_and_redact_sensitive_values(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/risk"
            method = "POST"

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                trade_log=TradeLogConfig(
                    enabled=True,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                )
            )
            event = write_web_audit_event(
                cfg,
                FakeRequest(),  # type: ignore[arg-type]
                action="risk_config",
                target="risk",
                detail="updated risk controls",
                payload={
                    "allow_live_trading": True,
                    "api_key": "secret-value",
                },
            )
            events = read_recent_web_audit_events(cfg)
            operations = build_operations_payload(cfg)

        self.assertEqual(event["status"], "ok")
        self.assertEqual(events[0]["action"], "risk_config")
        self.assertEqual(events[0]["payload"]["api_key"], "[redacted]")
        self.assertTrue(default_web_audit_path(cfg).endswith("web_audit_events.jsonl"))
        self.assertEqual(
            operations["web_audit"]["recent_events"][0]["event_id"],
            events[0]["event_id"],
        )

    def test_web_audit_events_rotate_with_trade_log_settings(self) -> None:
        class FakeRequest:
            headers = {"User-Agent": "unit-test"}
            remote = "127.0.0.1"
            path = "/api/risk"
            method = "POST"

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                trade_log=TradeLogConfig(
                    enabled=True,
                    path=os.path.join(tmp, "trade_events.jsonl"),
                    rotate_max_bytes=1,
                    rotate_keep_files=2,
                    rotate_compress=False,
                )
            )
            write_web_audit_event(
                cfg,
                FakeRequest(),  # type: ignore[arg-type]
                action="first",
            )
            write_web_audit_event(
                cfg,
                FakeRequest(),  # type: ignore[arg-type]
                action="second",
            )
            audit_path = Path(default_web_audit_path(cfg))
            rotated = sorted(audit_path.parent.glob("web_audit_events.jsonl.*"))
            rotated_text = rotated[0].read_text(encoding="utf-8")

            self.assertEqual(len(rotated), 1)
            self.assertIn('"action": "first"', rotated_text)

    def test_build_portfolio_pnl_splits_sources(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                asset="ACS",
                position_base=10_000.0,
                average_entry_price=0.00010,
                cash_balances={"USDC": 10.0, "USDT": 20.0, "KRW": 10_000.0},
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

        payload = build_portfolio_pnl(
            cfg,
            books,
            {"USDC": 1.0, "USDT": 1.0, "KRW": 0.00075},
        )

        self.assertEqual(payload["status"], "ok")
        self.assertAlmostEqual(payload["mark_price"], 0.00015)
        self.assertEqual(payload["positions"][0]["asset"], "ACS")
        self.assertAlmostEqual(payload["cash_balances_common"]["USDC"], 10.0)
        self.assertAlmostEqual(payload["cash_balances_common"]["USDT"], 20.0)
        self.assertAlmostEqual(payload["cash_balances_common"]["KRW"], 7.5)
        self.assertAlmostEqual(payload["cash_value"], 37.5)
        self.assertAlmostEqual(payload["sources"]["price_move"], 0.5)
        self.assertAlmostEqual(payload["sources"]["market_maker"], 1.25)
        self.assertAlmostEqual(payload["sources"]["arbitrage"], 2.5)
        self.assertAlmostEqual(payload["total_pnl"], 4.25)

    def test_build_portfolio_pnl_sums_multiple_assets(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                positions=[
                    AssetPosition(
                        asset="ACS",
                        position_base=10_000.0,
                        average_entry_price=0.00010,
                    ),
                    AssetPosition(
                        asset="XYZ",
                        position_base=2.0,
                        average_entry_price=2.0,
                    ),
                ],
                realized_pnl={"market_maker": 1.0, "arbitrage": 2.0},
            ),
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
                SpotMarketConfig(
                    asset="XYZ",
                    exchange="bybit-spot",
                    symbol="XYZ/USDT",
                    quote_currency="USDT",
                ),
            ],
        )
        books = {
            ("bybit-spot", "ACS/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="ACS/USDT",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            ),
            ("bybit-spot", "XYZ/USDT"): OrderBookSnapshot(
                exchange="bybit-spot",
                symbol="XYZ/USDT",
                bids=[BookLevel(price=2.9, amount=10)],
                asks=[BookLevel(price=3.1, amount=10)],
            ),
        }

        payload = build_portfolio_pnl(cfg, books, {"USDT": 1.0})

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["positions"]), 2)
        self.assertAlmostEqual(payload["positions"][0]["position_value"], 1.5)
        self.assertAlmostEqual(payload["positions"][1]["position_value"], 6.0)
        self.assertAlmostEqual(payload["position_value"], 7.5)
        self.assertAlmostEqual(payload["sources"]["price_move"], 2.5)
        self.assertAlmostEqual(payload["total_pnl"], 5.5)

    def test_build_portfolio_pnl_reports_missing_cash_rates(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                asset="ACS",
                cash_balances={"EUR": 100.0, "USDT": 5.0},
            )
        )

        payload = build_portfolio_pnl(cfg, {}, {"USDT": 1.0})

        self.assertEqual(payload["cash_missing_rates"], ["EUR"])
        self.assertAlmostEqual(payload["cash_value"], 5.0)

    def test_synced_portfolio_uses_live_account_balances(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                positions=[
                    AssetPosition(
                        asset="ACS",
                        position_base=0.0,
                        average_entry_price=0.0,
                    )
                ],
                cash_balances={"USDC": 0.0},
            ),
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
        )
        books = {
            ("coinbase-spot", "ACS/USDC"): OrderBookSnapshot(
                exchange="coinbase-spot",
                symbol="ACS/USDC",
                bids=[BookLevel(price=0.00014, amount=100_000)],
                asks=[BookLevel(price=0.00016, amount=100_000)],
            )
        }
        account_balances = {
            "status": "ok",
            "checked_account_count": 1,
            "last_finished": 123.0,
            "totals": [
                {"currency": "ACS", "free": 1000.0, "used": 100.0, "total": 1100.0},
                {"currency": "USDC", "free": 10.0, "used": 5.0, "total": 15.0},
                {"currency": "USD", "free": 2.0, "used": 0.0, "total": 2.0},
            ],
        }

        payload = build_synced_portfolio_pnl(
            cfg,
            books,
            {"USDC": 1.0, "USD": 1.0},
            account_balances,
        )

        self.assertEqual(payload["balance_source"], "live_accounts")
        self.assertAlmostEqual(payload["position_base"], 1100.0)
        self.assertAlmostEqual(payload["positions"][0]["position_value"], 0.165)
        self.assertAlmostEqual(payload["cash_balances"]["USDC"], 15.0)
        self.assertAlmostEqual(payload["cash_balances"]["USD"], 2.0)
        self.assertAlmostEqual(payload["cash_value"], 17.0)
        self.assertAlmostEqual(payload["sources"]["price_move"], 0.0)

    def test_synced_portfolio_falls_back_without_private_balances(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                asset="ACS",
                position_base=100.0,
                average_entry_price=0.00010,
                cash_balances={"USDC": 3.0},
            )
        )

        payload = build_synced_portfolio_pnl(
            cfg,
            {},
            {"USDC": 1.0},
            {"checked_account_count": 0, "totals": []},
        )

        self.assertEqual(payload["balance_source"], "configured")
        self.assertAlmostEqual(payload["position_base"], 100.0)
        self.assertAlmostEqual(payload["cash_value"], 3.0)

    def test_trade_pnl_uses_order_attribution_and_cost_basis(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                positions=[
                    AssetPosition(
                        asset="ACS",
                        position_base=1_000.0,
                        average_entry_price=0.00010,
                    )
                ],
            ),
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="coinbase-spot",
                    symbol="ACS/USDC",
                    quote_currency="USDC",
                )
            ],
        )
        entry = normalize_trade_event(
            {
                "logged_at": 123.0,
                "type": "market_maker",
                "strategy": "market_maker",
                "mode": "live",
                "status": "placed",
                "plan": {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "order": {"side": "sell"},
                },
                "execution": {
                    "placed_count": 1,
                    "canceled_count": 0,
                    "placed_order_ids": ["order-mm-1"],
                },
                "risk": {
                    "approved": True,
                    "level": "ok",
                    "order_count": 1,
                    "total_quote_notional": 0.15,
                },
            }
        )
        attribution = build_order_attribution_map([entry])

        enriched, summary = enrich_recent_trades_with_pnl(
            cfg,
            [
                {
                    "exchange": "coinbase-spot",
                    "symbol": "ACS/USDC",
                    "side": "sell",
                    "order_id": "order-mm-1",
                    "price": 0.00015,
                    "amount": 1_000.0,
                    "cost": 0.15,
                    "fee": {"cost": 0.0001, "currency": "USDC"},
                }
            ],
            quote_rates={"USDC": 1.0},
            books={},
            attribution=attribution,
        )

        self.assertEqual(enriched[0]["source"], "market_maker")
        self.assertEqual(summary["attributed_trade_count"], 1)
        self.assertAlmostEqual(
            summary["sources"]["market_maker"]["realized_pnl"],
            0.0499,
        )
        self.assertAlmostEqual(
            summary["sources"]["market_maker"]["fees_common"],
            0.0001,
        )

    def test_synced_portfolio_adds_attributed_fill_pnl(self) -> None:
        cfg = make_config(
            portfolio=PortfolioConfig(
                enabled=True,
                positions=[
                    AssetPosition(
                        asset="ACS",
                        position_base=10_000.0,
                        average_entry_price=0.00010,
                    )
                ],
                realized_pnl={"market_maker": 1.0, "arbitrage": 2.0},
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
        order_activity = {
            "pnl_summary": {
                "window": "recent_fills",
                "observed_at": 123.0,
                "sources": {
                    "market_maker": {"realized_pnl": 0.25},
                    "auto_buy_sell": {"realized_pnl": -0.01},
                },
            }
        }

        payload = build_synced_portfolio_pnl(
            cfg,
            books,
            {"USDT": 1.0},
            {"checked_account_count": 0, "totals": []},
            order_activity,
        )

        self.assertAlmostEqual(payload["sources"]["market_maker"], 1.25)
        self.assertAlmostEqual(payload["sources"]["arbitrage"], 2.0)
        self.assertAlmostEqual(payload["sources"]["auto_buy_sell"], -0.01)
        self.assertAlmostEqual(payload["sources"]["price_move"], 0.5)
        self.assertAlmostEqual(payload["total_pnl"], 3.74)
        self.assertEqual(payload["fill_pnl_window"], "recent_fills")

    def test_order_reconciliation_detects_mismatches(self) -> None:
        payload = build_order_reconciliation_payload(
            {
                "status": "ok",
                "open_orders": [
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                        "id": "manual-open-1",
                    }
                ],
                "closed_orders": [],
                "recent_trades": [
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                        "order_id": "auto-local-1",
                        "source": "auto_buy_sell",
                    },
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                        "order_id": "manual-fill-1",
                        "source": "unattributed",
                    },
                ],
            },
            market_maker_runtime={
                "open_order_exchange": "coinbase-spot",
                "open_order_symbol": "ACS/USDC",
                "open_order_ids": ["mm-local-1"],
            },
            auto_buy_sell_tasks={
                "tasks": [
                    {
                        "id": "task-1",
                        "config": {
                            "exchange": "coinbase-spot",
                            "symbol": "ACS/USDC",
                        },
                        "open_order_ids": ["auto-local-1"],
                        "placed_order_ids": ["auto-local-1"],
                    }
                ]
            },
        )

        issue_types = {issue["type"] for issue in payload["issues"]}
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["tracked_order_count"], 2)
        self.assertEqual(payload["matched_fill_count"], 1)
        self.assertEqual(payload["untracked_open_count"], 1)
        self.assertEqual(payload["unattributed_fill_count"], 1)
        self.assertEqual(payload["issue_count"], 2)
        self.assertEqual(payload["notice_count"], 2)
        self.assertEqual(payload["total_item_count"], 4)
        self.assertEqual(payload["critical_issue_count"], 0)
        self.assertFalse(payload["auto_stop_recommended"])
        self.assertEqual(payload["level_counts"]["warning"], 2)
        self.assertEqual(payload["level_counts"]["info"], 2)
        self.assertIn("tracked_order_missing", issue_types)
        self.assertIn("tracked_order_filled_not_cleared", issue_types)
        self.assertIn("untracked_open_order", issue_types)
        self.assertIn("unattributed_fill", issue_types)

    def test_order_reconciliation_retries_activity_errors_without_global_stop(
        self,
    ) -> None:
        payload = build_order_reconciliation_payload(
            {
                "status": "error",
                "open_orders": [],
                "closed_orders": [],
                "recent_trades": [],
            }
        )

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["issue_count"], 1)
        self.assertEqual(payload["notice_count"], 0)
        self.assertEqual(payload["total_item_count"], 1)
        self.assertEqual(payload["critical_issue_count"], 0)
        self.assertFalse(payload["auto_stop_recommended"])
        self.assertEqual(payload["recoverable_issue_count"], 1)
        self.assertTrue(payload["automatic_retry_active"])
        self.assertIn("order_activity_error", payload["recoverable_reasons"][0])

    def test_order_reconciliation_isolates_partial_account_errors(self) -> None:
        payload = build_order_reconciliation_payload(
            {
                "status": "error",
                "accounts": [
                    {
                        "exchange": "coinbase-spot",
                        "status": "error",
                        "errors": ["open orders unavailable"],
                    },
                    {
                        "exchange": "upbit-spot",
                        "status": "ok",
                        "errors": [],
                    },
                ],
                "open_orders": [],
                "closed_orders": [],
                "recent_trades": [],
            }
        )

        self.assertEqual(payload["status"], "warning")
        self.assertFalse(payload["auto_stop_recommended"])
        self.assertEqual(payload["critical_issue_count"], 0)
        self.assertEqual(payload["issues"][0]["type"], "account_order_activity_error")
        self.assertEqual(payload["issues"][0]["exchange"], "coinbase-spot")

    def test_uncertain_intent_warns_but_does_not_stop_other_accounts(self) -> None:
        payload = build_order_reconciliation_payload(
            {
                "status": "ok",
                "open_orders": [],
                "closed_orders": [],
                "recent_trades": [],
                "reliability": {"pending_count": 1, "unresolved_count": 1},
            }
        )

        self.assertEqual(payload["status"], "warning")
        self.assertFalse(payload["auto_stop_recommended"])
        self.assertEqual(payload["critical_issue_count"], 0)
        self.assertEqual(payload["issues"][0]["type"], "uncertain_order_intent")

    def test_order_reconciliation_does_not_auto_stop_for_info_only_items(self) -> None:
        payload = build_order_reconciliation_payload(
            {
                "status": "ok",
                "open_orders": [
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                        "id": "manual-open-1",
                    }
                ],
                "closed_orders": [],
                "recent_trades": [
                    {
                        "exchange": "coinbase-spot",
                        "symbol": "ACS/USDC",
                        "order_id": "manual-fill-1",
                        "source": "unattributed",
                    },
                ],
            }
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["issue_count"], 0)
        self.assertEqual(payload["notice_count"], 2)
        self.assertEqual(payload["total_item_count"], 2)
        self.assertEqual(payload["critical_issue_count"], 0)
        self.assertFalse(payload["auto_stop_recommended"])
        self.assertEqual(payload["level_counts"]["info"], 2)

    def test_order_reconciliation_does_not_auto_stop_for_unmanaged_attributed_orders(
        self,
    ) -> None:
        payload = build_order_reconciliation_payload(
            {
                "status": "ok",
                "open_orders": [
                    {
                        "exchange": "upbit-spot",
                        "symbol": "ACS/USDT",
                        "id": "mm-existing-1",
                        "attribution": {
                            "strategy": "market_maker",
                            "event_id": "previous-run",
                        },
                    }
                ],
                "closed_orders": [],
                "recent_trades": [],
            }
        )

        issue_types = {issue["type"] for issue in payload["issues"]}
        self.assertEqual(payload["status"], "warning")
        self.assertIn("unmanaged_strategy_order", issue_types)
        self.assertEqual(payload["issue_count"], 1)
        self.assertEqual(payload["notice_count"], 0)
        self.assertEqual(payload["critical_issue_count"], 0)
        self.assertFalse(payload["auto_stop_recommended"])

    def test_market_maker_sync_delta_detects_missing_tracked_orders(self) -> None:
        delta = _market_maker_order_sync_delta(
            ["mm-1", "mm-2", "mm-3"],
            {
                "source": "exchange",
                "order_ids": ["mm-1", "mm-3", "manual-1"],
                "error": None,
            },
        )

        self.assertTrue(delta["exchange_confirmed"])
        self.assertTrue(delta["changed"])
        self.assertEqual(delta["missing_tracked_order_ids"], ["mm-2"])
        self.assertEqual(delta["new_exchange_order_ids"], ["manual-1"])

    def test_market_maker_force_replace_on_sync_id_mismatch(self) -> None:
        previous_plan = {
            "orders": [
                {"side": "buy", "level": 1},
                {"side": "sell", "level": 1},
            ]
        }
        delta = _market_maker_order_sync_delta(
            ["mm-1", "mm-2"],
            {
                "source": "exchange",
                "order_ids": ["mm-1", "manual-1"],
                "error": None,
            },
        )

        reason = _market_maker_force_replace_reason(
            ["mm-1", "manual-1"],
            previous_plan,
            order_sync=delta,
        )

        self.assertEqual(
            reason,
            "exchange open orders differ from tracked MM ids; assuming fill/cancel drift",
        )

    def test_market_maker_force_replace_on_open_order_count_mismatch(self) -> None:
        previous_plan = {
            "orders": [
                {"side": "buy", "level": 1},
                {"side": "sell", "level": 1},
            ]
        }

        reason = _market_maker_force_replace_reason(
            ["mm-1"],
            previous_plan,
            order_sync={
                "source": "exchange",
                "exchange_confirmed": True,
                "changed": False,
            },
        )

        self.assertEqual(
            reason,
            "open order count differs from previous MM plan; assuming fill/cancel drift",
        )

    def test_market_maker_force_replace_on_partial_fill_or_config_change(self) -> None:
        previous_plan = {
            "orders": [
                {"side": "buy", "level": 1},
                {"side": "sell", "level": 1},
            ]
        }
        partial_fill_reason = _market_maker_force_replace_reason(
            ["mm-1", "mm-2"],
            previous_plan,
            existing_open_orders=[
                {"id": "mm-1", "amount": 10, "remaining": 9, "filled": 1},
                {"id": "mm-2", "amount": 10, "remaining": 10, "filled": 0},
            ],
        )
        config_reason = _market_maker_force_replace_reason(
            ["mm-1", "mm-2"],
            previous_plan,
            config_changed=True,
        )

        self.assertEqual(
            partial_fill_reason,
            "an MM order is partially filled; rebuilding the full ladder",
        )
        self.assertEqual(config_reason, "market maker configuration changed")

    def test_auto_stop_decision_stops_immediately_for_daily_loss(self) -> None:
        triggered, reason = _monitor_auto_stop_decision(
            auto_stop_enabled=True,
            auto_stop_consecutive_errors=3,
            daily_loss_stop=True,
            reconciliation_stop=False,
            consecutive_problem_cycles=1,
        )

        self.assertTrue(triggered)
        self.assertEqual(reason, "daily loss limit breached")

    def test_auto_stop_decision_debounces_reconciliation_issues(self) -> None:
        triggered, reason = _monitor_auto_stop_decision(
            auto_stop_enabled=True,
            auto_stop_consecutive_errors=3,
            daily_loss_stop=False,
            reconciliation_stop=True,
            consecutive_problem_cycles=1,
        )

        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_auto_stop_decision_ignores_generic_degraded_warnings(self) -> None:
        triggered, reason = _monitor_auto_stop_decision(
            auto_stop_enabled=True,
            auto_stop_consecutive_errors=3,
            daily_loss_stop=False,
            reconciliation_stop=False,
            consecutive_problem_cycles=99,
        )

        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_global_scan_health_warnings_ignore_onchain_errors(self) -> None:
        warnings = _global_scan_health_warnings(
            onchain_payload={
                "status": "error",
                "error": "Rate limit exceeded",
            },
            account_balances_payload={"status": "ok", "errors": []},
            order_activity_payload={"status": "ok", "errors": []},
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            _global_scan_health_warnings(
                account_balances_payload={
                    "status": "error",
                    "errors": ["balance failed"],
                },
                order_activity_payload={
                    "status": "error",
                    "errors": ["orders failed"],
                },
            ),
            ["Account balances: balance failed", "Orders: orders failed"],
        )

    def test_auto_stop_decision_stops_on_repeated_reconciliation_issues(self) -> None:
        triggered, reason = _monitor_auto_stop_decision(
            auto_stop_enabled=True,
            auto_stop_consecutive_errors=3,
            daily_loss_stop=False,
            reconciliation_stop=True,
            consecutive_problem_cycles=3,
        )

        self.assertTrue(triggered)
        self.assertEqual(
            reason,
            "critical reconciliation issue after 3 problem cycle(s)",
        )

    def test_reconciliation_streak_counts_only_new_matching_observations(self) -> None:
        count, fingerprint, observation = _monitor_reconciliation_streak(
            current_count=0,
            previous_fingerprint="",
            previous_observation="",
            reconciliation_stop=True,
            reasons=["critical: coinbase"],
            observation=100.0,
        )
        self.assertEqual(count, 1)

        repeated = _monitor_reconciliation_streak(
            current_count=count,
            previous_fingerprint=fingerprint,
            previous_observation=observation,
            reconciliation_stop=True,
            reasons=["critical: coinbase"],
            observation=100.0,
        )
        self.assertEqual(repeated[0], 1)

        advanced = _monitor_reconciliation_streak(
            current_count=repeated[0],
            previous_fingerprint=repeated[1],
            previous_observation=repeated[2],
            reconciliation_stop=True,
            reasons=["critical: coinbase"],
            observation=105.0,
        )
        self.assertEqual(advanced[0], 2)

        changed = _monitor_reconciliation_streak(
            current_count=advanced[0],
            previous_fingerprint=advanced[1],
            previous_observation=advanced[2],
            reconciliation_stop=True,
            reasons=["different critical condition"],
            observation=110.0,
        )
        self.assertEqual(changed[0], 1)

        cleared = _monitor_reconciliation_streak(
            current_count=changed[0],
            previous_fingerprint=changed[1],
            previous_observation=changed[2],
            reconciliation_stop=False,
            reasons=[],
            observation=115.0,
        )
        self.assertEqual(cleared, (0, "", ""))

    def test_reconciliation_warmup_active_after_process_start_or_resume(self) -> None:
        self.assertTrue(
            _monitor_reconciliation_warmup_active(
                process_uptime_seconds=2.0,
                program_age_seconds=120.0,
                warmup_seconds=15.0,
            )
        )
        self.assertTrue(
            _monitor_reconciliation_warmup_active(
                process_uptime_seconds=120.0,
                program_age_seconds=2.0,
                warmup_seconds=15.0,
            )
        )
        self.assertFalse(
            _monitor_reconciliation_warmup_active(
                process_uptime_seconds=20.0,
                program_age_seconds=20.0,
                warmup_seconds=15.0,
            )
        )
        self.assertFalse(
            _monitor_reconciliation_warmup_active(
                process_uptime_seconds=0.0,
                program_age_seconds=0.0,
                warmup_seconds=0.0,
            )
        )
