from pathlib import Path
import unittest

from arbitrage_bot.config import load_config


class ConfigTest(unittest.TestCase):
    def test_acs_onchain_monitor_uses_top_20_and_labels(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config.acs.example.json"

        cfg = load_config(config_path)

        self.assertEqual(cfg.onchain_monitor.top_n, 20)
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
        self.assertFalse(cfg.slow_execution.enabled)
        self.assertEqual(cfg.slow_execution.exchange, "bybit-spot")
        self.assertEqual(cfg.slow_execution.symbol, "ACS/USDT")
        self.assertEqual(cfg.slow_execution.side, "sell")
        self.assertEqual(cfg.slow_execution.interval_seconds, 60.0)
        self.assertEqual(cfg.slow_execution.total_quote, 0.0)
        self.assertEqual(cfg.slow_execution.slice_base_min, 0.0)
        self.assertEqual(cfg.slow_execution.slice_base_max, 0.0)
        self.assertFalse(cfg.slow_execution.randomize_slice)
        self.assertEqual(cfg.slow_execution.order_ttl_seconds, 0.0)
        self.assertEqual(cfg.slow_execution.start_price, 0.0)
        self.assertEqual(cfg.slow_execution.stop_price, 0.0)
        self.assertFalse(cfg.slow_execution.post_only)
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
        self.assertTrue(cfg.risk.strategy_enabled["cash_and_carry"])
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
        self.assertTrue(cfg.trade_log.enabled)
        self.assertEqual(cfg.trade_log.path, "data/trade_events.jsonl")
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
        self.assertEqual(spot_by_key["binance-spot"].id, "binance")
        self.assertEqual(spot_by_key["binance-spot"].options["defaultType"], "spot")
        self.assertEqual(derivative_by_key["binance-swap"].id, "binanceusdm")
        self.assertEqual(derivative_by_key["binance-swap"].market_type, "swap")
        self.assertEqual(derivative_by_key["bybit-swap"].id, "bybit")
        self.assertEqual(derivative_by_key["bybit-swap"].options["defaultType"], "swap")


if __name__ == "__main__":
    unittest.main()
