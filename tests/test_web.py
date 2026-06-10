from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from arbitrage_bot.config import (
    AlertConfig,
    AssetPosition,
    BotConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
    TradeLogConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.pnl import build_portfolio_pnl
from arbitrage_bot.trade_log import normalize_trade_event
from arbitrage_bot.web import (
    HTML,
    MonitorState,
    _market_maker_overrides_from_payload,
    _risk_overrides_from_payload,
    _slow_execution_overrides_from_payload,
    _spot_markets_from_payload,
    _daily_report_due,
    _ip_allowed,
    _make_session_token,
    _session_valid,
    build_daily_report_message,
    build_order_attribution_map,
    build_market_maker_payload,
    build_market_rows,
    build_operations_payload,
    build_slow_execution_payload,
    build_synced_portfolio_pnl,
    build_trading_console_payload,
    cancel_bulk_orders_payload,
    cancel_order_payload,
    enrich_recent_trades_with_pnl,
    fetch_account_balances_payload,
    fetch_order_activity_payload,
    slow_execution_accounts,
)


def make_config(
    *,
    market_maker: MarketMakerConfig | None = None,
    slow_execution: SlowExecutionConfig | None = None,
    portfolio: PortfolioConfig | None = None,
    spot_markets: list[SpotMarketConfig] | None = None,
    spot_exchanges: list[ExchangeConfig] | None = None,
    risk: RiskConfig | None = None,
    trade_log: TradeLogConfig | None = None,
    alerts: AlertConfig | None = None,
) -> BotConfig:
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
        market_maker=market_maker or MarketMakerConfig(),
        slow_execution=slow_execution or SlowExecutionConfig(),
        portfolio=portfolio or PortfolioConfig(),
        spot_symbols=[],
        spot_markets=spot_markets or [],
        cash_and_carry_pairs=[],
        spot_exchanges=spot_exchanges or [],
        derivative_exchanges=[],
        risk=risk or RiskConfig(),
        trade_log=trade_log or TradeLogConfig(enabled=False),
        alerts=alerts or AlertConfig(),
    )


