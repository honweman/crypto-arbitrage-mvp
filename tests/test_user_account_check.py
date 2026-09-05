from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.user_account_check import (
    WorkspaceAccountCheckService,
    check_workspace_api_connection,
    check_workspace_account,
    discover_workspace_markets,
    workspace_exchange_config,
)
from arbitrage_bot.user_workspace import (
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
)


class FakeWorkspaceManager:
    instances: list["FakeWorkspaceManager"] = []

    def __init__(self, *, credentials_by_key=None) -> None:
        self.credentials_by_key = credentials_by_key or {}
        self.closed = False
        self.client_instance = FakeWorkspaceClient()
        self.instances.append(self)

    def client(self, _cfg):
        return self.client_instance

    async def fetch_market_info(self, _cfg, *, symbol):
        return self.client_instance.markets.get(symbol)

    async def fetch_order_book(self, cfg, symbol, _depth):
        return OrderBookSnapshot(
            exchange=cfg.key,
            symbol=symbol,
            bids=[BookLevel(price=0.20, amount=1000)],
            asks=[BookLevel(price=0.21, amount=900)],
            timestamp_ms=1_700_000_000_000,
        )

    async def fetch_balance(self, _cfg):
        return {
            "ACS": {"free": 1000.0, "used": 10.0, "total": 1010.0},
            "USDC": {"free": 20.0, "used": 1.0, "total": 21.0},
        }

    async def fetch_open_orders(self, _cfg, *, symbol):
        return [{"id": "open-1", "symbol": symbol}]

    async def close(self):
        self.closed = True


class FakeWorkspaceClient:
    def __init__(self) -> None:
        self.markets = {
            "ACS/USDC": {
                "symbol": "ACS/USDC",
                "base": "ACS",
                "quote": "USDC",
                "type": "spot",
                "spot": True,
                "active": True,
                "limits": {
                    "amount": {"min": 1.0},
                    "cost": {"min": 1.0},
                },
                "precision": {"amount": 1.0, "price": 0.0000001},
            },
            "ACS/USDT:USDT": {
                "symbol": "ACS/USDT:USDT",
                "base": "ACS",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "active": True,
            },
            "BTC/USDC": {
                "symbol": "BTC/USDC",
                "base": "BTC",
                "quote": "USDC",
                "type": "spot",
                "spot": True,
                "active": True,
            },
            "ACS/KRW": {
                "symbol": "ACS/KRW",
                "base": "ACS",
                "quote": "KRW",
                "type": "spot",
                "spot": True,
                "active": False,
            },
        }

    async def load_markets(self):
        return self.markets

    async def fetch_open_orders(self):
        return [{"id": "open-1"}]

    async def fetch_tickers(self, symbols):
        prices = {"ACS/USDC": 0.2, "BTC/USDC": 60_000.0}
        return {
            symbol: {"symbol": symbol, "last": prices[symbol]}
            for symbol in symbols
            if symbol in prices
        }


class FakeBybitWorkspaceClient(FakeWorkspaceClient):
    async def fetch_balance(self, params=None):
        self.balance_params = dict(params or {})
        return {
            "ACS": {"free": 500.0, "used": 0.0, "total": 500.0},
        }


class FakeBybitWorkspaceManager(FakeWorkspaceManager):
    def __init__(self, *, credentials_by_key=None) -> None:
        super().__init__(credentials_by_key=credentials_by_key)
        self.client_instance = FakeBybitWorkspaceClient()

    async def fetch_balance(self, _cfg):
        return {
            "ACS": {"free": 0.0, "used": 0.0, "total": 0.0},
            "USDC": {"free": 0.0, "used": 0.0, "total": 0.0},
        }


class FakeBinanceWorkspaceManager(FakeWorkspaceManager):
    async def fetch_balance(self, cfg):
        total = 25.0 if cfg.market_type == "spot" else 100.0
        return {
            "USDT": {"free": total, "used": 0.0, "total": total},
        }


