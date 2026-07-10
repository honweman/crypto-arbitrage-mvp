from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.user_account_check import (
    WorkspaceAccountCheckService,
    check_workspace_account,
    discover_workspace_markets,
    workspace_exchange_config,
)
from arbitrage_bot.user_workspace import UserExchangeAccount, UserProject


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


class FailingWorkspaceManager(FakeWorkspaceManager):
    async def fetch_balance(self, _cfg):
        secret = next(iter(self.credentials_by_key.values()))["secret"]
        raise RuntimeError(f"authentication failed for {secret}")


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

        self.assertEqual(upbit.options["hostname"], "id-api.upbit.com")
        self.assertEqual(bithumb.options["private_api"], "v2.0")
        self.assertEqual(binance.id, "binanceusdm")

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

    async def test_account_check_uses_direct_credentials_and_returns_safe_summary(self) -> None:
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
