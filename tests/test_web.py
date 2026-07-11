from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from arbitrage_bot.config import (
    AlertConfig,
    AssetPosition,
    BacktestConfig,
    BotConfig,
    CashAndCarryPair,
    DcaConfig,
    ExchangeConfig,
    ExecutionAlgoConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    OptionComboConfig,
    OptionsArbitrageConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
    SpotMarketConfig,
    StrategyCenterConfig,
    StrategyTimelineConfig,
    TradeLogConfig,
    WebSecurityConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.pnl import build_portfolio_pnl
from arbitrage_bot.trade_log import (
    _read_recent_event_lines,
    normalize_trade_event,
    write_trade_event,
)
from arbitrage_bot.web import (
    APP_JS,
    HTML as INDEX_HTML,
    LOGIN_MAX_FAILURES,
    LoginRateLimiter,
    MonitorState,
    SECURITY_HEADERS,
    STYLES_CSS,
    _add_security_headers,
    _cookie_secret,
    _filter_state_payload_for_user,
    _market_maker_force_replace_reason,
    _monitor_auto_stop_decision,
    _monitor_reconciliation_warmup_active,
    _market_maker_order_sync_delta,
    _market_maker_overrides_from_payload,
    _cash_and_carry_pairs_from_payload,
    _backtest_overrides_from_payload,
    _dca_overrides_from_payload,
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
    _session_identity,
    _session_valid,
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
    cancel_bulk_orders_payload,
    cancel_order_payload,
    create_app,
    default_web_user_store_path,
    default_strategy_center_path,
    enrich_recent_trades_with_pnl,
    fetch_account_balances_payload,
    fetch_derivatives_risk_payload,
    fetch_funding_basis_payload,
    fetch_options_arbitrage_payload,
    fetch_order_activity_payload,
    read_recent_web_audit_events,
    slow_execution_accounts,
    write_web_audit_event,
)
from arbitrage_bot.web.render_payloads import state_payload_for_view
from arbitrage_bot.web.routes import register_routes
from arbitrage_bot.web.state import MonitorState as SplitMonitorState
from arbitrage_bot.web.users import WebUserStore, totp_code
from arbitrage_bot.web_config import (
    market_maker_config_from_payload,
    market_maker_configs_from_payload,
    strategy_universe_to_dict,
)
from arbitrage_bot.strategy_timeline import write_strategy_timeline_from_payload
from arbitrage_bot.user_strategies import UserStrategy
from arbitrage_bot.user_workspace import UserExchangeAccount, UserProject


HTML = f"{INDEX_HTML}\n{APP_JS}"


def make_config(
    *,
    market_maker: MarketMakerConfig | None = None,
    market_makers: list[MarketMakerConfig] | None = None,
    slow_execution: SlowExecutionConfig | None = None,
    spot_grid: SpotGridConfig | None = None,
    dca: DcaConfig | None = None,
    execution_algo: ExecutionAlgoConfig | None = None,
    backtest: BacktestConfig | None = None,
    options_arbitrage: OptionsArbitrageConfig | None = None,
    portfolio: PortfolioConfig | None = None,
    spot_markets: list[SpotMarketConfig] | None = None,
    spot_exchanges: list[ExchangeConfig] | None = None,
    cash_and_carry_pairs: list[CashAndCarryPair] | None = None,
    derivative_exchanges: list[ExchangeConfig] | None = None,
    option_combos: list[OptionComboConfig] | None = None,
    risk: RiskConfig | None = None,
    trade_log: TradeLogConfig | None = None,
    strategy_center: StrategyCenterConfig | None = None,
    strategy_timeline: StrategyTimelineConfig | None = None,
    alerts: AlertConfig | None = None,
    web_security: WebSecurityConfig | None = None,
    quote_rates: dict[str, float] | None = None,
) -> BotConfig:
    return BotConfig(
        poll_seconds=1.0,
        order_book_depth=20,
        notional_quote=200.0,
        min_profit_quote=0.1,
        min_profit_bps=1.0,
        min_basis_bps=15.0,
        common_quote_currency="USD",
        quote_rates=quote_rates or {"USD": 1.0},
        quote_rate_sources=[],
        onchain_monitor=OnchainMonitorConfig(),
        market_maker=market_maker or MarketMakerConfig(),
        market_makers=market_makers or [],
        slow_execution=slow_execution or SlowExecutionConfig(),
        spot_grid=spot_grid or SpotGridConfig(),
        dca=dca or DcaConfig(),
        execution_algo=execution_algo or ExecutionAlgoConfig(),
        backtest=backtest or BacktestConfig(),
        options_arbitrage=options_arbitrage or OptionsArbitrageConfig(),
        strategy_center=strategy_center or StrategyCenterConfig(),
        portfolio=portfolio or PortfolioConfig(),
        spot_symbols=[],
        spot_markets=spot_markets or [],
        cash_and_carry_pairs=cash_and_carry_pairs or [],
        option_combos=option_combos or [],
        spot_exchanges=spot_exchanges or [],
        derivative_exchanges=derivative_exchanges or [],
        risk=risk or RiskConfig(),
        trade_log=trade_log or TradeLogConfig(enabled=False),
        strategy_timeline=strategy_timeline or StrategyTimelineConfig(enabled=False),
        alerts=alerts or AlertConfig(),
        web_security=web_security or WebSecurityConfig(),
    )


class WebMonitorTest(unittest.TestCase):
    def test_page_uses_auto_buy_sell_label(self) -> None:
        self.assertIn(
            '<script src="/static/app.js?v=20260711-pages4" defer></script>',
            INDEX_HTML,
        )
        self.assertIn(
            '<script src="/static/i18n.js?v=20260711-pages4" defer></script>',
            INDEX_HTML,
        )
        self.assertIn(
            'id="user-workspace-notice" class="subtle" role="status"',
            INDEX_HTML,
        )
        self.assertIn('id="user-setup-readiness"', INDEX_HTML)
        self.assertIn('id="user-exchange-test"', INDEX_HTML)
        self.assertIn('id="backtest-section"', INDEX_HTML)
        self.assertIn('id="backtest-run"', INDEX_HTML)
        self.assertIn("Uses public historical candles", INDEX_HTML)
        self.assertNotIn(
            'data-ui-feature="backtest" data-ui-hidden-default="true"',
            INDEX_HTML,
        )

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

    def test_page_includes_paper_only_user_strategy_controls(self) -> None:
        self.assertIn('id="user-strategy-form"', INDEX_HTML)
        self.assertIn('id="user-strategy-accounts"', INDEX_HTML)
        self.assertIn('id="user-strategy-risk-order"', INDEX_HTML)
        self.assertIn('id="user-strategy-risk-total"', INDEX_HTML)
        self.assertIn('id="user-strategy-risk-fee"', INDEX_HTML)
        self.assertIn('id="user-paper-events"', INDEX_HTML)
        self.assertIn('id="user-strategies"', INDEX_HTML)
        strategy_form = INDEX_HTML.split('id="user-strategy-form"', 1)[1].split(
            "</form>",
            1,
        )[0]
        self.assertNotIn("live_enabled", strategy_form)
        self.assertNotIn("Live Ready", strategy_form)
        self.assertIn("Paper simulation only", strategy_form)

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
                ExchangeConfig(id="coinbase", label="coinbase-spot", market_type="spot"),
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
            '<link rel="stylesheet" href="/static/styles.css?v=20260711-pages4">',
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
        quant_overview = state_payload_for_view(payload, "quant", sections="backtest-points")
        quant_derivatives = state_payload_for_view(
            payload,
            "quant",
            sections="derivatives-risk,funding-basis,contract-strategies,options-arbitrage",
        )
        settings = state_payload_for_view(payload, "settings", sections="risk-form")
        records = state_payload_for_view(payload, "records", sections="console-strategies")
        records_open_orders = state_payload_for_view(
            payload,
            "records",
            sections="console-open-orders",
        )

        self.assertEqual(status_overview["markets"], [])
        self.assertEqual(status_overview["quote_rates"], {})
        self.assertEqual(status_overview["readiness"], {})
        self.assertIn("totals", status_overview["account_balances"])
        self.assertNotIn("accounts", status_overview["account_balances"])
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
        self.assertIn('data-ui-feature="readiness" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="scan_status" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="orders_detail" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="strategy_timeline" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="audit_trail" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="quote_rates" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="onchain_monitor" data-ui-hidden-default="true"', HTML)
        self.assertIn('data-ui-feature="onchain_history" data-ui-hidden-default="true"', HTML)
        self.assertIn("const HIDDEN_UI_FEATURES = new Set", APP_JS)
        self.assertIn("function applyFeatureVisibility", APP_JS)
        self.assertIn('status: [', APP_JS)
        self.assertIn('trading: [', APP_JS)
        self.assertIn('quant: [', APP_JS)
        self.assertIn('.ui-feature-hidden', STYLES_CSS)
        self.assertIn('[data-page].ui-feature-hidden', STYLES_CSS)
        self.assertIn('.statusbar[data-page].ui-feature-hidden', STYLES_CSS)

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
        self.assertIn('if (res.status === 401)', HTML)
        self.assertIn('setHeaderStatus("degraded", "Retrying")', HTML)
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
        self.assertIn('id="portfolio-position-detail"', HTML)
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
        self.assertIn('aria-expanded', HTML)
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
        self.assertIn('id="mm-quality-inventory"', HTML)
        self.assertIn('id="mm-quality-fills"', HTML)
        self.assertIn('id="mm-quality-spread"', HTML)

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
        self.assertIn("Account / Project / Exchange / Pair", HTML)
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
        self.assertEqual(payload["summary"]["action_count"], len(payload["next_actions"]))

    def test_readiness_payload_reports_checking_before_health_cache_is_ready(self) -> None:
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
        self.assertEqual(_session_identity(cfg, user_token), (True, "trader@example.com"))
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

        self.assertEqual(default_strategy_center_path(cfg), "data/strategy_center.sqlite3")

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

    def test_state_payload_filters_to_user_asset_scope(self) -> None:
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
                        {"id": "acs-task", "status": "running", "config": {"symbol": "ACS/USDC"}},
                        {"id": "btc-task", "status": "running", "config": {"symbol": "BTC/USDT"}},
                    ]
                },
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
        }

        filtered = _filter_state_payload_for_user(payload, cfg=cfg, user=user)

        self.assertEqual([row["asset"] for row in filtered["markets"]], ["ACS"])
        self.assertEqual(
            [row["asset"] for row in filtered["config"]["spot_markets"]],
            ["ACS"],
        )
        self.assertEqual(
            [row["asset"] for row in filtered["portfolio"]["positions"]],
            ["ACS"],
        )
        self.assertEqual(len(filtered["opportunities"]), 1)
        self.assertEqual(filtered["market_maker"]["status"], "out_of_scope")
        self.assertEqual(
            [task["id"] for task in filtered["slow_execution"]["tasks"]["tasks"]],
            ["acs-task"],
        )
        self.assertEqual(
            [row["id"] for row in filtered["order_activity"]["open_orders"]],
            ["acs-order"],
        )
        self.assertEqual(
            [row["id"] for row in filtered["order_activity"]["recent_trades"]],
            ["acs-fill"],
        )
        self.assertEqual(
            [
                row["currency"]
                for row in filtered["account_balances"]["accounts"][0]["balance"]["currencies"]
            ],
            ["ACS", "USDC"],
        )
        self.assertEqual(
            [row["id"] for row in filtered["trading_console"]["strategies"]],
            ["slow_execution", "spot_spread"],
        )
        self.assertEqual(filtered["trading_console"]["strategies"][1]["symbol"], "ACS")
        self.assertEqual(filtered["auth"]["mode"], "user")
        self.assertEqual(filtered["auth"]["email"], "trader@example.com")
        self.assertEqual(filtered["auth"]["asset_scope"], ["ACS"])

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

    def test_order_reconciliation_auto_stop_for_activity_errors_only(self) -> None:
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
        self.assertEqual(payload["critical_issue_count"], 1)
        self.assertTrue(payload["auto_stop_recommended"])
        self.assertIn("order_activity_error", payload["auto_stop_reasons"][0])

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

    def test_order_reconciliation_does_not_auto_stop_for_unmanaged_attributed_orders(self) -> None:
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


