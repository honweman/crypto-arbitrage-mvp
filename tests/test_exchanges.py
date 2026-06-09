import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import ExchangeConfig, load_config
from arbitrage_bot.exchanges import _proxy_options_from_env


class ExchangeProxyConfigTest(unittest.TestCase):
    def test_proxy_options_are_read_from_env(self) -> None:
        cfg = ExchangeConfig(
            id="bybit",
            label="bybit-account-a",
            https_proxy_env="BYBIT_ACCOUNT_A_PROXY",
        )

        with patch.dict(
            os.environ,
            {"BYBIT_ACCOUNT_A_PROXY": "http://user:pass@10.0.0.10:8080"},
        ):
            self.assertEqual(
                _proxy_options_from_env(cfg),
                {"httpsProxy": "http://user:pass@10.0.0.10:8080"},
            )

    def test_empty_proxy_env_is_ignored(self) -> None:
        cfg = ExchangeConfig(
            id="bybit",
            label="bybit-account-a",
            https_proxy_env="BYBIT_ACCOUNT_A_PROXY",
        )

        with patch.dict(os.environ, {"BYBIT_ACCOUNT_A_PROXY": ""}):
            self.assertEqual(_proxy_options_from_env(cfg), {})

    def test_multiple_rest_proxy_envs_raise(self) -> None:
        cfg = ExchangeConfig(
            id="bybit",
            label="bybit-account-a",
            http_proxy_env="BYBIT_ACCOUNT_A_HTTP_PROXY",
            https_proxy_env="BYBIT_ACCOUNT_A_HTTPS_PROXY",
        )

        with patch.dict(
            os.environ,
            {
                "BYBIT_ACCOUNT_A_HTTP_PROXY": "http://10.0.0.10:8080",
                "BYBIT_ACCOUNT_A_HTTPS_PROXY": "http://10.0.0.11:8080",
            },
        ):
            with self.assertRaisesRegex(ValueError, "multiple REST proxy"):
                _proxy_options_from_env(cfg)

    def test_exchange_proxy_envs_are_parsed_from_config(self) -> None:
        raw_config = {
            "spot_exchanges": [
                {
                    "id": "bybit",
                    "label": "bybit-account-a",
                    "api_key_env": "BYBIT_ACCOUNT_A_API_KEY",
                    "secret_env": "BYBIT_ACCOUNT_A_SECRET",
                    "https_proxy_env": "BYBIT_ACCOUNT_A_PROXY",
                    "ws_proxy_env": "BYBIT_ACCOUNT_A_WS_PROXY",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(raw_config), encoding="utf-8")

            cfg = load_config(config_path)

        exchange = cfg.spot_exchanges[0]
        self.assertEqual(exchange.key, "bybit-account-a")
        self.assertEqual(exchange.api_key_env, "BYBIT_ACCOUNT_A_API_KEY")
        self.assertEqual(exchange.secret_env, "BYBIT_ACCOUNT_A_SECRET")
        self.assertEqual(exchange.https_proxy_env, "BYBIT_ACCOUNT_A_PROXY")
        self.assertEqual(exchange.ws_proxy_env, "BYBIT_ACCOUNT_A_WS_PROXY")


if __name__ == "__main__":
    unittest.main()
