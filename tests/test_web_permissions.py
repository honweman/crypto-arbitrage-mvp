from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from arbitrage_bot.config import ExchangeConfig
from arbitrage_bot.web.permissions import (
    PERMISSION_MODEL,
    build_permission_scope,
    require_capability,
    require_resource_owner,
)
from arbitrage_bot.web.users import WebUser
from arbitrage_bot.workspace_runtime import workspace_exchange_allowed_symbols


def make_user(*, role: str = "user") -> WebUser:
    return WebUser(
        email="trader@example.com",
        username="trader",
        password_hash="unused",
        totp_secret="unused",
        role=role,
        allowed_assets=["SWAP"],
    )


def test_permission_scope_comes_from_owned_connections_not_legacy_assets() -> None:
    user = make_user()
    workspace = {
        "connections": [
            {
                "id": "coinbase-main",
                "owner_email": user.email,
                "exchange": "coinbase",
                "market_types": ["spot"],
                "market_scope": "selected_markets",
                "live_enabled": True,
                "markets": [{"symbol": "ACS/USDC", "market_type": "spot"}],
            },
            {
                "id": "binance-main",
                "owner_email": user.email,
                "exchange": "binance",
                "market_types": ["spot", "swap"],
                "market_scope": "all_supported_markets",
                "live_enabled": False,
                "markets": [],
            },
            {
                "id": "other-user",
                "owner_email": "other@example.com",
                "exchange": "bybit",
                "market_types": ["spot"],
                "live_enabled": True,
                "markets": [{"symbol": "BTC/USDT"}],
            },
        ]
    }

    scope = build_permission_scope(user, workspace)

    assert scope["model"] == PERMISSION_MODEL
    assert scope["connection_ids"] == ["coinbase-main", "binance-main"]
    assert scope["trading_connection_ids"] == ["coinbase-main"]
    assert scope["exchanges"] == ["binance", "coinbase"]
    assert scope["market_types"] == ["spot", "swap"]
    assert scope["symbols"] == ["ACS/USDC"]
    assert scope["assets"] == ["ACS"]
    assert scope["legacy_allowed_assets"] == ["SWAP"]


def test_owner_capabilities_and_resource_boundary_are_centralized() -> None:
    user = make_user()
    require_capability(user, "account.trade")
    require_resource_owner(user, user.email)

    with pytest.raises(PermissionError):
        require_capability(user, "platform.manage")
    with pytest.raises(PermissionError):
        require_resource_owner(user, "other@example.com")

    admin = make_user(role="admin")
    require_capability(admin, "platform.manage")
    with pytest.raises(PermissionError):
        require_resource_owner(admin, "other@example.com")
    require_resource_owner(admin, "other@example.com", admin_override=True)


def test_selected_account_markets_are_the_runtime_symbol_scope() -> None:
    exchange = ExchangeConfig(
        id="binance",
        label="workspace:binance-main:swap",
        market_type="swap",
        credential_connection_id="binance-main",
        credential_owner_email="trader@example.com",
        credential_store_path="/tmp/workspace.sqlite3",
        credential_master_key_env="TEST_KEY",
    )
    project = SimpleNamespace(id="btc-project", status="active")
    selected = SimpleNamespace(
        connection_id="binance-main",
        market_type="swap",
        enabled=True,
        project_id="btc-project",
        symbol="BTC/USDT:USDT",
    )
    ignored = SimpleNamespace(
        connection_id="binance-main",
        market_type="spot",
        enabled=True,
        project_id="btc-project",
        symbol="ETH/USDT",
    )

    with patch("arbitrage_bot.workspace_runtime.UserWorkspaceStore") as store_cls:
        store = store_cls.return_value
        store.list_projects.return_value = [project]
        store.list_accounts.return_value = [selected, ignored]
        assert workspace_exchange_allowed_symbols(exchange) == {"BTC/USDT:USDT"}

        selected.enabled = False
        assert workspace_exchange_allowed_symbols(exchange) == set()

        store.list_accounts.return_value = []
        assert workspace_exchange_allowed_symbols(exchange) is None