class LoginRateLimiterTest(unittest.TestCase):
    def test_locks_out_after_max_failures_and_recovers(self) -> None:
        limiter = LoginRateLimiter(
            max_failures=3,
            window_seconds=100.0,
            lockout_seconds=60.0,
        )
        key = "203.0.113.7"

        self.assertEqual(limiter.retry_after(key, now=0.0), 0.0)
        self.assertEqual(limiter.register_failure(key, now=1.0), 0.0)
        self.assertEqual(limiter.register_failure(key, now=2.0), 0.0)
        # Third failure crosses the threshold and triggers the lockout.
        self.assertEqual(limiter.register_failure(key, now=3.0), 60.0)
        self.assertEqual(limiter.retry_after(key, now=3.0), 60.0)
        self.assertAlmostEqual(limiter.retry_after(key, now=33.0), 30.0)
        # After the lockout window expires the client may try again.
        self.assertEqual(limiter.retry_after(key, now=64.0), 0.0)

    def test_success_clears_failure_history(self) -> None:
        limiter = LoginRateLimiter(max_failures=3, window_seconds=100.0)
        key = "203.0.113.8"
        limiter.register_failure(key, now=1.0)
        limiter.register_failure(key, now=2.0)
        limiter.register_success(key)
        # History reset, so two more failures must not lock the client out.
        self.assertEqual(limiter.register_failure(key, now=3.0), 0.0)
        self.assertEqual(limiter.register_failure(key, now=4.0), 0.0)
        self.assertEqual(limiter.retry_after(key, now=4.0), 0.0)

    def test_old_failures_outside_window_are_forgotten(self) -> None:
        limiter = LoginRateLimiter(max_failures=3, window_seconds=100.0)
        key = "203.0.113.9"
        limiter.register_failure(key, now=1.0)
        limiter.register_failure(key, now=2.0)
        # This failure is far outside the window; the earlier two have aged out.
        self.assertEqual(limiter.register_failure(key, now=500.0), 0.0)
        self.assertEqual(limiter.retry_after(key, now=500.0), 0.0)

    def test_clients_are_tracked_independently(self) -> None:
        limiter = LoginRateLimiter(
            max_failures=2, window_seconds=100.0, lockout_seconds=60.0
        )
        limiter.register_failure("10.0.0.1", now=1.0)
        self.assertEqual(limiter.register_failure("10.0.0.1", now=2.0), 60.0)
        # A different client IP is unaffected by the first one's lockout.
        self.assertEqual(limiter.retry_after("10.0.0.2", now=2.0), 0.0)
        self.assertEqual(limiter.register_failure("10.0.0.2", now=2.0), 0.0)


