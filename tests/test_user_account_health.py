from __future__ import annotations

import base64
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.user_account_health import (
    ERROR_RETRY_SECONDS,
    HEALTHY_REFRESH_SECONDS,
    refresh_workspace_accounts,
    workspace_account_check_due,
)
from arbitrage_bot.user_workspace import (
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

    async def test_due_health_refresh_enables_account_and_persists_balance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"TEST_ACCOUNT_HEALTH_KEY": MASTER_KEY},
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

    async def test_refresh_intervals_prevent_healthy_account_polling_churn(self) -> None:
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