class FakeKucoinWorkspaceClient(FakeWorkspaceClient):
    async def fetch_balance(self, params=None):
        assert (params or {}).get("type") == "main"
        return {
            "KCS": {"free": 2.0, "used": 0.0, "total": 2.0},
            "USDT": {"free": 5.0, "used": 0.0, "total": 5.0},
        }


class FakeKucoinWorkspaceManager(FakeBinanceWorkspaceManager):
    def __init__(self, *, credentials_by_key=None) -> None:
        super().__init__(credentials_by_key=credentials_by_key)
        self.client_instance = FakeKucoinWorkspaceClient()


class FakePricedWorkspaceClient(FakeWorkspaceClient):
    def __init__(self) -> None:
        super().__init__()
        self.markets["BNB/USDT"] = {
            "symbol": "BNB/USDT",
            "base": "BNB",
            "quote": "USDT",
            "type": "spot",
            "spot": True,
            "active": True,
        }

    async def fetch_tickers(self, symbols):
        return {
            "BNB/USDT": {
                "symbol": "BNB/USDT",
                "bid": 599.0,
                "ask": 601.0,
            }
        }


class FakePricedWorkspaceManager(FakeWorkspaceManager):
    def __init__(self, *, credentials_by_key=None) -> None:
        super().__init__(credentials_by_key=credentials_by_key)
        self.client_instance = FakePricedWorkspaceClient()

    async def fetch_balance(self, _cfg):
        return {
            "BNB": {"free": 9.99, "used": 0.0, "total": 9.99},
            "USDT": {"free": 100.0, "used": 0.0, "total": 100.0},
        }


class FakeCoinbaseReserveClient(FakeWorkspaceClient):
    async def fetch_open_orders(self):
        return [
            {
                "id": "buy-1",
                "symbol": "ACS/USDC",
                "side": "buy",
                "price": 0.2,
                "amount": 100.0,
                "filled": 0.0,
                "remaining": 100.0,
            },
            {
                "id": "sell-1",
                "symbol": "ACS/USDC",
                "side": "sell",
                "price": 0.3,
                "amount": 100.0,
                "filled": 0.0,
                "remaining": 100.0,
            },
        ]


class FakeCoinbaseReserveManager(FakeWorkspaceManager):
    def __init__(self, *, credentials_by_key=None) -> None:
        super().__init__(credentials_by_key=credentials_by_key)
        self.client_instance = FakeCoinbaseReserveClient()

    async def fetch_balance(self, _cfg):
        return {
            "ACS": {"free": 900.0, "used": 0.0, "total": 900.0},
            "USDC": {"free": 80.0, "used": 0.0, "total": 80.0},
        }


class FailingWorkspaceManager(FakeWorkspaceManager):
    async def fetch_balance(self, _cfg):
        secret = next(iter(self.credentials_by_key.values()))["secret"]
        raise RuntimeError(f"authentication failed for {secret}")


class EgressWorkspaceManager(FakeWorkspaceManager):
    async def fetch_egress_ip(self, _cfg):
        return "203.0.113.11"


class UserAccountCheckTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeWorkspaceManager.instances.clear()

    def test_exchange_config_maps_variants_and_contract_clients(self) -> None:
        upbit = workspace_exchange_config(
            exchange="upbit",
            market_type="spot",
            api_variant="indonesia",
            runtime_key="account-1",
        )
        upbit_korea = workspace_exchange_config(
            exchange="upbit",
            market_type="spot",
            api_variant="korea",
            runtime_key="account-korea",
        )
        bithumb = workspace_exchange_config(
            exchange="bithumb",
            market_type="spot",
            api_variant="v2",
            runtime_key="account-2",
        )
        binance = workspace_exchange_config(
            exchange="binance",
            market_type="swap",
            api_variant="default",
            runtime_key="account-3",
        )
        hyperliquid_testnet = workspace_exchange_config(
            exchange="hyperliquid",
            market_type="swap",
            api_variant="testnet",
            runtime_key="account-4",
        )
        gateio = workspace_exchange_config(
            exchange="gateio",
            market_type="swap",
            api_variant="default",
            runtime_key="account-gateio",
        )
        htx = workspace_exchange_config(
            exchange="htx",
            market_type="swap",
            api_variant="default",
            runtime_key="account-htx",
        )
        mexc = workspace_exchange_config(
            exchange="mexc",
            market_type="swap",
            api_variant="default",
            runtime_key="account-mexc",
        )
        kucoin_spot = workspace_exchange_config(
            exchange="kucoin",
            market_type="spot",
            api_variant="global",
            runtime_key="account-kucoin-spot",
        )
        kucoin_swap = workspace_exchange_config(
            exchange="kucoin",
            market_type="swap",
            api_variant="global",
            runtime_key="account-kucoin-swap",
        )

        self.assertEqual(upbit.options["hostname"], "id-api.upbit.com")
        self.assertEqual(upbit_korea.options["hostname"], "api.upbit.com")
        self.assertEqual(bithumb.options["private_api"], "v2.0")
        self.assertEqual(binance.id, "binanceusdm")
        self.assertEqual(binance.options["defaultType"], "swap")
        self.assertEqual(gateio.id, "gateio")
        self.assertEqual(gateio.options["defaultType"], "swap")
        self.assertEqual(htx.id, "htx")
        self.assertEqual(htx.options["defaultType"], "swap")
        self.assertEqual(htx.options["defaultSubType"], "linear")
        self.assertEqual(mexc.id, "mexc")
        self.assertEqual(mexc.options["defaultType"], "swap")
        self.assertEqual(kucoin_spot.id, "kucoin")
        self.assertEqual(kucoin_spot.options["defaultType"], "spot")
        self.assertEqual(kucoin_swap.id, "kucoinfutures")
        self.assertEqual(kucoin_swap.options["defaultType"], "swap")
        self.assertEqual(
            hyperliquid_testnet.options["hostname"],
            "hyperliquid-testnet.xyz",
        )

        bybit = workspace_exchange_config(
            exchange="bybit",
            market_type="spot",
            api_variant="default",
            runtime_key="account-5",
        )
        self.assertEqual(bybit.options["defaultType"], "spot")

    async def test_discovers_only_active_asset_markets_for_requested_type(self) -> None:
        rows = await discover_workspace_markets(
            exchange="coinbase",
            market_type="spot",
            api_variant="default",
            asset="ACS",
            manager_factory=FakeWorkspaceManager,
        )

        self.assertEqual([row["symbol"] for row in rows], ["ACS/USDC"])
        self.assertEqual(rows[0]["cost_min"], 1.0)
        self.assertTrue(FakeWorkspaceManager.instances[-1].closed)

    async def test_account_check_uses_direct_credentials_and_returns_safe_summary(
        self,
    ) -> None:
        project = UserProject.from_dict(
            {
                "owner_email": "member@example.com",
                "asset": "ACS",
                "quote_currency": "USDC",
                "status": "active",
            }
        )
        account = UserExchangeAccount.from_dict(
            {
                "owner_email": project.owner_email,
                "project_id": project.id,
                "exchange": "coinbase",
                "symbol": "ACS/USDC",
            }
        )
        credentials = {"api_key": "test-key", "secret": "test-secret"}

        result = await check_workspace_account(
            account=account,
            project=project,
            credentials=credentials,
            manager_factory=FakeWorkspaceManager,
        )
        manager = FakeWorkspaceManager.instances[-1]

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["open_order_count"], 1)
        self.assertEqual(result["balances"][0]["currency"], "ACS")
        self.assertEqual(
            manager.credentials_by_key[f"workspace:{account.id}"],
            credentials,
        )
        self.assertTrue(manager.closed)
        self.assertNotIn("test-secret", str(result))

    async def test_account_check_redacts_credentials_from_errors(self) -> None:
        project = UserProject.from_dict(
            {
                "owner_email": "member@example.com",
                "asset": "ACS",
                "quote_currency": "USDC",
            }
        )
        account = UserExchangeAccount.from_dict(
            {
                "owner_email": project.owner_email,
                "project_id": project.id,
                "exchange": "coinbase",
                "symbol": "ACS/USDC",
            }
        )

        result = await check_workspace_account(
            account=account,
            project=project,
            credentials={"api_key": "test-key", "secret": "leaky-secret"},
            manager_factory=FailingWorkspaceManager,
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("[redacted]", result["error"])
        self.assertNotIn("leaky-secret", result["error"])

    async def test_bybit_account_check_includes_non_tradable_funding_wallet(
        self,
    ) -> None:
        project = UserProject.from_dict(
            {
                "owner_email": "member@example.com",
                "asset": "ACS",
                "quote_currency": "USDC",
            }
        )
        account = UserExchangeAccount.from_dict(
            {
                "owner_email": project.owner_email,
                "project_id": project.id,
                "exchange": "bybit",
                "symbol": "ACS/USDC",
            }
        )

        result = await check_workspace_account(
            account=account,
            project=project,
            credentials={"api_key": "key", "secret": "secret"},
            manager_factory=FakeBybitWorkspaceManager,
        )

        funding = next(row for row in result["balances"] if row["wallet"] == "funding")
        self.assertEqual(funding["currency"], "ACS")
        self.assertEqual(funding["total"], 500.0)
        self.assertFalse(funding["tradable"])
        self.assertFalse(
            any(
                row["wallet"] == "trading" and row["currency"] == "USDC"
                for row in result["balances"]
            )
        )

    async def test_unified_api_check_covers_every_supported_market_type(self) -> None:
        connection = UserApiConnection.from_dict(
            {
                "owner_email": "member@example.com",
                "label": "Bybit Main",
                "exchange": "bybit",
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )

        result = await check_workspace_api_connection(
            api_connection=connection,
            credentials={"api_key": "key", "secret": "secret"},
            manager_factory=FakeBybitWorkspaceManager,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["market_types"], ["spot", "swap"])
        self.assertEqual(result["market_counts"], {"spot": 2, "swap": 1})
        self.assertEqual(result["open_order_count"], 2)
        self.assertEqual(len(FakeWorkspaceManager.instances), 2)
        self.assertTrue(all(row.closed for row in FakeWorkspaceManager.instances))

    async def test_coinbase_api_check_adds_hidden_open_order_reserves(self) -> None:
        connection = UserApiConnection.from_dict(
            {
                "owner_email": "member@example.com",
                "label": "Coinbase Main",
                "exchange": "coinbase",
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )

        result = await check_workspace_api_connection(
            api_connection=connection,
            credentials={"api_key": "key", "secret": "secret"},
            manager_factory=FakeCoinbaseReserveManager,
        )

        balances = {row["currency"]: row for row in result["balances"]}
        self.assertEqual(balances["USDC"]["free"], 80.0)
        self.assertEqual(balances["USDC"]["used"], 20.0)
        self.assertEqual(balances["USDC"]["total"], 100.0)
        self.assertEqual(balances["ACS"]["free"], 900.0)
        self.assertEqual(balances["ACS"]["used"], 100.0)
        self.assertEqual(balances["ACS"]["total"], 1000.0)
        self.assertEqual(balances["USDC"]["exchange_total"], 80.0)
        self.assertEqual(balances["USDC"]["open_order_reserved"], 20.0)

    async def test_dedicated_egress_mismatch_blocks_account_check(self) -> None:
        connection = UserApiConnection.from_dict(
            {
                "owner_email": "member@example.com",
                "label": "Bybit Dedicated",
                "exchange": "bybit",
                "egress_mode": "source_ip",
                "egress_source_ip": "10.0.0.12",
                "egress_expected_ip": "203.0.113.10",
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )

        result = await check_workspace_api_connection(
            api_connection=connection,
            credentials={"api_key": "key", "secret": "secret"},
            manager_factory=EgressWorkspaceManager,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["egress_observed_ip"], "203.0.113.11")
        self.assertIn("egress IP mismatch", result["error"])
        self.assertTrue(FakeWorkspaceManager.instances[-1].closed)

    async def test_binance_unified_check_keeps_spot_and_futures_wallets(self) -> None:
        connection = UserApiConnection.from_dict(
            {
                "owner_email": "member@example.com",
                "label": "Binance Main",
                "exchange": "binance",
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )

        result = await check_workspace_api_connection(
            api_connection=connection,
            credentials={"api_key": "key", "secret": "secret"},
            manager_factory=FakeBinanceWorkspaceManager,
        )

        balances = {
            (row["currency"], row["wallet"]): row["total"] for row in result["balances"]
        }
        self.assertEqual(
            balances,
            {("USDT", "spot"): 25.0, ("USDT", "swap"): 100.0},
        )

    async def test_unified_check_prices_non_stable_balances(self) -> None:
        connection = UserApiConnection.from_dict(
            {
                "owner_email": "member@example.com",
                "label": "Binance Main",
                "exchange": "binance",
                "market_types": ["spot"],
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )

        result = await check_workspace_api_connection(
            api_connection=connection,
            credentials={"api_key": "key", "secret": "secret"},
            manager_factory=FakePricedWorkspaceManager,
        )

        bnb = next(row for row in result["balances"] if row["currency"] == "BNB")
        self.assertEqual(bnb["valuation_price"], 600.0)
        self.assertEqual(bnb["valuation_quote"], "USDT")
        self.assertEqual(bnb["valuation_symbol"], "BNB/USDT")
        self.assertEqual(result["valuation_warnings"], [])

    async def test_multi_market_exchanges_keep_spot_and_contract_wallets_separate(
        self,
    ) -> None:
        for exchange in ("gateio", "htx", "mexc"):
            with self.subTest(exchange=exchange):
                FakeWorkspaceManager.instances.clear()
                connection = UserApiConnection.from_dict(
                    {
                        "owner_email": "member@example.com",
                        "label": f"{exchange} Main",
                        "exchange": exchange,
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                )

                result = await check_workspace_api_connection(
                    api_connection=connection,
                    credentials={"api_key": "key", "secret": "secret"},
                    manager_factory=FakeBinanceWorkspaceManager,
                )

                balances = {
                    (row["currency"], row["wallet"]): row["total"]
                    for row in result["balances"]
                }
                self.assertEqual(
                    balances,
                    {("USDT", "spot"): 25.0, ("USDT", "swap"): 100.0},
                )

    async def test_kucoin_keeps_trading_funding_and_contract_wallets_separate(
        self,
    ) -> None:
        connection = UserApiConnection.from_dict(
            {
                "owner_email": "member@example.com",
                "label": "KuCoin Main",
                "exchange": "kucoin",
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )

        result = await check_workspace_api_connection(
            api_connection=connection,
            credentials={
                "api_key": "key",
                "secret": "secret",
                "passphrase": "passphrase",
            },
            manager_factory=FakeKucoinWorkspaceManager,
        )

        balances = {
            (row["currency"], row["wallet"]): row["total"]
            for row in result["balances"]
        }
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(
            balances,
            {
                ("KCS", "funding"): 2.0,
                ("USDT", "funding"): 5.0,
                ("USDT", "spot"): 25.0,
                ("USDT", "swap"): 100.0,
            },
        )
        self.assertEqual(result["balance_warnings"], [])

    async def test_account_check_service_applies_per_account_cooldown(self) -> None:
        project = UserProject.from_dict(
            {
                "owner_email": "member@example.com",
                "asset": "ACS",
                "quote_currency": "USDC",
            }
        )
        account = UserExchangeAccount.from_dict(
            {
                "owner_email": project.owner_email,
                "project_id": project.id,
                "exchange": "coinbase",
                "symbol": "ACS/USDC",
            }
        )
        service = WorkspaceAccountCheckService(cooldown_seconds=10)
        with patch(
            "arbitrage_bot.user_account_check.check_workspace_account",
            new_callable=AsyncMock,
        ) as checker:
            checker.return_value = {"status": "healthy"}
            await service.check(
                account=account,
                project=project,
                credentials={"api_key": "key", "secret": "secret"},
            )
            with self.assertRaisesRegex(RuntimeError, "wait"):
                await service.check(
                    account=account,
                    project=project,
                    credentials={"api_key": "key", "secret": "secret"},
                )


if __name__ == "__main__":
    unittest.main()
