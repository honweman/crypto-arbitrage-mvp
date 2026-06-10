import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import ExchangeConfig, load_config
from arbitrage_bot.exchanges import (
    ExchangeManager,
    _credential_from_env,
    _proxy_options_from_env,
    limit_order_capability_errors,
    limit_order_features,
)


class ExchangeProxyConfigTest(unittest.TestCase):
    def test_credential_env_unescapes_newlines(self) -> None:
        with patch.dict(
            os.environ,
            {
                "COINBASE_SECRET": (
                    "-----BEGIN EC PRIVATE KEY-----\\n"
                    "secret-body\\n"
                    "-----END EC PRIVATE KEY-----\\n"
                )
            },
        ):
            self.assertEqual(
                _credential_from_env("COINBASE_SECRET"),
                (
                    "-----BEGIN EC PRIVATE KEY-----\n"
                    "secret-body\n"
                    "-----END EC PRIVATE KEY-----\n"
                ),
            )

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

    def test_bithumb_limit_order_features_block_post_only(self) -> None:
        cfg = ExchangeConfig(id="bithumb", label="bithumb-spot")

        features = limit_order_features(cfg)
        errors = limit_order_capability_errors(cfg, post_only=True)

        self.assertFalse(features.post_only)
        self.assertFalse(features.client_order_id)
        self.assertTrue(any("post-only" in error for error in errors))

    def test_bybit_limit_order_features_allow_post_only(self) -> None:
        cfg = ExchangeConfig(id="bybit", label="bybit-spot")

        features = limit_order_features(cfg)
        errors = limit_order_capability_errors(cfg, post_only=True)

        self.assertTrue(features.post_only)
        self.assertTrue(features.client_order_id)
        self.assertEqual(errors, [])


class ExchangeManagerAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_limit_orders_loads_markets_once(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.load_markets_count = 0

            async def load_markets(self) -> dict[str, object]:
                self.load_markets_count += 1
                return {
                    "ACS/USDT": {
                        "limits": {
                            "amount": {"min": 0.0, "max": None},
                            "price": {"min": 0.0, "max": None},
                            "cost": {"min": 0.0, "max": None},
                        },
                        "precision": {"amount": 0.1, "price": 0.0000001},
                    }
                }

            def amount_to_precision(self, _: str, amount: float) -> str:
                return f"{amount:.1f}"

            def price_to_precision(self, _: str, price: float) -> str:
                return f"{price:.7f}"

        cfg = ExchangeConfig(id="bybit", label="bybit-spot")
        client = FakeClient()
        manager = ExchangeManager()
        manager._clients[cfg.key] = client  # noqa: SLF001

        rows = await manager.prepare_limit_orders(
            cfg,
            symbol="ACS/USDT",
            orders=[
                {"side": "buy", "amount": 10.01, "price": 0.00014001},
                {"side": "sell", "amount": 11.02, "price": 0.00015002},
            ],
        )

        self.assertEqual(client.load_markets_count, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
