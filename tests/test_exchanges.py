from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import ExchangeConfig, load_config
from arbitrage_bot.exchanges import (
    BithumbV2Client,
    ExchangeManager,
    _bithumb_market_code,
    _bithumb_query_string,
    _credential_from_env,
    _jwt_hs256,
    _normalize_bithumb_v2_order,
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

    def test_ccxt_top_level_options_are_not_nested(self) -> None:
        class FakeUpbit:
            def __init__(self, options: dict[str, object]) -> None:
                self.options_payload = options

        class FakeCcxt:
            upbit = FakeUpbit

        cfg = ExchangeConfig(
            id="upbit",
            label="upbit-spot",
            api_key_env="UPBIT_ID_API_KEY",
            secret_env="UPBIT_ID_SECRET",
            options={
                "hostname": "id-api.upbit.com",
                "createMarketBuyOrderRequiresPrice": True,
            },
        )
        manager = ExchangeManager()

        with patch(
            "arbitrage_bot.exchanges.importlib.import_module",
            return_value=FakeCcxt,
        ), patch.dict(
            os.environ,
            {
                "UPBIT_ID_API_KEY": "key",
                "UPBIT_ID_SECRET": "secret",
            },
            clear=True,
        ):
            client = manager.client(cfg)

        self.assertEqual(client.options_payload["hostname"], "id-api.upbit.com")
        self.assertNotIn("hostname", client.options_payload["options"])
        self.assertEqual(
            client.options_payload["options"]["createMarketBuyOrderRequiresPrice"],
            True,
        )
        self.assertEqual(client.options_payload["apiKey"], "key")
        self.assertEqual(client.options_payload["secret"], "secret")

    def test_direct_credentials_override_environment_without_global_mutation(self) -> None:
        class FakeCoinbase:
            def __init__(self, options: dict[str, object]) -> None:
                self.options_payload = options

        class FakeCcxt:
            coinbase = FakeCoinbase

        cfg = ExchangeConfig(
            id="coinbase",
            label="workspace:account-1",
            api_key_env="GLOBAL_API_KEY",
            secret_env="GLOBAL_SECRET",
        )
        manager = ExchangeManager(
            credentials_by_key={
                cfg.key: {
                    "api_key": "direct-key",
                    "secret": "direct-secret",
                    "passphrase": "direct-passphrase",
                }
            }
        )

        with patch(
            "arbitrage_bot.exchanges.importlib.import_module",
            return_value=FakeCcxt,
        ), patch.dict(
            os.environ,
            {"GLOBAL_API_KEY": "global-key", "GLOBAL_SECRET": "global-secret"},
            clear=True,
        ):
            client = manager.client(cfg)
            global_api_key_after = os.environ["GLOBAL_API_KEY"]

        self.assertEqual(client.options_payload["apiKey"], "direct-key")
        self.assertEqual(client.options_payload["secret"], "direct-secret")
        self.assertEqual(client.options_payload["password"], "direct-passphrase")
        self.assertEqual(global_api_key_after, "global-key")

    def test_bithumb_limit_order_features_block_post_only(self) -> None:
        cfg = ExchangeConfig(id="bithumb", label="bithumb-spot")

        features = limit_order_features(cfg)
        errors = limit_order_capability_errors(cfg, post_only=True)

        self.assertFalse(features.post_only)
        self.assertTrue(features.client_order_id)
        self.assertTrue(features.recover_by_client_order_id)
        self.assertTrue(any("post-only" in error for error in errors))

    def test_bithumb_v2_helpers_match_official_payload_shape(self) -> None:
        self.assertEqual(_bithumb_market_code("ACS/KRW"), "KRW-ACS")
        self.assertEqual(
            _bithumb_query_string(
                {
                    "market": "KRW-ACS",
                    "side": "bid",
                    "order_type": "limit",
                    "price": 0.2172,
                    "volume": 10.0,
                }
            ),
            "market=KRW-ACS&side=bid&order_type=limit&price=0.2172&volume=10",
        )
        self.assertEqual(
            _bithumb_query_string({"order_ids": ["id1", "id2"]}),
            "order_ids[]=id1&order_ids[]=id2",
        )
        self.assertEqual(
            _jwt_hs256(
                {"access_key": "access", "nonce": "nonce", "timestamp": 1},
                "secret",
            ).count("."),
            2,
        )

    def test_bithumb_v2_order_normalization_is_ccxt_like(self) -> None:
        row = _normalize_bithumb_v2_order(
            {
                "order_id": "order-1",
                "client_order_id": "client-1",
                "market": "KRW-ACS",
                "side": "bid",
                "order_type": "limit",
                "state": "wait",
                "price": "0.2172",
                "volume": "1000",
                "remaining_volume": "750",
                "executed_volume": "250",
                "created_at": "2026-06-15T12:00:00+09:00",
            },
            "ACS/KRW",
        )

        self.assertEqual(row["id"], "order-1")
        self.assertEqual(row["clientOrderId"], "client-1")
        self.assertEqual(row["symbol"], "ACS/KRW")
        self.assertEqual(row["side"], "buy")
        self.assertEqual(row["type"], "limit")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["price"], 0.2172)
        self.assertEqual(row["amount"], 1000.0)
        self.assertEqual(row["filled"], 250.0)
        self.assertEqual(row["remaining"], 750.0)

    def test_bybit_limit_order_features_allow_post_only(self) -> None:
        cfg = ExchangeConfig(id="bybit", label="bybit-spot")

        features = limit_order_features(cfg)
        errors = limit_order_capability_errors(cfg, post_only=True)

        self.assertTrue(features.post_only)
        self.assertTrue(features.client_order_id)
        self.assertTrue(features.batch_create)
        self.assertTrue(features.batch_cancel)
        self.assertEqual(errors, [])

    def test_binance_limit_order_features_allow_post_only(self) -> None:
        cfg = ExchangeConfig(id="binance", label="binance-spot")

        features = limit_order_features(cfg)
        errors = limit_order_capability_errors(cfg, post_only=True)

        self.assertTrue(features.post_only)
        self.assertTrue(features.client_order_id)
        self.assertTrue(features.batch_create)
        self.assertTrue(features.batch_cancel)
        self.assertEqual(errors, [])

    def test_binance_usdm_limit_order_features_allow_post_only(self) -> None:
        cfg = ExchangeConfig(
            id="binanceusdm",
            label="binance-swap",
            market_type="swap",
        )

        features = limit_order_features(cfg)
        errors = limit_order_capability_errors(cfg, post_only=True)

        self.assertTrue(features.post_only)
        self.assertTrue(features.client_order_id)
        self.assertTrue(features.batch_create)
        self.assertTrue(features.batch_cancel)
        self.assertEqual(errors, [])


class ExchangeManagerAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_ohlcv_uses_public_ccxt_capability(self) -> None:
        class FakeClient:
            has = {"fetchOHLCV": True}

            def __init__(self) -> None:
                self.load_count = 0
                self.args = None

            async def load_markets(self) -> dict[str, object]:
                self.load_count += 1
                return {}

            async def fetch_ohlcv(self, *args: object) -> list[list[float]]:
                self.args = args
                return [[1_700_000_000_000, 1.0, 1.1, 0.9, 1.05, 10.0]]

        cfg = ExchangeConfig(id="coinbase", label="coinbase-public")
        client = FakeClient()
        manager = ExchangeManager()
        manager._clients[cfg.key] = client  # noqa: SLF001

        rows = await manager.fetch_ohlcv(
            cfg,
            symbol="ACS/USDC",
            timeframe="1h",
            since_ms=1_699_000_000_000,
            limit=200,
        )

        self.assertEqual(client.load_count, 1)
        self.assertEqual(
            client.args,
            ("ACS/USDC", "1h", 1_699_000_000_000, 200),
        )
        self.assertEqual(rows[0][4], 1.05)

    async def test_bithumb_v2_fetch_closed_orders_paginates_requested_limit(
        self,
    ) -> None:
        cfg = ExchangeConfig(id="bithumb", label="bithumb-spot")
        client = BithumbV2Client(cfg, object(), api_key="key", secret="secret")
        requests: list[dict[str, object]] = []

        async def fake_request(
            _method: str,
            _path: str,
            *,
            params: dict[str, object] | None = None,
            json_body: dict[str, object] | None = None,
        ) -> list[dict[str, object]]:
            self.assertIsNone(json_body)
            params = dict(params or {})
            requests.append(params)
            page = int(params.get("page") or 1)
            if page == 1:
                start, count = 0, 100
            elif page == 2:
                start, count = 100, 50
            else:
                return []
            return [
                {
                    "order_id": f"order-{index}",
                    "market": "KRW-ACS",
                    "side": "bid",
                    "state": "done",
                    "price": "0.22",
                    "volume": "10",
                    "executed_volume": "10",
                    "executed_funds": "2.2",
                }
                for index in range(start, start + count)
            ]

        client._request = fake_request  # type: ignore[method-assign]

        rows = await client.fetch_closed_orders("ACS/KRW", limit=150)

        self.assertEqual(len(rows), 150)
        self.assertEqual([request["page"] for request in requests], [1, 2])
        self.assertEqual([request["limit"] for request in requests], [100, 50])

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

    async def test_prepare_limit_orders_rounds_upbit_usdt_prices_to_tick(self) -> None:
        class FakeClient:
            async def load_markets(self) -> dict[str, object]:
                return {
                    "ACS/USDT": {
                        "limits": {
                            "amount": {"min": 0.0, "max": None},
                            "price": {"min": 0.0, "max": None},
                            "cost": {"min": 0.0, "max": None},
                        },
                        "precision": {"amount": 0.00000001, "price": 0.00000001},
                    }
                }

            def amount_to_precision(self, _: str, amount: float) -> str:
                return f"{amount:.8f}"

            def price_to_precision(self, _: str, price: float) -> str:
                return f"{price:.8f}"

        cfg = ExchangeConfig(id="upbit", label="upbit-spot")
        manager = ExchangeManager()
        manager._clients[cfg.key] = FakeClient()  # noqa: SLF001

        rows = await manager.prepare_limit_orders(
            cfg,
            symbol="ACS/USDT",
            orders=[
                {"side": "buy", "amount": 1000.0, "price": 0.000143456},
                {"side": "sell", "amount": 1000.0, "price": 0.000143456},
            ],
        )

        self.assertEqual(rows[0]["price"], 0.0001434)
        self.assertEqual(rows[1]["price"], 0.0001435)

    async def test_bithumb_krw_uses_private_api_minimum_order_cost(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.market = {
                    "limits": {
                        "amount": {"min": 1.0, "max": None},
                        "price": {"min": 0.0001, "max": None},
                        "cost": {"min": 500.0, "max": None},
                    },
                    "precision": {"amount": 1.0, "price": 0.0001},
                }

            async def load_markets(self) -> dict[str, object]:
                return {"ACS/KRW": self.market}

            def amount_to_precision(self, _: str, amount: float) -> str:
                return f"{amount:.0f}"

            def price_to_precision(self, _: str, price: float) -> str:
                return f"{price:.4f}"

        cfg = ExchangeConfig(id="bithumb", label="bithumb-spot")
        client = FakeClient()
        manager = ExchangeManager()
        manager._clients[cfg.key] = client  # noqa: SLF001

        rejected = await manager.prepare_limit_order(
            cfg,
            symbol="ACS/KRW",
            side="buy",
            amount=30_000.0,
            price=0.16,
        )
        accepted = await manager.prepare_limit_order(
            cfg,
            symbol="ACS/KRW",
            side="buy",
            amount=32_000.0,
            price=0.16,
        )
        market = await manager.fetch_market_info(cfg, symbol="ACS/KRW")

        self.assertEqual(rejected["status"], "error")
        self.assertIn("minimum 5000", rejected["errors"][0])
        self.assertEqual(accepted["status"], "ok")
        self.assertEqual(market["limits"]["cost"]["min"], 5_000.0)
        self.assertEqual(client.market["limits"]["cost"]["min"], 500.0)

    async def test_create_prepared_limit_orders_uses_batch_client(self) -> None:
        class FakeClient:
            has = {"createOrders": True}

            def __init__(self) -> None:
                self.orders = []

            async def create_orders(self, orders: list[dict[str, object]]) -> list[dict[str, str]]:
                self.orders = orders
                return [{"id": f"order-{index}"} for index, _ in enumerate(orders, 1)]

        cfg = ExchangeConfig(id="bybit", label="bybit-spot")
        client = FakeClient()
        manager = ExchangeManager()
        manager._clients[cfg.key] = client  # noqa: SLF001

        result = await manager.create_prepared_limit_orders(
            cfg,
            symbol="ACS/USDT",
            sides=["buy", "sell"],
            prepared_orders=[
                {"amount": 10.0, "price": 0.00014, "errors": []},
                {"amount": 11.0, "price": 0.00015, "errors": []},
            ],
            post_only=True,
            client_order_ids=["cid-1", "cid-2"],
        )

        self.assertEqual([item["id"] for item in result], ["order-1", "order-2"])
        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[0]["params"]["postOnly"], True)
        self.assertEqual(client.orders[0]["params"]["clientOrderId"], "cid-1")

    async def test_cancel_orders_uses_batch_client(self) -> None:
        class FakeClient:
            has = {"cancelOrders": True}

            def __init__(self) -> None:
                self.ids = []
                self.symbol = ""

            async def cancel_orders(
                self,
                ids: list[str],
                symbol: str,
            ) -> list[dict[str, str]]:
                self.ids = ids
                self.symbol = symbol
                return [{"id": order_id, "status": "canceled"} for order_id in ids]

        cfg = ExchangeConfig(id="bybit", label="bybit-spot")
        client = FakeClient()
        manager = ExchangeManager()
        manager._clients[cfg.key] = client  # noqa: SLF001

        result = await manager.cancel_orders(
            cfg,
            symbol="ACS/USDT",
            order_ids=["a", "b"],
        )

        self.assertEqual(client.ids, ["a", "b"])
        self.assertEqual(client.symbol, "ACS/USDT")
        self.assertEqual(len(result), 2)

    async def test_optional_history_fetches_respect_ccxt_capabilities(self) -> None:
        class FakeClient:
            has = {
                "fetchClosedOrders": False,
                "fetchMyTrades": False,
            }

            async def fetch_closed_orders(self, *_: object) -> list[dict[str, object]]:
                raise AssertionError("fetch_closed_orders should not be called")

            async def fetch_my_trades(self, *_: object) -> list[dict[str, object]]:
                raise AssertionError("fetch_my_trades should not be called")

        cfg = ExchangeConfig(id="upbit", label="upbit-spot")
        manager = ExchangeManager()
        manager._clients[cfg.key] = FakeClient()  # noqa: SLF001

        self.assertEqual(
            await manager.fetch_closed_orders(cfg, symbol="ACS/USDT"),
            [],
        )
        self.assertEqual(
            await manager.fetch_my_trades(cfg, symbol="ACS/USDT"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
