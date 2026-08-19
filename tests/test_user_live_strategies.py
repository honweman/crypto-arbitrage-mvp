from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import load_config
from arbitrage_bot.user_live_strategies import user_market_maker_bindings
from arbitrage_bot.user_strategies import UserStrategy
from arbitrage_bot.user_workspace import (
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)


class UserLiveStrategyTest(unittest.TestCase):
    def _ready_account(
        self,
        store: UserWorkspaceStore,
        *,
        owner: str,
        label: str,
    ) -> tuple[UserProject, UserExchangeAccount]:
        project = store.upsert_project(
            UserProject.from_dict(
                {
                    "owner_email": owner,
                    "name": "ACS/USDC",
                    "asset": "ACS",
                    "quote_currency": "USDC",
                    "status": "active",
                }
            )
        )
        connection = store.upsert_api_connection(
            UserApiConnection.from_dict(
                {
                    "owner_email": owner,
                    "label": label,
                    "exchange": "coinbase",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                }
            ),
            credentials={"api_key": f"{label}-key", "secret": "secret"},
        )
        account = store.upsert_account(
            UserExchangeAccount.from_dict(
                {
                    "owner_email": owner,
                    "project_id": project.id,
                    "connection_id": connection.id,
                    "label": label,
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDC",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                }
            ),
            credentials={"api_key": f"{label}-key", "secret": "secret"},
        )
        store.update_api_connection_check(
            connection.id,
            status="healthy",
            check={"balances": [], "open_order_count": 0},
        )
        account = store.update_account_connection(
            account.id,
            status="healthy",
            check={"balances": [], "open_order_count": 0},
        )
        return project, account

    def test_live_market_maker_is_owner_isolated_and_fully_mapped(self) -> None:
        key = base64.urlsafe_b64encode(b"l" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_LIVE_MASTER_KEY": key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_LIVE_MASTER_KEY",
            )
            project, account = self._ready_account(
                store,
                owner="owner@example.com",
                label="Owner Coinbase",
            )
            self._ready_account(
                store,
                owner="other@example.com",
                label="Other Coinbase",
            )
            strategy = store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "owner_email": "owner@example.com",
                        "project_id": project.id,
                        "name": "Owner ACS MM",
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                        "mode": "live",
                        "enabled": True,
                        "parameters": {
                            "levels": 3,
                            "quote_per_level": 2.0,
                            "depth_shape": "flat",
                            "reprice_threshold_bps": 8.0,
                            "max_cancels_per_cycle": 4,
                            "inventory_control_enabled": True,
                            "inventory_target_base": 100.0,
                        },
                        "risk": {
                            "max_order_quote": 2.0,
                            "max_total_quote": 12.0,
                            "max_open_orders": 6,
                        },
                    }
                )
            )
            bindings = user_market_maker_bindings(
                load_config("config.acs.example.json"),
                store,
            )

        self.assertEqual(len(bindings), 1)
        binding = bindings[0]
        self.assertEqual(binding.strategy_id, strategy.id)
        self.assertEqual(binding.owner_email, "owner@example.com")
        self.assertEqual(binding.config.levels, 3)
        self.assertEqual(binding.config.depth_shape, "flat")
        self.assertEqual(binding.config.reprice_threshold_bps, 8.0)
        self.assertEqual(binding.config.max_cancels_per_cycle, 4)
        self.assertTrue(binding.config.inventory_control_enabled)
        self.assertEqual(binding.config.inventory_target_base, 100.0)
        self.assertEqual(binding.runtime_config.risk.allowed_exchanges, [binding.config.exchange])
        self.assertEqual(
            {row.credential_owner_email for row in binding.runtime_config.spot_exchanges},
            {"owner@example.com"},
        )

    def test_legacy_paper_market_maker_is_never_promoted(self) -> None:
        key = base64.urlsafe_b64encode(b"p" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_LIVE_MASTER_KEY": key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_LIVE_MASTER_KEY",
            )
            project, account = self._ready_account(
                store,
                owner="owner@example.com",
                label="Owner Coinbase",
            )
            store.upsert_strategy(
                UserStrategy.from_dict(
                    {
                        "owner_email": "owner@example.com",
                        "project_id": project.id,
                        "strategy_type": "market_maker",
                        "account_ids": [account.id],
                        "mode": "paper",
                        "enabled": True,
                    }
                )
            )

            bindings = user_market_maker_bindings(
                load_config("config.acs.example.json"),
                store,
            )

        self.assertEqual(bindings, [])
