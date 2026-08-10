from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.config import ExchangeConfig, SpotMarketConfig, load_config
from arbitrage_bot.user_workspace import (
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)
from arbitrage_bot.web_config import (
    _rebalance_symbols_by_exchange,
    auto_buy_sell_exchanges,
    slow_execution_accounts,
)
from arbitrage_bot.web.state import MonitorState
from arbitrage_bot.workspace_runtime import (
    WorkspaceRuntimeAccounts,
    build_workspace_runtime_accounts,
    isolated_workspace_runtime_config,
    merge_workspace_runtime_accounts,
)


class WorkspaceRuntimeAccountsTest(unittest.TestCase):
    def test_owner_runtime_includes_healthy_unbound_spot_and_swap_account(self) -> None:
        master_key = base64.urlsafe_b64encode(b"u" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_MASTER_KEY": master_key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            connection = store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "owner@example.com",
                        "label": "Binance Main",
                        "exchange": "binance",
                        "market_types": ["spot", "swap"],
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            store.update_api_connection_check(
                connection.id,
                status="healthy",
                check={"balances": [], "open_order_count": 0},
            )

            workspace = build_workspace_runtime_accounts(
                store,
                owner_emails=["owner@example.com"],
                include_unbound_market_types=True,
            )
            cfg = isolated_workspace_runtime_config(
                load_config("config.acs.example.json"),
                workspace,
                risk_profile=store.risk_profile("owner@example.com"),
            )

        self.assertEqual(len(cfg.spot_exchanges), 1)
        self.assertEqual(len(cfg.derivative_exchanges), 1)
        self.assertEqual(cfg.spot_exchanges[0].credential_owner_email, "owner@example.com")
        self.assertEqual(cfg.derivative_exchanges[0].market_type, "swap")
        self.assertEqual(
            cfg.risk.allowed_exchanges,
            [cfg.spot_exchanges[0].key, cfg.derivative_exchanges[0].key],
        )
        self.assertEqual(cfg.risk.allowed_symbols, [])
        self.assertEqual(cfg.risk.blocked_symbols, [])

    def test_runtime_risk_overrides_preserve_new_workspace_accounts(self) -> None:
        cfg = load_config("config.acs.example.json")
        runtime_key = "workspace:connection-gate:spot"
        workspace = WorkspaceRuntimeAccounts(
            spot_exchanges=(
                ExchangeConfig(
                    id="gateio",
                    label=runtime_key,
                    display_label="Gate Main · SPOT",
                ),
            ),
            spot_markets=(
                SpotMarketConfig(
                    asset="ACS",
                    exchange=runtime_key,
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "web_runtime_overrides.json"
            store_path.write_text(
                json.dumps(
                    {
                        "risk_overrides": {
                            "account_enabled": {"coinbase-spot": True}
                        }
                    }
                ),
                encoding="utf-8",
            )
            async def build_runtime_config():
                state = MonitorState(
                    cfg,
                    1.0,
                    runtime_store_path=str(store_path),
                    workspace_runtime_accounts=workspace,
                )
                return state._runtime_config_unlocked(cfg)

            runtime_cfg = asyncio.run(build_runtime_config())

        self.assertTrue(runtime_cfg.risk.account_enabled[runtime_key])
        self.assertTrue(runtime_cfg.risk.account_enabled["coinbase-spot"])

    def test_runtime_risk_overrides_respect_explicit_workspace_disable(self) -> None:
        cfg = load_config("config.acs.example.json")
        runtime_key = "workspace:connection-gate:spot"
        workspace = WorkspaceRuntimeAccounts(
            spot_exchanges=(ExchangeConfig(id="gateio", label=runtime_key),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "web_runtime_overrides.json"
            store_path.write_text(
                json.dumps(
                    {
                        "risk_overrides": {
                            "account_enabled": {runtime_key: False}
                        }
                    }
                ),
                encoding="utf-8",
            )
            async def build_runtime_config():
                state = MonitorState(
                    cfg,
                    1.0,
                    runtime_store_path=str(store_path),
                    workspace_runtime_accounts=workspace,
                )
                return state._runtime_config_unlocked(cfg)

            runtime_cfg = asyncio.run(build_runtime_config())

        self.assertFalse(runtime_cfg.risk.account_enabled[runtime_key])

    def test_gateio_and_htx_bindings_feed_all_core_spot_strategies(self) -> None:
        master_key = base64.urlsafe_b64encode(b"w" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_MASTER_KEY": master_key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "owner_email": "admin@example.com",
                        "name": "ACS/USDT",
                        "asset": "ACS",
                        "quote_currency": "USDT",
                        "status": "active",
                    }
                )
            )
            for exchange, label in (("gateio", "Gate Main"), ("htx", "HTX Main")):
                connection = store.upsert_api_connection(
                    UserApiConnection.from_dict(
                        {
                            "owner_email": "admin@example.com",
                            "label": label,
                            "exchange": exchange,
                            "withdrawal_disabled_confirmed": True,
                            "trade_permission_confirmed": True,
                        }
                    ),
                    credentials={"api_key": f"{exchange}-key", "secret": "secret"},
                )
                account = store.upsert_account(
                    UserExchangeAccount.from_dict(
                        {
                            "owner_email": "admin@example.com",
                            "project_id": project.id,
                            "connection_id": connection.id,
                            "label": label,
                            "exchange": exchange,
                            "market_type": "spot",
                            "symbol": "ACS/USDT",
                            "withdrawal_disabled_confirmed": True,
                            "trade_permission_confirmed": True,
                        }
                    ),
                    credentials={"api_key": f"{exchange}-key", "secret": "secret"},
                )
                store.update_api_connection_check(
                    connection.id,
                    status="healthy",
                    check={"balances": [], "open_order_count": 0},
                )
                store.update_account_connection(
                    account.id,
                    status="healthy",
                    check={"balances": [], "open_order_count": 0},
                )

            workspace = build_workspace_runtime_accounts(
                store,
                owner_emails=["admin@example.com"],
            )
            cfg = merge_workspace_runtime_accounts(
                load_config("config.acs.example.json"),
                workspace,
            )

        dynamic = [row for row in cfg.spot_exchanges if row.key.startswith("workspace:")]
        self.assertEqual({row.id for row in dynamic}, {"gateio", "htx"})
        self.assertEqual(
            {row.display_label for row in dynamic},
            {"Gate Main · SPOT", "HTX Main · SPOT"},
        )
        dynamic_keys = {row.key for row in dynamic}
        self.assertEqual(
            {key: cfg.risk.account_enabled.get(key) for key in dynamic_keys},
            {key: True for key in dynamic_keys},
        )
        self.assertEqual(
            {row.exchange for row in cfg.spot_markets if row.exchange in dynamic_keys},
            dynamic_keys,
        )
        self.assertTrue(
            dynamic_keys.issubset({row.key for row in auto_buy_sell_exchanges(cfg)})
        )
        rebalance_accounts = slow_execution_accounts(
            cfg.spot_exchanges,
            _rebalance_symbols_by_exchange(cfg),
            spot_markets=cfg.spot_markets,
        )
        account_by_key = {row["key"]: row for row in rebalance_accounts}
        for key in dynamic_keys:
            self.assertEqual(account_by_key[key]["symbols"], ["ACS/USDT"])
            self.assertNotIn("workspace:", account_by_key[key]["label"])
            self.assertNotIn(
                "workspace:",
                account_by_key[key]["markets"][0]["exchange_label"],
            )

    def test_imported_legacy_connection_is_not_duplicated(self) -> None:
        master_key = base64.urlsafe_b64encode(b"x" * 32).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TEST_MASTER_KEY": master_key},
            clear=False,
        ):
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3",
                master_key_env="TEST_MASTER_KEY",
            )
            store.upsert_api_connection(
                UserApiConnection.from_dict(
                    {
                        "owner_email": "admin@example.com",
                        "label": "Coinbase Main",
                        "exchange": "coinbase",
                        "runtime_keys": ["coinbase-spot"],
                        "withdrawal_disabled_confirmed": True,
                        "trade_permission_confirmed": True,
                    }
                ),
                credentials={"api_key": "key", "secret": "secret"},
            )
            workspace = build_workspace_runtime_accounts(
                store,
                owner_emails=["admin@example.com"],
            )

        self.assertEqual(workspace.spot_exchanges, ())
        self.assertEqual(workspace.derivative_exchanges, ())
        self.assertEqual(workspace.spot_markets, ())

    def test_explicit_workspace_risk_disable_is_preserved(self) -> None:
        cfg = load_config("config.acs.example.json")
        dynamic_key = "workspace:connection-gate:spot"
        exchange = next(row for row in cfg.spot_exchanges if row.market_type == "spot")
        dynamic_exchange = replace(
            exchange,
            id="gateio",
            label=dynamic_key,
            display_label="Gate Main · SPOT",
        )
        cfg = replace(
            cfg,
            risk=replace(
                cfg.risk,
                account_enabled={**cfg.risk.account_enabled, dynamic_key: False},
            ),
        )
        merged = merge_workspace_runtime_accounts(
            cfg,
            WorkspaceRuntimeAccounts(spot_exchanges=(dynamic_exchange,)),
        )

        self.assertFalse(merged.risk.account_enabled[dynamic_key])
