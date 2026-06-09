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
        self.assertEqual(cfg.market_maker.exchange, "bybit-spot")
        self.assertEqual(cfg.market_maker.symbol, "ACS/USDT")
        self.assertEqual(cfg.market_maker.levels, 10)
        self.assertEqual(cfg.market_maker.price_band_pct, 10.0)
        self.assertEqual(cfg.market_maker.quote_per_level, 1.0)


if __name__ == "__main__":
    unittest.main()
