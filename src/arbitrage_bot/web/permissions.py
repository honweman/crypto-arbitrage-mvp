from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .users import WebUser


PERMISSION_MODEL = "account_owner_v1"

OWNER_CAPABILITIES = frozenset(
    {
        "account.read",
        "account.manage",
        "account.trade",
        "risk.read",
        "risk.manage",
        "strategy.read",
        "strategy.manage",
        "security.manage",
    }
)
ADMIN_CAPABILITIES = frozenset(
    {
        *OWNER_CAPABILITIES,
        "platform.read",
        "platform.manage",
        "user.manage",
    }
)


def user_capabilities(user: WebUser | None) -> frozenset[str]:
    # Legacy password sessions are retained for local compatibility only. They
    # have platform capabilities but no owner identity and therefore cannot
    # manage another user's encrypted account records.
    if user is None:
        return ADMIN_CAPABILITIES
    return ADMIN_CAPABILITIES if user.role == "admin" else OWNER_CAPABILITIES


def require_capability(user: WebUser | None, capability: str) -> None:
    if capability not in user_capabilities(user):
        raise PermissionError(f"{capability} permission is required for this action")


def require_resource_owner(
    user: WebUser | None,
    owner_email: str,
    *,
    admin_override: bool = False,
) -> None:
    owner = str(owner_email or "").strip().lower()
    if user is None:
        if admin_override:
            return
        raise PermissionError("a registered account owner is required")
    if owner == user.email:
        return
    if admin_override and user.role == "admin":
        return
    raise PermissionError("this resource belongs to another user")


def require_owned_exchange(user: WebUser | None, exchange: Any) -> None:
    if user is None or user.role == "admin":
        return
    owner = str(getattr(exchange, "credential_owner_email", "") or "").lower()
    if owner != user.email:
        raise PermissionError("the selected exchange account is not owned by this user")


def _base_asset(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].split(":", 1)[0].strip().upper()


def build_permission_scope(
    user: WebUser,
    workspace: dict[str, Any] | None,
) -> dict[str, Any]:
    workspace = workspace if isinstance(workspace, dict) else {}
    connections = [
        row
        for row in workspace.get("connections", []) or []
        if isinstance(row, dict)
        and str(row.get("owner_email") or user.email).strip().lower() == user.email
    ]
    trading_connections = [row for row in connections if bool(row.get("live_enabled"))]
    symbols = sorted(
        {
            str(market.get("symbol") or "").strip().upper()
            for row in connections
            for market in row.get("markets", []) or []
            if isinstance(market, dict) and str(market.get("symbol") or "").strip()
        }
    )
    assets = sorted({_base_asset(symbol) for symbol in symbols if _base_asset(symbol)})
    exchanges = sorted(
        {str(row.get("exchange") or "").strip().lower() for row in connections}
        - {""}
    )
    market_types = sorted(
        {
            str(market_type or "").strip().lower()
            for row in connections
            for market_type in (
                row.get("market_types") or [row.get("market_type") or "spot"]
            )
        }
        - {""}
    )
    return {
        "model": PERMISSION_MODEL,
        "owner_email": user.email,
        "role": user.role,
        "capabilities": sorted(user_capabilities(user)),
        "connection_ids": [str(row.get("id") or "") for row in connections],
        "trading_connection_ids": [
            str(row.get("id") or "") for row in trading_connections
        ],
        "exchanges": exchanges,
        "market_types": market_types,
        "symbols": symbols,
        "assets": assets,
        "scope_source": "owned_api_connections",
        "legacy_allowed_assets": list(user.allowed_assets),
        "summary": {
            "connection_count": len(connections),
            "trading_connection_count": len(trading_connections),
            "selected_symbol_count": len(symbols),
            "all_supported_markets_connection_count": sum(
                row.get("market_scope") == "all_supported_markets"
                for row in connections
            ),
        },
    }


def permission_assets(scope: dict[str, Any] | None) -> set[str]:
    if not isinstance(scope, dict):
        return set()
    return {
        str(asset or "").strip().upper()
        for asset in scope.get("assets", []) or []
        if str(asset or "").strip()
    }


def owns_any_connection(
    user: WebUser,
    connection_ids: Iterable[str],
    workspace: dict[str, Any] | None,
) -> bool:
    scope = build_permission_scope(user, workspace)
    owned = set(scope["connection_ids"])
    return any(str(connection_id or "") in owned for connection_id in connection_ids)