class CookieSecretTest(unittest.TestCase):
    def test_unconfigured_secret_is_random_not_a_known_constant(self) -> None:
        cfg = make_config(
            web_security=WebSecurityConfig(
                password_env=None,
                cookie_secret_env=None,
            )
        )
        secret = _cookie_secret(cfg)
        self.assertTrue(secret)
        self.assertNotEqual(secret, "crypto-arbitrage-dev")
        # Stable within the process so existing sessions stay valid.
        self.assertEqual(secret, _cookie_secret(cfg))

    def test_explicit_cookie_secret_env_takes_precedence(self) -> None:
        cfg = make_config(
            web_security=WebSecurityConfig(
                password_env="WEB_PW_TEST",
                cookie_secret_env="COOKIE_SECRET_TEST",
            )
        )
        with patch.dict(
            os.environ,
            {"COOKIE_SECRET_TEST": "configured-secret", "WEB_PW_TEST": "pw"},
        ):
            self.assertEqual(_cookie_secret(cfg), "configured-secret")


class WebMonitorStateTest(unittest.IsolatedAsyncioTestCase):
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
                        bad = await client.post(
                            "/login", data={"password": "wrong"}
                        )
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

    async def test_metrics_endpoint_allows_local_scrape_without_dashboard_session(self) -> None:
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

    async def test_fetch_order_activity_payload_treats_unused_accounts_as_idle(self) -> None:
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

    async def test_fetch_account_balances_warns_when_used_account_missing_api_env(self) -> None:
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

    async def test_fetch_derivatives_risk_payload_flags_leverage_and_liquidation(self) -> None:
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
        self.assertTrue(any("leverage" in reason for reason in position["risk_reasons"]))
        self.assertTrue(
            any("liquidation buffer" in reason for reason in position["risk_reasons"])
        )

    async def test_fetch_funding_basis_payload_uses_strategy_center_settings(self) -> None:
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

    async def test_fetch_options_arbitrage_payload_blocks_wide_option_spread(self) -> None:
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
        self.assertFalse(
            row["paper_execution"]["protection"]["live_submit_allowed"]
        )

    async def test_fetch_options_arbitrage_payload_finds_box_spread_candidate(self) -> None:
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
                            "exchange": "coinbase-spot",
                            "symbol": "ACS/USDC",
                            "side": "buy",
                            "total_quote": 10.0,
                            "price_mode": "taker",
                        },
                        "filled_quote": 1.5,
                        "remaining_quote": 8.5,
                        "progress_pct": 15.0,
                        "open_order_count": 1,
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
        self.assertEqual(view_task["config"]["exchange"], "coinbase-spot")
        self.assertEqual(view_task["config"]["price_mode"], "taker")
        self.assertIn("total_quote", view_task["config"])

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
        accounts = {
            row["key"]: row for row in update["market_maker"]["accounts"]
        }

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
        mm_accounts = {
            row["key"]: row for row in payload["market_maker"]["accounts"]
        }
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
                        {"action": "update_user", "email": member.email, "role": "admin"},
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
            admin = store.create_user(email="admin@example.com", password="Strong-pass-1!")
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
            admin = store.create_user(email="admin@example.com", password="Strong-pass-1!")
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
            admin = store.create_user(email="admin@example.com", password="Strong-pass-1!")
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
            row for row in create_payload["users"] if row["email"] == "trader@example.com"
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
            row for row in asset_payload["users"] if row["email"] == "trader@example.com"
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
                    response = await client.get(
                        f"/api/user-backtests?run_id={run_id}"
                    )
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

    async def test_user_workspace_project_approval_and_encrypted_account_flow(self) -> None:
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
                    approve_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "approve_project",
                            "project_id": project["id"],
                        },
                    )
                    approve_payload = await approve_response.json()
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
                    unregistered_owner_payload = await unregistered_owner_response.json()

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
                                "enabled": True,
                            },
                        },
                    )
                    strategy_payload = await strategy_response.json()
                    strategy = strategy_payload["workspace"]["strategies"][0]
                    stored_strategy = app["user_workspace_store"].get_strategy(
                        strategy["id"]
                    )
                    app["user_paper_store"].persist_cycle(
                        stored_strategy,
                        {
                            "run_id": "paper-api-test",
                            "status": "running",
                            "reason": "paper test state",
                            "fill_count": 3,
                            "open_order_count": 2,
                            "total_pnl_common": 1.25,
                            "daily_pnl_common": 0.25,
                            "common_quote_currency": "USD",
                        },
                    )
                    paper_state_payload = await (
                        await client.get("/api/state?view=settings")
                    ).json()
                    paper_reset_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "reset_strategy_paper",
                            "strategy_id": strategy["id"],
                        },
                    )
                    paper_reset_payload = await paper_reset_response.json()
                    live_strategy_response = await client.post(
                        "/api/user-workspace",
                        json={
                            "action": "upsert_strategy",
                            "strategy": {
                                "id": strategy["id"],
                                "live_enabled": True,
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
                finally:
                    await client.close()

            persisted_member = user_store.get_user(member.email)
            database_bytes = workspace_path.read_bytes()
            paper_database_bytes = workspace_path.with_name(
                "user_paper_trading.sqlite3"
            ).read_bytes()

        self.assertEqual(project_response.status, 200, project_payload)
        self.assertEqual(project["status"], "pending")
        self.assertEqual(discovery_response.status, 200, discovery_payload)
        self.assertEqual(discovery_payload["markets"][0]["symbol"], "ACS/USDC")
        self.assertEqual(account_response.status, 200, account_payload)
        self.assertTrue(account["credentials"]["configured"])
        self.assertEqual(account["connection_status"], "unverified")
        self.assertEqual(account["symbol"], "ACS/USDC")
        self.assertNotIn("api_key", account)
        self.assertNotIn("secret", account)
        self.assertEqual(premature_enable_response.status, 403)
        self.assertEqual(member_approve_response.status, 403)
        self.assertEqual(approve_response.status, 200, approve_payload)
        self.assertEqual(approve_payload["workspace"]["projects"][0]["status"], "active")
        self.assertEqual(unregistered_owner_response.status, 400, unregistered_owner_payload)
        self.assertIn("not a registered user", unregistered_owner_payload["error"])
        self.assertIn("ACS", persisted_member.allowed_assets)
        self.assertEqual(untested_enable_response.status, 400, untested_enable_payload)
        self.assertIn("connection test", untested_enable_payload["error"])
        self.assertEqual(connection_test_response.status, 200, connection_test_payload)
        self.assertEqual(
            connection_test_payload["connection_test"]["status"],
            "healthy",
        )
        self.assertEqual(enable_response.status, 200, enable_payload)
        self.assertTrue(enable_payload["workspace"]["accounts"][0]["enabled"])
        self.assertTrue(
            enable_payload["workspace"]["accounts"][0]["connection_fresh"]
        )
        self.assertEqual(strategy_response.status, 200, strategy_payload)
        self.assertEqual(strategy["status"], "paper_ready")
        self.assertTrue(strategy["effective_enabled"])
        self.assertFalse(strategy["readiness"]["live_submit_allowed"])
        paper_strategy = paper_state_payload["user_workspace"]["strategies"][0]
        self.assertEqual(paper_strategy["paper_runtime"]["status"], "running")
        self.assertEqual(paper_strategy["paper_runtime"]["fill_count"], 3)
        self.assertEqual(paper_strategy["paper_counts"]["state_count"], 1)
        self.assertEqual(paper_reset_response.status, 200, paper_reset_payload)
        self.assertEqual(paper_reset_payload["paper_reset"]["state_count"], 1)
        self.assertEqual(
            paper_reset_payload["workspace"]["strategies"][0]["paper_runtime"][
                "status"
            ],
            "not_started",
        )
        self.assertEqual(live_strategy_response.status, 400, live_strategy_payload)
        self.assertIn("paper-only", live_strategy_payload["error"])
        self.assertEqual(
            account_delete_blocked_response.status,
            400,
            account_delete_blocked_payload,
        )
        self.assertIn("strategies using this account", account_delete_blocked_payload["error"])
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
        self.assertFalse(pause_strategy_payload["workspace"]["strategies"][0]["enabled"])
        self.assertEqual(resume_strategy_response.status, 200, resume_strategy_payload)
        self.assertTrue(resume_strategy_payload["workspace"]["strategies"][0]["enabled"])
        self.assertEqual(exchange_change_response.status, 400, exchange_change_payload)
        self.assertIn("re-enter API key", exchange_change_payload["error"])
        self.assertEqual(scope_change_response.status, 200, scope_change_payload)
        self.assertEqual(scope_change_payload["workspace"]["projects"][0]["status"], "pending")
        self.assertFalse(scope_change_payload["workspace"]["accounts"][0]["enabled"])
        self.assertFalse(scope_change_payload["workspace"]["strategies"][0]["enabled"])
        self.assertEqual(settings_state["user_workspace"]["summary"]["project_count"], 1)
        self.assertNotIn(b"test-api-key-value", database_bytes)
        self.assertNotIn(b"test-secret-value", database_bytes)
        self.assertNotIn(b"test-api-key-value", paper_database_bytes)
        self.assertNotIn(b"test-secret-value", paper_database_bytes)

class WebPerformanceAndStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_api_state_is_gzip_compressed_for_gzip_clients(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/state")
            payload = await response.json()

            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
            self.assertIn("status", payload)
        finally:
            await client.close()

    async def test_static_assets_get_immutable_cache_control_and_gzip(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/static/app.js")

            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get("Cache-Control"),
                "public, max-age=31536000, immutable",
            )
            self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        finally:
            await client.close()

    async def test_favicon_is_served_even_without_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cfg = make_config(
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
                    ico = await client.get("/favicon.ico", allow_redirects=False)
                    svg = await client.get(
                        "/static/favicon.svg", allow_redirects=False
                    )
                    page = await client.get("/", allow_redirects=False)

                    self.assertEqual(ico.status, 200)
                    self.assertEqual(ico.headers.get("Content-Type"), "image/svg+xml")
                    self.assertEqual(svg.status, 200)
                    # The dashboard itself still requires a session.
                    self.assertEqual(page.status, 302)
                finally:
                    await client.close()

    async def test_state_stream_pushes_snapshots_matching_state_payload(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            state_response = await client.get("/api/state?view=status")
            state_payload = await state_response.json()

            stream_response = await client.get(
                "/api/state/stream?view=status&interval=1"
            )
            self.assertEqual(stream_response.status, 200)
            self.assertEqual(
                stream_response.headers.get("Content-Type"),
                "text/event-stream",
            )
            event = await asyncio.wait_for(
                stream_response.content.readuntil(b"\n\n"),
                timeout=10,
            )
            stream_response.close()

            self.assertTrue(event.startswith(b"data: "))
            streamed_payload = json.loads(event[len(b"data: "):].decode("utf-8"))
            self.assertEqual(
                sorted(streamed_payload.keys()),
                sorted(state_payload.keys()),
            )
            self.assertIn("status", streamed_payload)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
