from __future__ import annotations

import base64
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from arbitrage_bot.config import AssetLedgerConfig

from arbitrage_bot.user_account_health import (
    API_CONNECTION_ERROR_RETRY_SECONDS,
    API_CONNECTION_HEALTHY_REFRESH_SECONDS,
    ERROR_RETRY_SECONDS,
    HEALTHY_REFRESH_SECONDS,
    _NEXT_CASH_FLOW_SYNC_AT,
    _sync_workspace_cash_flows,
    refresh_workspace_accounts,
    workspace_account_check_due,
    workspace_api_connection_check_due,
)
from arbitrage_bot.user_workspace import (
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)


MASTER_KEY = base64.urlsafe_b64encode(b"h" * 32).decode("ascii")


class FakeChecker:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[str] = []

    async def check(self, *, account, **_kwargs):
        self.calls.append(account.id)
        return dict(self.result)


class WorkspaceAccountHealthTest(unittest.IsolatedAsyncioTestCase):
    def _store_with_account(
        self,
        path: Path,
    ) -> tuple[UserWorkspaceStore, UserExchangeAccount]:
        store = UserWorkspaceStore(
            path,
            master_key_env="TEST_ACCOUNT_HEALTH_KEY",
        )
        project = store.upsert_project(
            UserProject.from_dict(
                {
                    "owner_email": "trader@example.com",
                    "asset": "ACS",
                    "quote_currency": "USDT",
                    "status": "active",
                }
            )
        )
        account = store.upsert_account(
            UserExchangeAccount.from_dict(
                {
                    "owner_email": project.owner_email,
                    "project_id": project.id,
                    "label": "Bybit Main",
                    "exchange": "bybit",
                    "symbol": "ACS/USDT",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                }
            ),
            credentials={"api_key": "key", "secret": "secret"},
        )
        account = store.update_account_connection(account.id, status="healthy")
        account = store.upsert_account(
            replace(
                account,
                enabled=False,
                connection_checked_at=time.time() - HEALTHY_REFRESH_SECONDS - 1,
            )
        )
        return store, account

    async def test_due_health_refresh_enables_account_and_persists_balance(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"TEST_ACCOUNT_HEALTH_KEY": MASTER_KEY},
            ),
        ):
            store, account = self._store_with_account(Path(tmp) / "workspace.sqlite3")
            checker = FakeChecker(
                {
                    "status": "healthy",
                    "latency_ms": 12.0,
                    "balances": [
                        {
                            "currency": "USDT",
                            "free": 99.0,
                            "used": 1.0,
                            "total": 100.0,
                        }
                    ],
                    "open_order_count": 1,
                }
            )
            result = await refresh_workspace_accounts(store, checker)
            refreshed = store.get_account(account.id)

        self.assertEqual(result["refreshed_count"], 1)
        self.assertEqual(result["enabled_count"], 1)
        self.assertEqual(checker.calls, [account.id])
        self.assertIsNotNone(refreshed)
        self.assertTrue(refreshed.enabled)
        self.assertEqual(refreshed.balance_snapshot[0]["total"], 100.0)
        self.assertEqual(refreshed.open_order_count, 1)

    async def test_refresh_intervals_prevent_healthy_account_polling_churn(
        self,
    ) -> None:
        now = time.time()
        healthy = UserExchangeAccount.from_dict(
            {
                "owner_email": "trader@example.com",
                "project_id": "project-acs",
                "exchange": "coinbase",
                "connection_status": "healthy",
                "connection_checked_at": now,
            }
        )
        failed = UserExchangeAccount.from_dict(
            {
                **healthy.to_dict(),
                "connection_status": "error",
            }
        )

        self.assertFalse(
            workspace_account_check_due(
                healthy,
                now=now + HEALTHY_REFRESH_SECONDS - 1,
            )
        )
        self.assertTrue(
            workspace_account_check_due(
                healthy,
                now=now + HEALTHY_REFRESH_SECONDS,
            )
        )
        self.assertFalse(
            workspace_account_check_due(
                failed,
                now=now + ERROR_RETRY_SECONDS - 1,
            )
        )
        self.assertTrue(
            workspace_account_check_due(
                failed,
                now=now + ERROR_RETRY_SECONDS,
            )
        )

    async def test_global_api_balances_refresh_more_often_than_market_bindings(
        self,
    ) -> None:
        now = time.time()
        healthy = UserApiConnection.from_dict(
            {
                "owner_email": "trader@example.com",
                "exchange": "binance",
                "connection_status": "healthy",
                "connection_checked_at": now,
            }
        )
        failed = replace(healthy, connection_status="error")

        self.assertLess(API_CONNECTION_HEALTHY_REFRESH_SECONDS, HEALTHY_REFRESH_SECONDS)
        self.assertFalse(
            workspace_api_connection_check_due(
                healthy,
                now=now + API_CONNECTION_HEALTHY_REFRESH_SECONDS - 1,
            )
        )
        self.assertTrue(
            workspace_api_connection_check_due(
                healthy,
                now=now + API_CONNECTION_HEALTHY_REFRESH_SECONDS,
            )
        )
        self.assertFalse(
            workspace_api_connection_check_due(
                failed,
                now=now + API_CONNECTION_ERROR_RETRY_SECONDS - 1,
            )
        )
        self.assertTrue(
            workspace_api_connection_check_due(
                failed,
                now=now + API_CONNECTION_ERROR_RETRY_SECONDS,
            )
        )

    async def test_workspace_cash_flows_share_one_spot_swap_ledger_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = str(Path(tmp) / "asset-ledger.sqlite3")
            ledger_cfg = AssetLedgerConfig(
                enabled=True,
                path=ledger_path,
                cash_flow_interval_seconds=0.0,
            )
            connection = UserApiConnection.from_dict(
                {
                    "id": "connection-test",
                    "owner_email": "trader@example.com",
                    "exchange": "bybit",
                    "market_type": "spot",
                    "api_variant": "default",
                }
            )
            manager = MagicMock()
            manager.cash_flow_capabilities.return_value = [
                "deposit",
                "withdrawal",
            ]
            manager.fetch_cash_flows = AsyncMock(
                return_value={
                    "supported_types": ["deposit", "withdrawal"],
                    "transactions": [
                        {
                            "id": "deposit-1",
                            "type": "deposit",
                            "currency": "USDT",
                            "amount": 100.0,
                            "timestamp": time.time() * 1000.0,
                            "status": "ok",
                        }
                    ],
                    "errors": [],
                }
            )
            manager.close = AsyncMock()
            _NEXT_CASH_FLOW_SYNC_AT.pop(connection.id, None)
            with patch(
                "arbitrage_bot.user_account_health.ExchangeManager",
                return_value=manager,
            ):
                await _sync_workspace_cash_flows(
                    connection,
                    {"api_key": "key", "secret": "secret"},
                    ledger_cfg,
                )
                await _sync_workspace_cash_flows(
                    connection,
                    {"api_key": "key", "secret": "secret"},
                    ledger_cfg,
                )

            with sqlite3.connect(ledger_path) as db:
                self.assertEqual(
                    db.execute("select count(*) from cash_flows").fetchone()[0],
                    1,
                )
                aliases = dict(
                    db.execute(
                        """
                        select account_key, canonical_account_key
                        from cash_flow_account_aliases
                        """
                    ).fetchall()
                )
            self.assertEqual(
                aliases["workspace:connection-test:swap"],
                "connection-test",
            )
            self.assertEqual(manager.fetch_cash_flows.await_count, 1)