class WebMonitorTest(unittest.TestCase):
    def test_page_uses_auto_buy_sell_label(self) -> None:
        self.assertIn("Auto Buy/Sell", HTML)
        self.assertIn("/api/auto-buy-sell", HTML)
        self.assertIn("/api/auto-buy-sell/tasks", HTML)
        self.assertIn('id="slow-create-task"', HTML)
        self.assertIn('id="slow-tasks"', HTML)
        self.assertNotIn("Slow Execution", HTML)

    def test_page_uses_generic_dashboard_title(self) -> None:
        self.assertIn("Crypto Trading Dashboard", HTML)
        self.assertIn("Multi-asset arbitrage", HTML)
        self.assertNotIn("ACS Arbitrage Monitor", HTML)

    def test_page_includes_market_config_controls(self) -> None:
        self.assertIn("Markets", HTML)
        self.assertIn("/api/markets", HTML)
        self.assertIn('id="markets-form"', HTML)
        self.assertIn('id="market-symbol"', HTML)
        self.assertIn('id="markets-config"', HTML)

    def test_page_includes_account_balances(self) -> None:
        self.assertIn("Account Balances", HTML)
        self.assertIn('id="account-balances"', HTML)

    def test_page_includes_orders_and_fills(self) -> None:
        self.assertIn("Orders & Fills", HTML)
        self.assertIn("/api/orders/cancel", HTML)
        self.assertIn('id="open-orders"', HTML)
        self.assertIn('id="recent-fills"', HTML)

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

    def test_page_includes_risk_controls(self) -> None:
        self.assertIn("Risk Controls", HTML)
        self.assertIn("/api/risk", HTML)
        self.assertIn('id="risk-allow-live"', HTML)
        self.assertIn('id="risk-accounts"', HTML)
        self.assertIn('id="risk-strategies"', HTML)
        self.assertIn('id="risk-max-order"', HTML)
        self.assertIn('id="risk-max-exposure"', HTML)

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
            spot_exchanges=[
                ExchangeConfig(id="bybit", label="bybit-spot"),
                ExchangeConfig(id="coinbase", label="coinbase-spot"),
            ],
            risk=RiskConfig(
                allow_live_trading=True,
                allow_market_maker=True,
                allow_slow_execution=True,
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
                stop_price=0.001,
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

        self.assertEqual(
            payload["accounts"],
            [
                {
                    "key": "bybit-spot",
                    "label": "bybit-spot",
                    "id": "bybit",
                    "market_type": "spot",
                    "symbol": "ACS/USDT",
                    "symbols": ["ACS/USDT"],
                },
                {
                    "key": "coinbase-spot",
                    "label": "coinbase-spot",
                    "id": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDC",
                    "symbols": ["ACS/USDC"],
                },
            ],
        )

    def test_slow_execution_accounts_uses_key_fallback(self) -> None:
        accounts = slow_execution_accounts([ExchangeConfig(id="bybit")])

        self.assertEqual(accounts[0]["key"], "bybit:spot")
        self.assertEqual(accounts[0]["label"], "bybit:spot")
        self.assertEqual(accounts[0]["symbol"], "")
        self.assertEqual(accounts[0]["symbols"], [])

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
                "stop_price": "0.01",
            },
            allowed_exchanges={"bybit-spot"},
            symbols_by_exchange={"bybit-spot": ["ACS/USDT"]},
        )

        self.assertTrue(overrides["enabled"])
        self.assertEqual(overrides["exchange"], "bybit-spot")
        self.assertEqual(overrides["symbol"], "ACS/USDT")
        self.assertEqual(overrides["side"], "buy")
        self.assertEqual(overrides["total_quote"], 5.0)
        self.assertEqual(overrides["slice_base"], 0.0)
        self.assertEqual(overrides["slice_quote"], 0.0)
        self.assertEqual(overrides["slice_base_min"], 10.0)
        self.assertEqual(overrides["slice_base_max"], 20.0)
        self.assertTrue(overrides["randomize_slice"])

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
                "poll_seconds": "1",
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
        self.assertEqual(overrides["poll_seconds"], 1.0)
        self.assertTrue(overrides["post_only"])

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
                "max_order_quote": "5.5",
                "max_exposure_quote": "250",
                "max_daily_loss_quote": "10",
                "max_open_orders": "12",
                "max_cancels_per_cycle": "4",
                "min_seconds_between_cancels": "1.5",
                "max_order_book_age_seconds": "60",
            },
            allowed_accounts={"coinbase-spot", "bybit-spot"},
            allowed_strategies={"market_maker", "slow_execution"},
        )

        self.assertTrue(overrides["allow_live_trading"])
        self.assertFalse(overrides["account_enabled"]["bybit-spot"])
        self.assertFalse(overrides["strategy_enabled"]["slow_execution"])
        self.assertEqual(overrides["max_order_quote"], 5.5)
        self.assertEqual(overrides["max_exposure_quote"], 250.0)
        self.assertEqual(overrides["max_daily_loss_quote"], 10.0)
        self.assertEqual(overrides["max_open_orders"], 12)
        self.assertEqual(overrides["max_cancels_per_cycle"], 4)
        self.assertEqual(overrides["min_seconds_between_cancels"], 1.5)
        self.assertEqual(overrides["max_order_book_age_seconds"], 60.0)

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

        self.assertTrue(_session_valid(cfg, token))
        self.assertFalse(_session_valid(cfg, token + "bad"))
        self.assertTrue(_ip_allowed("66.96.212.97", ["66.96.212.97"]))
        self.assertTrue(_ip_allowed("66.96.212.97", ["66.96.212.0/24"]))
        self.assertFalse(_ip_allowed("66.96.213.1", ["66.96.212.0/24"]))

    def test_daily_report_due_and_message(self) -> None:
        cfg = make_config(
            alerts=AlertConfig(
                daily_report_enabled=True,
                daily_report_time="00:00",
            )
        )

        due, day = _daily_report_due(
            cfg,
            last_report_day=None,
            now=1_704_153_599,
        )
        not_due, _ = _daily_report_due(
            cfg,
            last_report_day=day,
            now=1_704_153_600,
        )
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
        payload = build_operations_payload(make_config())

        self.assertIn("risk", payload)
        self.assertIn("trade_log", payload)
        self.assertIn("alerts", payload)
        self.assertFalse(payload["risk"]["allow_live_trading"])
        self.assertEqual(payload["trade_log"]["recent_events"], [])
        self.assertEqual(payload["trade_log"]["recent_entries"], [])
        self.assertEqual(payload["trade_log"]["summary"]["event_count"], 0)

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


class WebMonitorStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_order_activity_payload_summarizes_orders_and_fills(self) -> None:
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
        self.assertEqual(payload["recent_trades"][0]["order_id"], "order-closed-1")
        self.assertEqual(payload["recent_trades"][0]["fee"]["currency"], "USDC")

    async def test_cancel_order_payload_validates_and_cancels_configured_symbol(self) -> None:
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

    async def test_fetch_account_balances_skips_missing_api_env(self) -> None:
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

        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["checked_account_count"], 0)
        self.assertEqual(manager.calls, 0)
        self.assertEqual(
            payload["accounts"][0]["balance"]["skipped_reason"],
            "api env vars missing",
        )

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

if __name__ == "__main__":
    unittest.main()
