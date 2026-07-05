from pathlib import Path
import unittest
from unittest.mock import patch

from arbitrage_bot.config import load_config


class ConfigTest(unittest.TestCase):
    def test_acs_onchain_monitor_uses_top_20_and_labels(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config.acs.example.json"

        cfg = load_config(config_path)

        self.assertEqual(cfg.onchain_monitor.top_n, 20)
        self.assertEqual(cfg.onchain_monitor.rpc_url_env, "SOLANA_RPC_URLS")
        self.assertEqual(
            cfg.onchain_monitor.rpc_urls,
            [
                "https://solana-rpc.publicnode.com",
                "https://api.mainnet-beta.solana.com",
            ],
        )
        self.assertEqual(
            cfg.onchain_monitor.history_path,
            "data/onchain_holder_changes.json",
        )
        self.assertEqual(
            cfg.onchain_monitor.address_labels[
                "8Mm46CsqxiyAputDUp2cXHg41HE3BfynTeMBDwzrMZQH"
            ],
            "Bithumb Hot Wallet 1",
        )
        self.assertEqual(
            cfg.onchain_monitor.address_labels[
                "9obNtb5GyUegcs3a1CbBkLuc5hEWynWfJC6gjz5uWQkE"
            ],
            "Coinbase Hot Wallet",
        )
        self.assertTrue(cfg.market_maker.enabled)
        self.assertFalse(cfg.market_maker.live_enabled)
        self.assertEqual(cfg.market_maker.exchange, "bybit-spot")
        self.assertEqual(cfg.market_maker.symbol, "ACS/USDT")
        self.assertEqual(cfg.market_maker.levels, 10)
        self.assertEqual(cfg.market_maker.price_band_pct, 10.0)
        self.assertEqual(cfg.market_maker.quote_per_level, 1.0)
        self.assertEqual(cfg.market_maker.poll_seconds, 1.0)
        self.assertEqual(cfg.market_maker.max_order_quote, 0.0)
        self.assertEqual(cfg.market_maker.max_cycle_quote, 0.0)
        self.assertEqual(cfg.market_maker.max_open_orders, 0)
        self.assertEqual(cfg.market_maker.max_cancels_per_cycle, 0)
        self.assertEqual(cfg.market_maker.max_slippage_bps, 0.0)
        self.assertEqual(cfg.market_maker.max_order_book_age_seconds, 0.0)
        self.assertFalse(cfg.market_maker.inventory_control_enabled)
        self.assertEqual(cfg.market_maker.inventory_target_base, 0.0)
        self.assertEqual(cfg.market_maker.inventory_band_base, 0.0)
        self.assertEqual(cfg.market_maker.inventory_max_deviation_base, 0.0)
        self.assertFalse(cfg.slow_execution.enabled)
        self.assertEqual(cfg.slow_execution.exchange, "bybit-spot")
        self.assertEqual(cfg.slow_execution.symbol, "ACS/USDT")
        self.assertEqual(cfg.slow_execution.side, "sell")
        self.assertEqual(cfg.slow_execution.interval_seconds, 60.0)
        self.assertEqual(cfg.slow_execution.total_quote, 0.0)
        self.assertFalse(cfg.slow_execution.unlimited_total)
        self.assertEqual(cfg.slow_execution.slice_mode, "configured")
        self.assertEqual(cfg.slow_execution.slice_base_min, 0.0)
        self.assertEqual(cfg.slow_execution.slice_base_max, 0.0)
        self.assertFalse(cfg.slow_execution.randomize_slice)
        self.assertEqual(cfg.slow_execution.order_ttl_seconds, 0.0)
        self.assertEqual(cfg.slow_execution.start_price, 0.0)
        self.assertEqual(cfg.slow_execution.stop_price, 0.0)
        self.assertEqual(cfg.slow_execution.price_mode, "taker")
        self.assertEqual(cfg.slow_execution.price_offset_bps, 0.0)
        self.assertFalse(cfg.slow_execution.post_only)
        self.assertFalse(cfg.spot_grid.enabled)
        self.assertFalse(cfg.spot_grid.live_enabled)
        self.assertEqual(cfg.spot_grid.exchange, "bybit-spot")
        self.assertEqual(cfg.spot_grid.symbol, "ACS/USDT")
        self.assertEqual(cfg.spot_grid.grid_count, 10)
        self.assertEqual(cfg.spot_grid.spacing, "arithmetic")
        self.assertEqual(cfg.spot_grid.quote_per_grid, 1.0)
        self.assertEqual(cfg.spot_grid.max_open_orders, 20)
        self.assertEqual(cfg.spot_grid.min_grid_step_bps, 10.0)
        self.assertEqual(cfg.spot_grid.cancel_retry_attempts, 3)
        self.assertTrue(cfg.spot_grid.post_only)
        self.assertEqual(cfg.spot_grid.runtime_path, "data/spot_grid_runtime.json")
        self.assertFalse(cfg.dca.enabled)
        self.assertFalse(cfg.dca.live_enabled)
        self.assertEqual(cfg.dca.exchange, "bybit-spot")
        self.assertEqual(cfg.dca.symbol, "ACS/USDT")
        self.assertEqual(cfg.dca.side, "buy")
        self.assertEqual(cfg.dca.interval_seconds, 3600.0)
        self.assertEqual(cfg.dca.quote_per_order, 1.0)
        self.assertEqual(cfg.dca.size_multiplier, 1.0)
        self.assertEqual(cfg.dca.max_orders, 10)
        self.assertEqual(cfg.dca.price_mode, "taker")
        self.assertFalse(cfg.execution_algo.enabled)
        self.assertFalse(cfg.execution_algo.live_enabled)
        self.assertEqual(cfg.execution_algo.exchange, "bybit-spot")
        self.assertEqual(cfg.execution_algo.symbol, "ACS/USDT")
        self.assertEqual(cfg.execution_algo.side, "buy")
        self.assertEqual(cfg.execution_algo.algo, "twap")
        self.assertEqual(cfg.execution_algo.total_quote, 10.0)
        self.assertEqual(cfg.execution_algo.slice_count, 12)
        self.assertEqual(cfg.execution_algo.participation_rate, 0.05)
        self.assertEqual(cfg.execution_algo.price_mode, "taker")
        self.assertFalse(cfg.backtest.enabled)
        self.assertEqual(cfg.backtest.strategy, "spot_grid")
        self.assertEqual(cfg.backtest.exchange, "bybit-spot")
        self.assertEqual(cfg.backtest.symbol, "ACS/USDT")
        self.assertEqual(cfg.backtest.initial_cash, 1000.0)
        self.assertEqual(cfg.backtest.step_count, 200)
        self.assertEqual(cfg.backtest.data_source, "synthetic")
        self.assertFalse(cfg.backtest.depth_simulation_enabled)
        self.assertEqual(cfg.backtest.depth_quote_per_level, 0.0)
        self.assertEqual(cfg.backtest.depth_step_bps, 5.0)
        self.assertEqual(cfg.backtest.depth_levels, 5)
        self.assertEqual(cfg.backtest.latency_steps, 0)
        self.assertFalse(cfg.options_arbitrage.enabled)
        self.assertEqual(cfg.options_arbitrage.notional_quote, 200.0)
        self.assertEqual(cfg.options_arbitrage.min_edge_quote, 0.1)
        self.assertEqual(cfg.options_arbitrage.min_edge_bps, 10.0)
        self.assertEqual(cfg.options_arbitrage.min_option_depth_quote, 0.0)
        self.assertEqual(cfg.options_arbitrage.max_option_spread_bps, 0.0)
        self.assertEqual(cfg.options_arbitrage.min_days_to_expiry_open, 0.0)
        self.assertEqual(cfg.options_arbitrage.expiry_reminder_days, 0.0)
        self.assertTrue(cfg.contract_strategies.enabled)
        self.assertTrue(cfg.contract_strategies.funding_bot_enabled)
        self.assertTrue(cfg.contract_strategies.basis_bot_enabled)
        self.assertFalse(cfg.contract_strategies.futures_grid_enabled)
        self.assertFalse(cfg.contract_strategies.hedge_rebalancer_enabled)
        self.assertFalse(cfg.contract_strategies.live_enabled)
        self.assertEqual(cfg.contract_strategies.spot_exchange, "bybit-spot")
        self.assertEqual(cfg.contract_strategies.spot_symbol, "ACS/USDT")
        self.assertEqual(cfg.contract_strategies.derivative_exchange, "")
        self.assertEqual(cfg.contract_strategies.derivative_symbol, "")
        self.assertEqual(cfg.contract_strategies.notional_quote, 200.0)
        self.assertEqual(cfg.contract_strategies.basis_entry_bps, 15.0)
        self.assertEqual(cfg.contract_strategies.futures_grid_max_leverage, 1.0)
        self.assertFalse(cfg.triangular_arbitrage.enabled)
        self.assertEqual(cfg.triangular_arbitrage.notional_quote, 50.0)
        self.assertEqual(cfg.triangular_arbitrage.min_profit_quote, 0.05)
        self.assertEqual(cfg.triangular_arbitrage.min_profit_bps, 5.0)
        self.assertEqual(cfg.triangular_arbitrage.routes, [])
        self.assertEqual(cfg.option_combos, [])
        self.assertTrue(cfg.strategy_center.enabled)
        self.assertEqual(cfg.strategy_center.path, "data/strategy_center.sqlite3")
        self.assertEqual(cfg.strategy_center.max_recent_signals, 100)
        self.assertTrue(cfg.portfolio.enabled)
        self.assertEqual(cfg.portfolio.positions[0].asset, "ACS")
        self.assertEqual(cfg.portfolio.positions[0].position_base, 0.0)
        self.assertEqual(cfg.portfolio.positions[0].average_entry_price, 0.0)
        self.assertEqual(cfg.portfolio.cash_balances["USDC"], 0.0)
        self.assertEqual(cfg.portfolio.cash_balances["USDT"], 0.0)
        self.assertEqual(cfg.portfolio.cash_balances["KRW"], 0.0)
        self.assertEqual(cfg.portfolio.realized_pnl["market_maker"], 0.0)
        self.assertEqual(cfg.portfolio.realized_pnl["arbitrage"], 0.0)
        self.assertTrue(cfg.risk.enabled)
        self.assertTrue(cfg.risk.trading_enabled)
        self.assertFalse(cfg.risk.allow_live_trading)
        self.assertTrue(cfg.risk.allow_market_maker)
        self.assertTrue(cfg.risk.allow_slow_execution)
        self.assertFalse(cfg.risk.require_post_only)
        self.assertTrue(cfg.risk.strategy_enabled["market_maker"])
        self.assertTrue(cfg.risk.strategy_enabled["slow_execution"])
        self.assertTrue(cfg.risk.strategy_enabled["spot_grid"])
        self.assertTrue(cfg.risk.strategy_enabled["dca"])
        self.assertTrue(cfg.risk.strategy_enabled["execution_algo"])
        self.assertTrue(cfg.risk.strategy_enabled["backtest"])
        self.assertTrue(cfg.risk.strategy_enabled["cash_and_carry"])
        self.assertTrue(cfg.risk.strategy_enabled["triangular_arbitrage"])
        self.assertTrue(cfg.risk.strategy_enabled["funding_arbitrage"])
        self.assertTrue(cfg.risk.strategy_enabled["funding_bot"])
        self.assertTrue(cfg.risk.strategy_enabled["basis_bot"])
        self.assertTrue(cfg.risk.strategy_enabled["futures_grid"])
        self.assertTrue(cfg.risk.strategy_enabled["hedge_rebalancer"])
        self.assertTrue(cfg.risk.strategy_enabled["options_arbitrage"])
        self.assertTrue(cfg.risk.strategy_enabled["signal_bot"])
        self.assertEqual(cfg.risk.strategy_overrides, {})
        self.assertTrue(cfg.risk.account_enabled["bybit-spot"])
        self.assertTrue(cfg.risk.account_enabled["binance-spot"])
        self.assertTrue(cfg.risk.account_enabled["binance-swap"])
        self.assertTrue(cfg.risk.account_enabled["bybit-swap"])
        self.assertEqual(cfg.risk.max_order_quote, 5.0)
        self.assertEqual(cfg.risk.max_cycle_quote, 25.0)
        self.assertEqual(cfg.risk.max_position_base_by_asset["ACS"], 0.0)
        self.assertEqual(cfg.risk.max_exposure_quote_by_asset["ACS"], 0.0)
        self.assertEqual(cfg.risk.max_daily_loss_quote, 0.0)
        self.assertEqual(cfg.risk.max_open_orders, 50)
        self.assertEqual(cfg.risk.max_cancels_per_cycle, 50)
        self.assertEqual(cfg.risk.max_slippage_bps, 50.0)
        self.assertEqual(cfg.risk.min_order_book_depth_quote, 0.0)
        self.assertEqual(cfg.risk.max_order_book_gap_bps, 2000.0)
        self.assertEqual(cfg.risk.max_price_jump_bps, 1000.0)
        self.assertEqual(cfg.risk.max_order_book_age_seconds, 10.0)
        self.assertEqual(cfg.risk.max_derivative_leverage, 0.0)
        self.assertEqual(cfg.risk.min_liquidation_buffer_pct, 0.0)
        self.assertEqual(cfg.risk.max_margin_usage_pct, 0.0)
        self.assertTrue(cfg.trade_log.enabled)
        self.assertEqual(cfg.trade_log.path, "data/trade_events.jsonl")
        self.assertEqual(cfg.trade_log.rotate_max_bytes, 67108864)
        self.assertEqual(cfg.trade_log.rotate_keep_files, 8)
        self.assertTrue(cfg.trade_log.rotate_compress)
        self.assertTrue(cfg.strategy_timeline.enabled)
        self.assertEqual(
            cfg.strategy_timeline.path,
            "data/strategy_timeline.jsonl",
        )
        self.assertEqual(cfg.strategy_timeline.max_recent_events, 100)
        self.assertEqual(cfg.strategy_timeline.rotate_max_bytes, 67108864)
        self.assertEqual(cfg.strategy_timeline.rotate_keep_files, 8)
        self.assertTrue(cfg.strategy_timeline.rotate_compress)
        self.assertTrue(cfg.pnl_store.enabled)
        self.assertEqual(cfg.pnl_store.path, "data/fill_pnl.sqlite3")
        self.assertFalse(cfg.alerts.enabled)
        self.assertFalse(cfg.alerts.auto_stop_enabled)
        self.assertFalse(cfg.alerts.daily_report_enabled)
        self.assertEqual(cfg.alerts.daily_report_time, "23:59")
        self.assertEqual(cfg.web_security.password_env, "CRYPTO_ARB_WEB_PASSWORD")
        self.assertEqual(
            cfg.web_security.allowed_ips_env,
            "CRYPTO_ARB_WEB_ALLOWED_IPS",
        )
        self.assertEqual(cfg.web_security.user_store_path, "data/web_users.json")
        self.assertFalse(cfg.web_security.registration_enabled)
        self.assertEqual(
            cfg.web_security.registration_code_env,
            "CRYPTO_ARB_WEB_REGISTRATION_CODE",
        )
        self.assertEqual(cfg.web_security.totp_issuer, "DayDayUp Trade")
        self.assertTrue(
            any(
                market.exchange == "upbit-spot"
                and market.symbol == "ACS/USDT"
                and market.quote_currency == "USDT"
                for market in cfg.spot_markets
            )
        )
        self.assertTrue(any(exchange.key == "upbit-spot" for exchange in cfg.spot_exchanges))
        spot_by_key = {exchange.key: exchange for exchange in cfg.spot_exchanges}
        derivative_by_key = {exchange.key: exchange for exchange in cfg.derivative_exchanges}
        self.assertEqual(spot_by_key["bithumb-spot"].options["private_api"], "v2")
        self.assertEqual(spot_by_key["upbit-spot"].api_key_env, "UPBIT_ID_API_KEY")
        self.assertEqual(spot_by_key["upbit-spot"].secret_env, "UPBIT_ID_SECRET")
        self.assertEqual(spot_by_key["upbit-spot"].options["hostname"], "id-api.upbit.com")
        self.assertEqual(spot_by_key["binance-spot"].id, "binance")
        self.assertEqual(spot_by_key["binance-spot"].options["defaultType"], "spot")
        self.assertEqual(derivative_by_key["binance-swap"].id, "binanceusdm")
        self.assertEqual(derivative_by_key["binance-swap"].market_type, "swap")
        self.assertEqual(derivative_by_key["bybit-swap"].id, "bybit")
        self.assertEqual(derivative_by_key["bybit-swap"].options["defaultType"], "swap")

    def test_onchain_rpc_env_overrides_configured_urls(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config.acs.example.json"

        with patch.dict(
            "os.environ",
            {
                "SOLANA_RPC_URLS": (
                    "https://fast-rpc.example/one,"
                    "https://fast-rpc.example/two"
                )
            },
        ):
            cfg = load_config(config_path)

        self.assertEqual(cfg.onchain_monitor.rpc_url, "https://fast-rpc.example/one")
        self.assertEqual(
            cfg.onchain_monitor.rpc_urls[:2],
            [
                "https://fast-rpc.example/one",
                "https://fast-rpc.example/two",
            ],
        )


if __name__ == "__main__":
    unittest.main()
