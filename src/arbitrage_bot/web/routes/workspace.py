from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import replace
from typing import Any

from aiohttp import web

from ..constants import LIVE_MARKET_MAKER_CONFIRMATION
from ..users import (
    WebUser,
)
from ..user_scope import (
    _base_asset_from_symbol,
    _require_admin_user,
)

from ..permissions import (
    require_capability,
    require_resource_owner,
)
from ..security import (
    _owner_email_from_payload,
    _request_user,
    _require_owner_or_admin,
    _strategy_center_store,
    _user_paper_store,
    _user_store,
    _user_workspace_store,
    _workspace_account_checker,
    _workspace_market_discovery,
    write_web_audit_event,
)

from ...config import (
    BotConfig,
)
from ...dex_venues import probe_dex_venue
from ...hyperliquid_auth import recover_authorizer, submit_agent_authorization
from ...strategy_center import (
    StrategyInstance,
    UserApiAccount,
)
from ...venue_health import (
    refresh_venue_connections,
)
from ...user_backtesting import UserBacktestService
from ...user_strategies import LIVE_USER_STRATEGY_TYPES, UserStrategy
from ...user_workspace import (
    UserApiConnection,
    UserExchangeAccount,
    UserProject,
    UserRiskProfile,
    UserWorkspaceStore,
    account_connection_is_fresh,
    api_connection_is_fresh,
    required_credentials_for_exchange,
)
from ...workspace_runtime import (
    build_workspace_runtime_accounts,
)


from ..core import build_strategy_center_payload, build_user_workspace_payload


def _strategy_center_response_payload(
    request: web.Request,
    cfg: BotConfig,
) -> dict[str, Any]:
    return build_strategy_center_payload(
        cfg,
        _strategy_center_store(request),
        user=_request_user(request),
    )


def _strategy_center_existing_row(
    rows: list[dict[str, Any]],
    row_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            return row
    raise ValueError(f"{label} not found: {row_id}")


def _strategy_center_optional_row(
    rows: list[dict[str, Any]],
    row_id: str,
) -> dict[str, Any] | None:
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            return row
    return None


def _strategy_payload_from_request(
    payload: dict[str, Any],
    *,
    user: WebUser | None,
    existing: dict[str, Any] | None = None,
) -> StrategyInstance:
    raw = dict(existing or {})
    raw.update(
        payload.get("strategy")
        if isinstance(payload.get("strategy"), dict)
        else payload
    )
    raw["owner_email"] = _owner_email_from_payload(raw, user)
    strategy = StrategyInstance.from_dict(raw)
    _require_owner_or_admin(user, strategy.owner_email)
    return strategy


def _api_account_payload_from_request(
    payload: dict[str, Any],
    *,
    user: WebUser | None,
    existing: dict[str, Any] | None = None,
) -> UserApiAccount:
    raw = dict(existing or {})
    raw.update(
        payload.get("account") if isinstance(payload.get("account"), dict) else payload
    )
    raw["owner_email"] = _owner_email_from_payload(raw, user)
    account = UserApiAccount.from_dict(raw)
    _require_owner_or_admin(user, account.owner_email)
    return account


def _require_workspace_user(user: WebUser | None) -> WebUser:
    if user is None:
        raise PermissionError("registered user account is required")
    return user


def _user_backtest_service(request: web.Request) -> UserBacktestService:
    return request.app["user_backtest_service"]


def _workspace_owner(
    raw: dict[str, Any],
    *,
    user: WebUser,
    existing_owner: str = "",
) -> str:
    owner = existing_owner or str(raw.get("owner_email") or user.email).strip().lower()
    require_resource_owner(user, owner)
    return owner


def _require_workspace_owner(user: WebUser, owner_email: str) -> None:
    require_resource_owner(user, owner_email)


def _workspace_connection_accounts(
    store: UserWorkspaceStore,
    *,
    user: WebUser,
    connection_id: str,
) -> list[UserExchangeAccount]:
    key = str(connection_id or "").strip()
    rows = [
        account
        for account in store.list_accounts(owner_email=user.email, is_admin=False)
        if account.connection_id == key
    ]
    for account in rows:
        _require_workspace_owner(user, account.owner_email)
    return rows


async def _sync_workspace_connection(
    request: web.Request,
    *,
    user: WebUser,
    raw: dict[str, Any],
    credentials: dict[str, Any] | None,
) -> tuple[str, list[UserExchangeAccount], list[str]]:
    store = _user_workspace_store(request)
    connection_id = str(raw.get("connection_id") or "").strip()
    exchange = str(raw.get("exchange") or "").strip().lower()
    market_type = str(raw.get("market_type") or "").strip().lower()
    api_variant = str(raw.get("api_variant") or "").strip().lower()
    label = str(raw.get("label") or "").strip()
    existing_connection = (
        store.get_api_connection(connection_id) if connection_id else None
    )
    existing_rows = (
        _workspace_connection_accounts(
            store,
            user=user,
            connection_id=connection_id,
        )
        if connection_id
        else []
    )
    if connection_id and existing_connection is None and not existing_rows:
        raise ValueError(f"API connection not found: {connection_id}")
    if not connection_id and label:
        connection_candidates = [
            item
            for item in store.list_api_connections(
                owner_email=user.email,
                is_admin=False,
            )
            if item.exchange == exchange
            and (not api_variant or item.api_variant == api_variant)
            and item.label.casefold() == label.casefold()
        ]
        if connection_candidates:
            newest = max(connection_candidates, key=lambda item: item.updated_at)
            existing_connection = newest
            connection_id = newest.id
            existing_rows = _workspace_connection_accounts(
                store,
                user=user,
                connection_id=connection_id,
            )
        else:
            candidates = [
                account
                for account in store.list_accounts(
                    owner_email=user.email,
                    is_admin=False,
                )
                if account.exchange == exchange
                and (not api_variant or account.api_variant == api_variant)
                and account.label.casefold() == label.casefold()
            ]
            newest = (
                max(candidates, key=lambda account: account.updated_at)
                if candidates
                else None
            )
            if newest is not None:
                connection_id = newest.connection_id
                existing_connection = store.get_api_connection(connection_id)
                existing_rows = _workspace_connection_accounts(
                    store,
                    user=user,
                    connection_id=connection_id,
                )
    if not connection_id:
        connection_id = f"connection-{secrets.token_hex(6)}"

    if not label:
        label = (
            existing_connection.label
            if existing_connection is not None
            else store.suggest_api_connection_label(
                owner_email=user.email,
                exchange=exchange,
            )
        )

    projects = store.list_projects(owner_email=user.email, is_admin=False)

    if exchange == "hyperliquid":
        raise ValueError(
            "connect Hyperliquid from Wallets & Decentralized Venues so its "
            "agent authorization can be verified"
        )
    if existing_connection is not None and (
        existing_connection.exchange != exchange
        or (api_variant and existing_connection.api_variant != api_variant)
    ):
        raise ValueError(
            "exchange and API region cannot be changed on an existing "
            "connection; add a new connection instead"
        )
    if not market_type:
        market_type = (
            existing_connection.market_type
            if existing_connection is not None
            else existing_rows[0].market_type
            if existing_rows
            else "spot"
        )
    if existing_connection is not None and not api_variant:
        api_variant = existing_connection.api_variant
    elif existing_rows and not api_variant:
        api_variant = existing_rows[0].api_variant
    supplied = {
        str(key): str(value).strip()
        for key, value in (credentials or {}).items()
        if str(value or "").strip()
    }
    connection_raw = existing_connection.to_dict() if existing_connection else {}
    connection_raw.update(
        {
            "id": connection_id,
            "owner_email": user.email,
            "label": label,
            "exchange": exchange,
            "market_type": market_type,
            "market_types": raw.get("market_types"),
            "api_variant": api_variant,
            "egress_mode": raw.get(
                "egress_mode",
                existing_connection.egress_mode if existing_connection else "default",
            ),
            "egress_source_ip": raw.get(
                "egress_source_ip",
                existing_connection.egress_source_ip if existing_connection else "",
            ),
            "egress_expected_ip": raw.get(
                "egress_expected_ip",
                existing_connection.egress_expected_ip if existing_connection else "",
            ),
            "withdrawal_disabled_confirmed": bool(
                raw.get(
                    "withdrawal_disabled_confirmed",
                    existing_connection.withdrawal_disabled_confirmed
                    if existing_connection
                    else False,
                )
            ),
            "trade_permission_confirmed": bool(
                raw.get(
                    "trade_permission_confirmed",
                    existing_connection.trade_permission_confirmed
                    if existing_connection
                    else False,
                )
            ),
        }
    )
    connection_candidate = UserApiConnection.from_dict(connection_raw)
    staged_egress_warning = ""
    if existing_connection is None:
        same_exchange_accounts = [
            row
            for row in store.list_api_connections(
                owner_email=user.email,
                is_admin=False,
            )
            if row.exchange == connection_candidate.exchange
        ]
        if same_exchange_accounts:
            staged_egress_warning = (
                f"saved as inactive: {connection_candidate.exchange} already has "
                "another API account; assign and verify a unique public IP for each "
                "account before enabling this one"
            )
    api_connection = store.upsert_api_connection(
        connection_candidate,
        credentials=supplied or None,
    )
    matches: list[tuple[UserProject, dict[str, Any], str]] = []
    warnings: list[str] = []
    if staged_egress_warning:
        warnings.append(staged_egress_warning)
    selected_market_rows = raw.get("markets")
    replace_markets = bool(raw.get("replace_markets")) and isinstance(
        selected_market_rows, list
    )
    if replace_markets:
        if len(selected_market_rows) > 100:
            raise ValueError("an account supports at most 100 selected trading pairs")
        projects_by_pair = {
            (project.asset, project.quote_currency): project for project in projects
        }
        seen_markets: set[tuple[str, str]] = set()
        for selected in selected_market_rows:
            if not isinstance(selected, dict):
                raise ValueError("selected markets must be objects")
            selected_type = str(selected.get("market_type") or "spot").lower()
            if selected_type not in api_connection.market_types:
                raise ValueError(
                    f"{exchange} does not support selected market type {selected_type}"
                )
            selected_symbol = str(selected.get("symbol") or "").strip().upper()
            if "/" not in selected_symbol:
                raise ValueError(f"invalid trading pair: {selected_symbol}")
            selected_asset = selected_symbol.split("/", 1)[0]
            try:
                discovered, _ = await _workspace_market_discovery(request).discover(
                    exchange=exchange,
                    market_type=selected_type,
                    api_variant=api_variant,
                    asset=selected_asset,
                )
            except RuntimeError as exc:
                raise ValueError(
                    f"cannot verify {selected_symbol} {selected_type}: {exc}"
                ) from exc
            market = next(
                (
                    row
                    for row in discovered
                    if str(row.get("symbol") or "").upper() == selected_symbol
                    and row.get("active") is not False
                ),
                None,
            )
            if market is None:
                raise ValueError(
                    f"{selected_symbol} is not an active {selected_type} market on "
                    f"{exchange}"
                )
            key = (selected_type, selected_symbol)
            if key in seen_markets:
                continue
            seen_markets.add(key)
            quote = str(market.get("quote") or "").upper()
            project_key = (selected_asset, quote)
            project = projects_by_pair.get(project_key)
            if project is None:
                project = store.upsert_project(
                    UserProject.from_dict(
                        {
                            "owner_email": user.email,
                            "name": f"{selected_asset}/{quote}",
                            "asset": selected_asset,
                            "quote_currency": quote,
                            "status": "active",
                        }
                    )
                )
                projects.append(project)
                projects_by_pair[project_key] = project
            elif project.status != "active":
                project = store.set_project_status(project.id, "active")
                projects_by_pair[project_key] = project
            matches.append((project, market, selected_type))
    else:
        for project in projects:
            if project.status != "active":
                continue
            try:
                markets, _ = await _workspace_market_discovery(request).discover(
                    exchange=exchange,
                    market_type=market_type,
                    api_variant=api_variant,
                    asset=project.asset,
                )
            except RuntimeError as exc:
                warnings.append(f"{project.symbol} discovery failed: {exc}")
                continue
            exact = [
                market
                for market in markets
                if str(market.get("quote") or "").upper() == project.quote_currency
                and market.get("active") is not False
            ]
            if not exact:
                continue
            exact.sort(key=lambda market: str(market.get("symbol") or ""))
            matches.append((project, exact[0], market_type))

        matched_project_ids = {project.id for project, _, _ in matches}
        project_by_id = {project.id: project for project in projects}
        for existing in existing_rows:
            if existing.project_id in matched_project_ids:
                continue
            project = project_by_id.get(existing.project_id)
            if project is None:
                continue
            matches.append((project, {"symbol": existing.symbol}, existing.market_type))
            warnings.append(
                f"kept existing {existing.symbol} binding because automatic market "
                "discovery did not return it"
            )

    existing_by_market = {
        (account.market_type, account.symbol): account for account in existing_rows
    }
    credential_copy: dict[str, str] = dict(supplied)
    if not credential_copy:
        credential_copy = store.decrypt_credentials(
            account_id=api_connection.id,
            owner_email=user.email,
        )
    saved: list[UserExchangeAccount] = []
    try:
        for project, market, binding_market_type in matches:
            binding_symbol = str(market.get("symbol") or "").upper()
            existing = existing_by_market.get((binding_market_type, binding_symbol))
            account_raw = existing.to_dict() if existing is not None else {}
            account_raw.update(
                {
                    "id": (
                        existing.id
                        if existing is not None
                        else f"account-{secrets.token_hex(6)}"
                    ),
                    "owner_email": user.email,
                    "project_id": project.id,
                    "connection_id": connection_id,
                    "label": api_connection.label,
                    "exchange": exchange,
                    "market_type": binding_market_type,
                    "api_variant": api_variant,
                    "symbol": binding_symbol,
                    "egress_mode": api_connection.egress_mode,
                    "egress_source_ip": api_connection.egress_source_ip,
                    "egress_expected_ip": api_connection.egress_expected_ip,
                    "egress_observed_ip": api_connection.egress_observed_ip,
                    "egress_checked_at": api_connection.egress_checked_at,
                    "enabled": bool(existing.enabled)
                    if existing is not None
                    else False,
                    "withdrawal_disabled_confirmed": bool(
                        api_connection.withdrawal_disabled_confirmed
                    ),
                    "trade_permission_confirmed": bool(
                        api_connection.trade_permission_confirmed
                    ),
                }
            )
            account = UserExchangeAccount.from_dict(account_raw)
            account_credentials: dict[str, str] | None = None
            if supplied or existing is None:
                account_credentials = credential_copy
            saved_account = store.upsert_account(
                account,
                credentials=account_credentials,
                replace_credentials=bool(
                    existing is not None and existing.exchange != exchange
                ),
            )
            if existing is None and api_connection_is_fresh(api_connection):
                saved_account = store.update_account_connection(
                    saved_account.id,
                    status="healthy",
                    check={
                        "balances": api_connection.balance_snapshot,
                        "open_order_count": api_connection.open_order_count or 0,
                        "latency_ms": api_connection.connection_latency_ms or 0.0,
                    },
                )
            saved.append(saved_account)
        if replace_markets:
            selected_keys = {(account.market_type, account.symbol) for account in saved}
            for existing in existing_rows:
                if (existing.market_type, existing.symbol) in selected_keys:
                    continue
                try:
                    if existing.enabled:
                        store.upsert_account(replace(existing, enabled=False))
                    store.delete_account(existing.id)
                except ValueError as exc:
                    warnings.append(
                        f"kept {existing.symbol} because it is still in use: {exc}"
                    )
    finally:
        credential_copy.clear()
        supplied.clear()
    return connection_id, saved, warnings


async def _refresh_admin_workspace_runtime_accounts(request: web.Request) -> None:
    admin_emails = [
        row.email for row in _user_store(request).list_users() if row.role == "admin"
    ]
    workspace = build_workspace_runtime_accounts(
        _user_workspace_store(request),
        owner_emails=admin_emails,
    )
    await request.app["monitor_state"].set_workspace_runtime_accounts(workspace)


async def api_user_workspace(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    store = _user_workspace_store(request)
    action = ""
    user: WebUser | None = None
    safe_error_target = ""
    try:
        user = _require_workspace_user(_request_user(request))
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            raise ValueError("action is required")
        if action in {
            "wallet_challenge",
            "verify_wallet",
            "prepare_hyperliquid_agent",
            "complete_hyperliquid_agent",
            "cancel_hyperliquid_agent",
            "delete_wallet",
            "test_wallet_venue",
            "refresh_venue_connection",
            "refresh_all_venue_connections",
            "delete_venue_connection",
        }:
            require_capability(user, "security.manage")
        elif action in {
            "upsert_project",
            "activate_project",
            "approve_project",
            "disable_project",
            "delete_project",
            "sync_account",
            "test_connection",
            "discover_markets",
            "test_account",
            "upsert_account",
            "delete_connection",
            "delete_account",
        }:
            require_capability(user, "account.manage")
        elif action in {
            "upsert_strategy",
            "set_strategy_enabled",
            "clone_strategy",
            "delete_strategy",
            "reset_strategy_paper",
        }:
            require_capability(user, "strategy.manage")
        elif action == "update_risk_profile":
            require_capability(user, "risk.manage")
        raw_target = (
            payload.get("account")
            if isinstance(payload.get("account"), dict)
            else payload
        )
        safe_error_target = str(
            raw_target.get("connection_id")
            or raw_target.get("exchange")
            or raw_target.get("id")
            or ""
        )[:120]

        audit_target = ""
        audit_detail = ""
        audit_payload: dict[str, Any] = {}
        response_extra: dict[str, Any] = {}

        if action == "wallet_challenge":
            challenge = store.create_wallet_challenge(
                owner_email=user.email,
                address=str(payload.get("address") or ""),
                chain_id=int(payload.get("chain_id") or 0),
                wallet_type=str(payload.get("wallet_type") or "injected"),
                domain=str(request.host or "crypto-arbitrage"),
            )
            audit_target = challenge["address"]
            audit_detail = "issued read-only wallet authorization challenge"
            audit_payload = {
                "challenge_id": challenge["challenge_id"],
                "address": challenge["address"],
                "chain_id": challenge["chain_id"],
                "expires_at": challenge["expires_at"],
            }
            response_extra = {"wallet_challenge": challenge}
        elif action == "verify_wallet":
            wallet = store.verify_wallet_challenge(
                owner_email=user.email,
                challenge_id=str(payload.get("challenge_id") or ""),
                signature=str(payload.get("signature") or ""),
                label=str(payload.get("label") or ""),
            )
            audit_target = wallet.id
            audit_detail = "verified and linked read-only wallet"
            audit_payload = wallet.to_dict()
            response_extra = {"wallet": wallet.to_dict()}
        elif action == "prepare_hyperliquid_agent":
            wallet_id = str(payload.get("wallet_id") or "").strip()
            wallet = store.get_wallet(wallet_id)
            if wallet is None:
                raise ValueError(
                    "verify a MetaMask wallet before authorizing Hyperliquid"
                )
            _require_workspace_owner(user, wallet.owner_email)
            raw = dict(
                payload.get("account")
                if isinstance(payload.get("account"), dict)
                else {}
            )
            account_id = str(raw.get("id") or "").strip()
            existing = store.get_account(account_id) if account_id else None
            if existing is not None:
                _require_workspace_owner(user, existing.owner_email)
                base = existing.to_dict()
                base.update(raw)
                raw = base
            project_id = str(raw.get("project_id") or "").strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_workspace_owner(user, project.owner_email)
            raw.update(
                {
                    "owner_email": user.email,
                    "exchange": "hyperliquid",
                    "symbol": str(raw.get("symbol") or project.symbol).upper(),
                    "enabled": False,
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                    "connection_status": (
                        existing.connection_status if existing else "unverified"
                    ),
                    "connection_checked_at": (
                        existing.connection_checked_at if existing else None
                    ),
                    "connection_error": existing.connection_error if existing else "",
                }
            )
            account = UserExchangeAccount.from_dict(raw)
            if _base_asset_from_symbol(account.symbol) != project.asset:
                raise ValueError(
                    f"account symbol base must match project asset {project.asset}"
                )
            authorization = store.prepare_hyperliquid_authorization(
                owner_email=user.email,
                wallet=wallet,
                account=account,
                chain_id=int(payload.get("chain_id") or 0),
            )
            audit_target = authorization["authorization_id"]
            audit_detail = "prepared encrypted Hyperliquid API wallet authorization"
            audit_payload = {
                "authorization_id": authorization["authorization_id"],
                "wallet_id": wallet.id,
                "wallet_address": wallet.address,
                "account_id": account.id,
                "agent_address": authorization["agent_address"],
                "agent_name": authorization["agent_name"],
                "api_variant": account.api_variant,
                "expires_at": authorization["expires_at"],
            }
            response_extra = {
                "hyperliquid_authorization": {
                    **audit_payload,
                    "typed_data": authorization["typed_data"],
                }
            }
        elif action == "complete_hyperliquid_agent":
            authorization_id = str(payload.get("authorization_id") or "").strip()
            signature = str(payload.get("signature") or "").strip()
            pending = store.get_hyperliquid_authorization(
                authorization_id,
                owner_email=user.email,
            )
            recovered = recover_authorizer(pending["typed_data"], signature)
            if recovered.lower() != str(pending["wallet_address"]).lower():
                raise ValueError(
                    "MetaMask signature does not match the verified wallet address"
                )
            submission = await submit_agent_authorization(
                action=pending["action"],
                nonce=int(pending["nonce"]),
                signature=signature,
                api_variant=str(pending["api_variant"]),
            )
            account = store.finalize_hyperliquid_authorization(
                authorization_id,
                owner_email=user.email,
            )
            wallet = store.get_wallet(str(pending["wallet_id"]))
            venue_connection = None
            if wallet is not None:
                venue_check = await probe_dex_venue(
                    venue="hyperliquid",
                    wallet_address=wallet.address,
                )
                venue_connection = store.upsert_venue_connection(
                    owner_email=user.email,
                    venue="hyperliquid",
                    wallet=wallet,
                    check=venue_check,
                )
            audit_target = account.id
            audit_detail = "authorized encrypted Hyperliquid API wallet"
            audit_payload = {
                "account_id": account.id,
                "wallet_id": account.wallet_id,
                "agent_address": account.agent_address,
                "agent_name": account.agent_name,
                "api_variant": account.api_variant,
                "enabled": False,
                "live_order_submitted": False,
            }
            response_extra = {
                "account": {
                    **account.to_dict(),
                    "credentials": store.credential_status(account.id),
                },
                "hyperliquid_authorization": submission,
                "venue_connection": (
                    venue_connection.to_dict() if venue_connection else None
                ),
            }
        elif action == "cancel_hyperliquid_agent":
            authorization_id = str(payload.get("authorization_id") or "").strip()
            store.cancel_hyperliquid_authorization(
                authorization_id,
                owner_email=user.email,
            )
            audit_target = authorization_id
            audit_detail = "discarded pending Hyperliquid API wallet authorization"
            audit_payload = {"authorization_id": authorization_id}
        elif action == "delete_wallet":
            wallet_id = str(payload.get("wallet_id") or payload.get("id") or "").strip()
            wallet = store.get_wallet(wallet_id)
            if wallet is None:
                raise ValueError(f"wallet not found: {wallet_id}")
            _require_workspace_owner(user, wallet.owner_email)
            store.delete_wallet(wallet.id, owner_email=user.email)
            audit_target = wallet.id
            audit_detail = "revoked linked wallet"
            audit_payload = {"wallet_id": wallet.id, "address": wallet.address}
        elif action == "test_wallet_venue":
            wallet_id = str(payload.get("wallet_id") or "").strip()
            wallet = store.get_wallet(wallet_id) if wallet_id else None
            if wallet is not None:
                _require_workspace_owner(user, wallet.owner_email)
            venue = str(payload.get("venue") or "").strip().lower()
            if venue != "dydx" and wallet is None:
                raise ValueError(f"{venue or 'venue'} requires a verified wallet")
            venue_check = await probe_dex_venue(
                venue=venue,
                wallet_address=wallet.address if wallet else "",
            )
            venue_connection = store.upsert_venue_connection(
                owner_email=user.email,
                venue=venue,
                wallet=wallet,
                check=venue_check,
            )
            audit_target = f"{venue}:{wallet.id if wallet else 'public'}"
            audit_detail = f"tested {venue} read-only connectivity"
            audit_payload = {
                "venue": venue,
                "wallet_id": wallet.id if wallet else "",
                "status": venue_check["status"],
                "latency_ms": venue_check["latency_ms"],
                "live_trading_authorized": False,
            }
            response_extra = {
                "venue_check": venue_check,
                "venue_connection": venue_connection.to_dict(),
            }
        elif action == "refresh_venue_connection":
            connection_id = str(
                payload.get("connection_id") or payload.get("id") or ""
            ).strip()
            venue_connection = store.get_venue_connection(connection_id)
            if venue_connection is None:
                raise ValueError(f"venue connection not found: {connection_id}")
            _require_workspace_owner(user, venue_connection.owner_email)
            refresh_result = await refresh_venue_connections(
                store,
                [venue_connection],
                force=True,
                max_batch=1,
            )
            if not refresh_result["connections"]:
                raise ValueError("venue connection was revoked during refresh")
            refreshed_connection = refresh_result["connections"][0]
            audit_target = venue_connection.id
            audit_detail = "refreshed read-only venue connection"
            audit_payload = {
                "connection_id": venue_connection.id,
                "venue": venue_connection.venue,
                "status": refreshed_connection["status"],
                "live_trading_authorized": False,
            }
            response_extra = {"venue_refresh": refresh_result}
        elif action == "refresh_all_venue_connections":
            venue_connections = store.list_venue_connections(
                owner_email=user.email,
                is_admin=False,
            )
            refresh_result = await refresh_venue_connections(
                store,
                venue_connections,
                force=True,
            )
            audit_target = user.email
            audit_detail = "refreshed all owned read-only venue connections"
            audit_payload = {
                "candidate_count": refresh_result["candidate_count"],
                "refreshed_count": refresh_result["refreshed_count"],
                "healthy_count": refresh_result["healthy_count"],
                "error_count": refresh_result["error_count"],
                "live_trading_authorized": False,
            }
            response_extra = {"venue_refresh": refresh_result}
        elif action == "delete_venue_connection":
            connection_id = str(
                payload.get("connection_id") or payload.get("id") or ""
            ).strip()
            venue_connection = store.get_venue_connection(connection_id)
            if venue_connection is None:
                raise ValueError(f"venue connection not found: {connection_id}")
            _require_workspace_owner(user, venue_connection.owner_email)
            store.delete_venue_connection(
                venue_connection.id,
                owner_email=user.email,
            )
            audit_target = venue_connection.id
            audit_detail = "revoked read-only venue connection"
            audit_payload = {
                "connection_id": venue_connection.id,
                "venue": venue_connection.venue,
                "wallet_id": venue_connection.wallet_id,
            }
        elif action == "upsert_project":
            raw = dict(
                payload.get("project")
                if isinstance(payload.get("project"), dict)
                else payload
            )
            project_id = str(raw.get("id") or "").strip()
            existing = store.get_project(project_id) if project_id else None
            if existing is not None:
                _require_workspace_owner(user, existing.owner_email)
                base = existing.to_dict()
                base.update(raw)
                raw = base
            owner = _workspace_owner(
                raw,
                user=user,
                existing_owner=existing.owner_email if existing else "",
            )
            if _user_store(request).get_user(owner) is None:
                raise ValueError("project owner is not a registered user")
            raw["owner_email"] = owner
            raw["status"] = (
                "disabled"
                if existing is not None and existing.status == "disabled"
                else "active"
            )
            project = UserProject.from_dict(raw)
            project = store.upsert_project(project)
            connection_sync: list[dict[str, Any]] = []
            api_connections = (
                store.list_api_connections(
                    owner_email=project.owner_email,
                    is_admin=False,
                )
                if project.owner_email == user.email
                else []
            )
            for api_connection in api_connections:
                try:
                    (
                        _,
                        synced_accounts,
                        sync_warnings,
                    ) = await _sync_workspace_connection(
                        request,
                        user=user,
                        raw=api_connection.to_dict(),
                        credentials=None,
                    )
                    connection_sync.append(
                        {
                            "connection_id": api_connection.id,
                            "account_ids": [row.id for row in synced_accounts],
                            "warnings": sync_warnings,
                        }
                    )
                except (RuntimeError, ValueError) as exc:
                    connection_sync.append(
                        {
                            "connection_id": api_connection.id,
                            "account_ids": [],
                            "warnings": [str(exc)],
                        }
                    )
            audit_target = project.id
            audit_detail = f"saved self-service user project {project.name}"
            audit_payload = project.to_dict()
            response_extra = {
                "project": project.to_dict(),
                "connection_sync": connection_sync,
            }
        elif action == "activate_project":
            project_id = str(
                payload.get("project_id") or payload.get("id") or ""
            ).strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_owner_or_admin(user, project.owner_email)
            project = store.set_project_status(project.id, "active")
            audit_target = project.id
            audit_detail = f"activated user project {project.name}"
            audit_payload = project.to_dict()
            response_extra = {"project": project.to_dict()}
        elif action == "approve_project":
            _require_admin_user(user)
            project_id = str(
                payload.get("project_id") or payload.get("id") or ""
            ).strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            project = store.set_project_status(project.id, "active")
            audit_target = project.id
            audit_detail = f"approved user project {project.name}"
            audit_payload = project.to_dict()
        elif action == "disable_project":
            project_id = str(
                payload.get("project_id") or payload.get("id") or ""
            ).strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_owner_or_admin(user, project.owner_email)
            project = store.set_project_status(project.id, "disabled")
            audit_target = project.id
            audit_detail = f"disabled user project {project.name}"
            audit_payload = project.to_dict()
        elif action == "delete_project":
            project_id = str(
                payload.get("project_id") or payload.get("id") or ""
            ).strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_workspace_owner(user, project.owner_email)
            store.delete_project(project.id)
            audit_target = project.id
            audit_detail = f"deleted user project {project.name}"
            audit_payload = {"project_id": project.id}
        elif action == "sync_account":
            raw = dict(
                payload.get("account")
                if isinstance(payload.get("account"), dict)
                else payload
            )
            credentials = raw.pop("credentials", None)
            connection_id, accounts, warnings = await _sync_workspace_connection(
                request,
                user=user,
                raw=raw,
                credentials=credentials if isinstance(credentials, dict) else None,
            )
            api_connection = store.get_api_connection(connection_id)
            if api_connection is None:
                raise RuntimeError("global API connection was not persisted")
            audit_target = connection_id
            audit_detail = (
                f"synced {api_connection.exchange} API connection to "
                f"{len(accounts)} project market(s)"
            )
            audit_payload = {
                "connection_id": connection_id,
                "exchange": api_connection.exchange,
                "market_type": api_connection.market_type,
                "account_ids": [account.id for account in accounts],
                "project_ids": [account.project_id for account in accounts],
                "symbols": [account.symbol for account in accounts],
                "warnings": warnings,
            }
            response_extra = {
                "connection_id": connection_id,
                "accounts": [account.to_dict() for account in accounts],
                "warnings": warnings,
            }
        elif action == "test_connection":
            connection_id = str(
                payload.get("connection_id") or payload.get("id") or ""
            ).strip()
            accounts = _workspace_connection_accounts(
                store,
                user=user,
                connection_id=connection_id,
            )
            api_connection = store.get_api_connection(connection_id)
            if api_connection is None:
                raise ValueError(f"API connection not found: {connection_id}")
            _require_workspace_owner(user, api_connection.owner_email)
            results: list[dict[str, Any]] = []
            updated_accounts: list[UserExchangeAccount] = []
            if not accounts:
                credential_status = store.credential_status(api_connection.id)
                if not credential_status["configured"]:
                    raise ValueError("configure required credentials before testing")
                credentials = store.decrypt_credentials(
                    account_id=api_connection.id,
                    owner_email=api_connection.owner_email,
                )
                try:
                    check_result = await _workspace_account_checker(
                        request
                    ).check_api_connection(
                        api_connection=api_connection,
                        credentials=credentials,
                    )
                finally:
                    credentials.clear()
                api_connection = store.update_api_connection_check(
                    api_connection.id,
                    status=str(check_result.get("status") or "error"),
                    error=str(check_result.get("error") or ""),
                    check=check_result,
                )
                results.append(check_result)
                if check_result.get("status") == "healthy":
                    (
                        _,
                        synced_accounts,
                        sync_warnings,
                    ) = await _sync_workspace_connection(
                        request,
                        user=user,
                        raw={
                            **api_connection.to_dict(),
                            "connection_id": api_connection.id,
                        },
                        credentials=None,
                    )
                    if synced_accounts:
                        accounts = synced_accounts
                        results.clear()
                    if sync_warnings:
                        response_extra["warnings"] = sync_warnings
            for account in accounts:
                project = store.get_project(account.project_id)
                if project is None:
                    raise ValueError(f"project not found: {account.project_id}")
                credential_status = store.credential_status(api_connection.id)
                if not credential_status["configured"]:
                    raise ValueError(
                        f"configure required credentials before testing {account.symbol}"
                    )
                credentials = store.decrypt_credentials(
                    account_id=api_connection.id,
                    owner_email=api_connection.owner_email,
                )
                try:
                    check_result = await _workspace_account_checker(request).check(
                        account=account,
                        project=project,
                        credentials=credentials,
                    )
                finally:
                    credentials.clear()
                current_account = store.get_account(account.id)
                current_project = store.get_project(project.id)
                if (
                    current_account is None
                    or current_project is None
                    or current_account.updated_at != account.updated_at
                    or current_project.updated_at != project.updated_at
                ):
                    raise RuntimeError(
                        "account or project changed during the connection test; "
                        "result discarded"
                    )
                updated = store.update_account_connection(
                    account.id,
                    status=str(check_result.get("status") or "error"),
                    error=str(check_result.get("error") or ""),
                    check=check_result,
                )
                if (
                    check_result.get("status") == "healthy"
                    and updated.withdrawal_disabled_confirmed
                    and updated.trade_permission_confirmed
                    and not updated.enabled
                ):
                    updated = store.upsert_account(replace(updated, enabled=True))
                updated_accounts.append(updated)
                results.append(
                    {
                        **check_result,
                        "account_id": updated.id,
                        "project_id": updated.project_id,
                        "symbol": updated.symbol,
                    }
                )
            healthy_count = sum(result.get("status") == "healthy" for result in results)
            connection_status = "healthy" if healthy_count == len(results) else "error"
            if accounts:
                representative = next(
                    (result for result in results if result.get("status") == "healthy"),
                    results[0] if results else {},
                )
                balance_rows: dict[tuple[str, str], dict[str, Any]] = {}
                open_orders_by_symbol: dict[str, int] = {}
                for result in results:
                    symbol_key = str(result.get("symbol") or "").upper()
                    if symbol_key:
                        open_orders_by_symbol[symbol_key] = max(
                            open_orders_by_symbol.get(symbol_key, 0),
                            int(result.get("open_order_count") or 0),
                        )
                    for row in result.get("balances") or []:
                        if not isinstance(row, dict) or not row.get("currency"):
                            continue
                        key = (
                            str(row.get("currency") or "").upper(),
                            str(row.get("wallet") or "trading"),
                        )
                        current = balance_rows.get(key)
                        if current is None or float(row.get("total") or 0.0) > float(
                            current.get("total") or 0.0
                        ):
                            balance_rows[key] = dict(row)
                aggregate_check = {
                    **representative,
                    "balances": list(balance_rows.values()),
                    "latency_ms": max(
                        (float(result.get("latency_ms") or 0.0) for result in results),
                        default=0.0,
                    ),
                    "open_order_count": sum(open_orders_by_symbol.values()),
                }
                api_connection = store.update_api_connection_check(
                    api_connection.id,
                    status=connection_status,
                    error=next(
                        (
                            str(result.get("error") or "")
                            for result in results
                            if result.get("status") != "healthy"
                        ),
                        "",
                    ),
                    check=aggregate_check,
                )
            audit_target = connection_id
            audit_detail = (
                f"tested {api_connection.exchange} API connection: "
                f"{healthy_count}/{len(results)} markets healthy"
            )
            audit_payload = {
                "connection_id": connection_id,
                "exchange": api_connection.exchange,
                "status": connection_status,
                "healthy_count": healthy_count,
                "market_count": len(results),
                "results": results,
            }
            response_extra = {
                "connection_test": {
                    "connection_id": connection_id,
                    "status": connection_status,
                    "healthy_count": healthy_count,
                    "market_count": len(results),
                    "results": results,
                },
                "accounts": [account.to_dict() for account in updated_accounts],
            }
        elif action == "upsert_account":
            raw = dict(
                payload.get("account")
                if isinstance(payload.get("account"), dict)
                else payload
            )
            credentials = raw.pop("credentials", None)
            account_id = str(raw.get("id") or "").strip()
            existing = store.get_account(account_id) if account_id else None
            if existing is not None:
                _require_workspace_owner(user, existing.owner_email)
                base = existing.to_dict()
                base.update(raw)
                raw = base
            project_id = str(raw.get("project_id") or "").strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_workspace_owner(user, project.owner_email)
            owner = _workspace_owner(
                raw,
                user=user,
                existing_owner=existing.owner_email
                if existing
                else project.owner_email,
            )
            if owner != project.owner_email:
                raise ValueError("project and exchange account owners must match")
            raw["owner_email"] = owner
            raw["symbol"] = str(raw.get("symbol") or project.symbol).strip().upper()
            raw["connection_status"] = (
                existing.connection_status if existing else "unverified"
            )
            raw["connection_checked_at"] = (
                existing.connection_checked_at if existing else None
            )
            raw["connection_error"] = existing.connection_error if existing else ""
            for managed_field in (
                "wallet_id",
                "agent_address",
                "agent_name",
                "authorization_verified_at",
            ):
                raw[managed_field] = (
                    getattr(existing, managed_field)
                    if existing is not None
                    and str(raw.get("exchange") or "").strip().lower() == "hyperliquid"
                    else None
                    if managed_field == "authorization_verified_at"
                    else ""
                )
            account = UserExchangeAccount.from_dict(raw)
            if _base_asset_from_symbol(account.symbol) != project.asset:
                raise ValueError(
                    f"account symbol base must match project asset {project.asset}"
                )
            exchange_changed = bool(
                existing is not None and existing.exchange != account.exchange
            )
            supplied = credentials if isinstance(credentials, dict) else {}
            credentials_changed = any(
                str(value or "").strip() for value in supplied.values()
            )
            connection_changed = bool(
                existing is None
                or credentials_changed
                or existing.exchange != account.exchange
                or existing.market_type != account.market_type
                or existing.api_variant != account.api_variant
                or existing.symbol != account.symbol
            )
            if connection_changed:
                account = replace(
                    account,
                    enabled=False,
                    connection_status="unverified",
                    connection_checked_at=None,
                    connection_error="",
                )
            if exchange_changed:
                required = required_credentials_for_exchange(account.exchange)
                supplied_fields = {
                    key for key, value in supplied.items() if str(value or "").strip()
                }
                missing = sorted(required.difference(supplied_fields))
                if missing:
                    raise ValueError(
                        "re-enter API key / required credentials when changing exchange: "
                        + ", ".join(missing)
                    )
            if account.enabled:
                require_capability(user, "account.trade")
                if project.status != "active":
                    raise PermissionError(
                        "project must be active before enabling account"
                    )
                if not account.withdrawal_disabled_confirmed:
                    raise ValueError(
                        "confirm that API withdrawal permission is disabled"
                    )
                if not account.trade_permission_confirmed:
                    raise ValueError("confirm that the API key has trading permission")
                current_auth = store.credential_status(account.id)
                required = required_credentials_for_exchange(account.exchange)
                supplied_fields = {
                    key for key, value in supplied.items() if str(value or "").strip()
                }
                has_required = current_auth["configured"] or required.issubset(
                    supplied_fields
                )
                if not has_required:
                    raise ValueError(
                        "configure required credentials before enabling account"
                    )
                if not current_auth["vault_available"]:
                    raise RuntimeError("credential encryption is not configured")
                if not account_connection_is_fresh(account):
                    raise ValueError(
                        "run a successful account connection test before enabling"
                    )
            account = store.upsert_account(
                account,
                credentials=credentials,
                replace_credentials=exchange_changed,
            )
            audit_target = account.id
            audit_detail = f"saved encrypted {account.exchange} account"
            audit_payload = account.to_dict()
            audit_payload["credentials"] = store.credential_status(account.id)
            response_extra = {"account": account.to_dict()}
        elif action == "discover_markets":
            project_id = str(payload.get("project_id") or "").strip()
            exchange = str(payload.get("exchange") or "").strip().lower()
            api_variant = str(payload.get("api_variant") or "").strip().lower()
            connection_id = str(payload.get("connection_id") or "").strip()
            api_connection = (
                store.get_api_connection(connection_id) if connection_id else None
            )
            if api_connection is not None:
                _require_workspace_owner(user, api_connection.owner_email)
                exchange = api_connection.exchange
                api_variant = api_connection.api_variant
            probe = api_connection or UserApiConnection.from_dict(
                {
                    "owner_email": user.email,
                    "label": exchange,
                    "exchange": exchange,
                    "api_variant": api_variant,
                }
            )
            raw_assets = payload.get("assets")
            if isinstance(raw_assets, str):
                raw_assets = raw_assets.replace(",", " ").split()
            assets = [
                str(asset).strip().upper()
                for asset in (raw_assets or [])
                if str(asset).strip()
            ]
            if project_id:
                project = store.get_project(project_id)
                if project is None:
                    raise ValueError(f"project not found: {project_id}")
                _require_workspace_owner(user, project.owner_email)
                assets.append(project.asset)
            assets = list(dict.fromkeys(assets))
            if not assets:
                raise ValueError("enter at least one tradable currency")
            if len(assets) > 20:
                raise ValueError("load at most 20 currencies at a time")
            requested_type = str(payload.get("market_type") or "").strip().lower()
            market_types = (
                (requested_type,)
                if requested_type
                else probe.market_types or (probe.market_type,)
            )
            markets: list[dict[str, Any]] = []
            cached = True
            for market_type in market_types:
                for asset in assets:
                    discovered, from_cache = await _workspace_market_discovery(
                        request
                    ).discover(
                        exchange=probe.exchange,
                        market_type=market_type,
                        api_variant=probe.api_variant,
                        asset=asset,
                    )
                    cached = cached and from_cache
                    markets.extend(
                        {**market, "market_type": market_type} for market in discovered
                    )
            deduplicated = {
                (str(market.get("market_type")), str(market.get("symbol"))): market
                for market in markets
            }
            markets = sorted(
                deduplicated.values(),
                key=lambda row: (
                    str(row.get("base") or ""),
                    str(row.get("market_type") or ""),
                    str(row.get("quote") or ""),
                    str(row.get("symbol") or ""),
                ),
            )[:500]
            audit_target = connection_id or exchange
            audit_detail = f"discovered {len(markets)} account markets on {exchange}"
            audit_payload = {
                "connection_id": connection_id,
                "exchange": exchange,
                "market_types": list(market_types),
                "api_variant": probe.api_variant,
                "assets": assets,
                "market_count": len(markets),
                "cached": cached,
            }
            response_extra = {
                "markets": markets,
                "cached": cached,
            }
        elif action == "test_account":
            account_id = str(
                payload.get("account_id") or payload.get("id") or ""
            ).strip()
            account = store.get_account(account_id)
            if account is None:
                raise ValueError(f"exchange account not found: {account_id}")
            _require_workspace_owner(user, account.owner_email)
            project = store.get_project(account.project_id)
            if project is None:
                raise ValueError(f"project not found: {account.project_id}")
            credential_status = store.credential_status(account.id)
            if not credential_status["configured"]:
                raise ValueError(
                    "configure required credentials before testing account"
                )
            credentials = store.decrypt_credentials(
                account_id=account.id,
                owner_email=account.owner_email,
            )
            try:
                check_result = await _workspace_account_checker(request).check(
                    account=account,
                    project=project,
                    credentials=credentials,
                )
            finally:
                credentials.clear()
            current_account = store.get_account(account.id)
            current_project = store.get_project(project.id)
            if (
                current_account is None
                or current_project is None
                or current_account.updated_at != account.updated_at
                or current_project.updated_at != project.updated_at
            ):
                raise RuntimeError(
                    "account or project changed during the connection test; result discarded"
                )
            account = store.update_account_connection(
                account.id,
                status=str(check_result.get("status") or "error"),
                error=str(check_result.get("error") or ""),
                check=check_result,
            )
            audit_target = account.id
            audit_detail = (
                f"tested {account.exchange} account: {account.connection_status}"
            )
            audit_payload = {
                "account_id": account.id,
                "project_id": account.project_id,
                "exchange": account.exchange,
                "market_type": account.market_type,
                "api_variant": account.api_variant,
                "symbol": account.symbol,
                "status": account.connection_status,
                "latency_ms": check_result.get("latency_ms"),
                "error": account.connection_error,
            }
            response_extra = {"connection_test": check_result}
        elif action == "upsert_strategy":
            raw = dict(
                payload.get("strategy")
                if isinstance(payload.get("strategy"), dict)
                else payload
            )
            strategy_id = str(raw.get("id") or "").strip()
            existing = store.get_strategy(strategy_id) if strategy_id else None
            if existing is not None:
                _require_workspace_owner(user, existing.owner_email)
                base = existing.to_dict()
                base.update(raw)
                raw = base
            project_id = str(raw.get("project_id") or "").strip()
            project = store.get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_workspace_owner(user, project.owner_email)
            owner = _workspace_owner(
                raw,
                user=user,
                existing_owner=(
                    existing.owner_email if existing else project.owner_email
                ),
            )
            if owner != project.owner_email:
                raise ValueError("project and strategy owners must match")
            raw["owner_email"] = owner
            requested_type = str(raw.get("strategy_type") or "").strip().lower()
            live_supported = requested_type in LIVE_USER_STRATEGY_TYPES
            raw["mode"] = "live" if live_supported else "paper"
            raw["live_enabled"] = live_supported
            strategy = UserStrategy.from_dict(raw)
            if strategy.enabled:
                if strategy.mode == "live":
                    require_capability(user, "account.trade")
                if (
                    strategy.mode == "live"
                    and payload.get("confirm_live")
                    != LIVE_MARKET_MAKER_CONFIRMATION
                ):
                    raise ValueError(
                        "enabling or changing a live owner Market Maker requires "
                        f"confirm_live={LIVE_MARKET_MAKER_CONFIRMATION}"
                    )
                readiness = store.strategy_readiness(strategy)
                if not readiness["ready"]:
                    raise ValueError(
                        "strategy cannot be enabled: "
                        + "; ".join(readiness["blockers"])
                    )
            strategy = store.upsert_strategy(strategy)
            audit_target = strategy.id
            audit_detail = f"saved {strategy.mode} owner strategy {strategy.name}"
            audit_payload = strategy.to_dict()
        elif action == "set_strategy_enabled":
            strategy_id = str(
                payload.get("strategy_id") or payload.get("id") or ""
            ).strip()
            strategy = store.get_strategy(strategy_id)
            if strategy is None:
                raise ValueError(f"strategy not found: {strategy_id}")
            _require_workspace_owner(user, strategy.owner_email)
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false")
            updated = replace(strategy, enabled=enabled)
            if enabled:
                if strategy.mode == "live":
                    require_capability(user, "account.trade")
                if (
                    strategy.mode == "live"
                    and payload.get("confirm_live")
                    != LIVE_MARKET_MAKER_CONFIRMATION
                ):
                    raise ValueError(
                        "enabling a live owner Market Maker requires "
                        f"confirm_live={LIVE_MARKET_MAKER_CONFIRMATION}"
                    )
                readiness = store.strategy_readiness(updated)
                if not readiness["ready"]:
                    raise ValueError(
                        "strategy cannot be enabled: "
                        + "; ".join(readiness["blockers"])
                    )
            strategy = store.upsert_strategy(updated)
            audit_target = strategy.id
            audit_detail = (
                f"{'resumed' if enabled else 'paused'} owner strategy {strategy.name}"
            )
            audit_payload = {
                "strategy_id": strategy.id,
                "enabled": strategy.enabled,
                "mode": strategy.mode,
            }
        elif action == "clone_strategy":
            strategy_id = str(
                payload.get("strategy_id") or payload.get("id") or ""
            ).strip()
            strategy = store.get_strategy(strategy_id)
            if strategy is None:
                raise ValueError(f"strategy not found: {strategy_id}")
            _require_workspace_owner(user, strategy.owner_email)
            raw = strategy.to_dict()
            raw.pop("id", None)
            raw.pop("created_at", None)
            raw.pop("updated_at", None)
            raw["name"] = str(payload.get("name") or f"{strategy.name} Copy").strip()[
                :80
            ]
            raw["enabled"] = False
            copied = store.upsert_strategy(UserStrategy.from_dict(raw))
            audit_target = copied.id
            audit_detail = f"copied owner strategy {strategy.id} to {copied.id}"
            audit_payload = {
                "source_strategy_id": strategy.id,
                "strategy": copied.to_dict(),
            }
            response_extra = {"copied_strategy_id": copied.id}
        elif action == "delete_strategy":
            strategy_id = str(
                payload.get("strategy_id") or payload.get("id") or ""
            ).strip()
            strategy = store.get_strategy(strategy_id)
            if strategy is None:
                raise ValueError(f"strategy not found: {strategy_id}")
            _require_workspace_owner(user, strategy.owner_email)
            store.delete_strategy(strategy.id)
            _user_paper_store(request).delete_strategy(strategy.id)
            audit_target = strategy.id
            audit_detail = f"deleted owner strategy {strategy.name}"
            audit_payload = {"strategy_id": strategy.id, "mode": strategy.mode}
        elif action == "reset_strategy_paper":
            strategy_id = str(
                payload.get("strategy_id") or payload.get("id") or ""
            ).strip()
            strategy = store.get_strategy(strategy_id)
            if strategy is None:
                raise ValueError(f"strategy not found: {strategy_id}")
            _require_workspace_owner(user, strategy.owner_email)
            if strategy.mode != "paper":
                raise ValueError(
                    "paper reset is only available for paper strategies"
                )
            reset_counts = _user_paper_store(request).reset_strategy(strategy)
            audit_target = strategy.id
            audit_detail = f"reset paper simulation {strategy.name}"
            audit_payload = {
                "strategy_id": strategy.id,
                "mode": "paper",
                "deleted": reset_counts,
            }
            response_extra = {"paper_reset": reset_counts}
        elif action == "delete_connection":
            connection_id = str(
                payload.get("connection_id") or payload.get("id") or ""
            ).strip()
            accounts = _workspace_connection_accounts(
                store,
                user=user,
                connection_id=connection_id,
            )
            api_connection = store.get_api_connection(connection_id)
            if api_connection is None:
                raise ValueError(f"API connection not found: {connection_id}")
            _require_workspace_owner(user, api_connection.owner_email)
            for account in accounts:
                if account.enabled:
                    store.upsert_account(replace(account, enabled=False))
            deleted_count = store.delete_connection(
                connection_id,
                owner_email=user.email,
            )
            audit_target = connection_id
            audit_detail = (
                f"deleted {api_connection.exchange} API connection and "
                f"{deleted_count} market binding(s)"
            )
            audit_payload = {
                "connection_id": connection_id,
                "account_ids": [account.id for account in accounts],
                "deleted_count": deleted_count,
            }
            response_extra = {"deleted_count": deleted_count}
        elif action == "delete_account":
            account_id = str(
                payload.get("account_id") or payload.get("id") or ""
            ).strip()
            account = store.get_account(account_id)
            if account is None:
                raise ValueError(f"exchange account not found: {account_id}")
            _require_workspace_owner(user, account.owner_email)
            store.delete_account(account.id)
            audit_target = account.id
            audit_detail = f"deleted encrypted {account.exchange} account"
            audit_payload = {"account_id": account.id}
        elif action == "update_risk_profile":
            raw_profile = dict(
                payload.get("risk_profile")
                if isinstance(payload.get("risk_profile"), dict)
                else payload
            )
            raw_profile["owner_email"] = user.email
            profile = store.upsert_risk_profile(UserRiskProfile.from_dict(raw_profile))
            audit_target = user.email
            audit_detail = "updated user risk profile"
            audit_payload = profile.to_dict()
        else:
            raise ValueError(f"unsupported workspace action: {action}")

        if user.role == "admin" and action in {
            "upsert_project",
            "activate_project",
            "disable_project",
            "delete_project",
            "sync_account",
            "test_connection",
            "upsert_account",
            "delete_connection",
            "delete_account",
        }:
            await _refresh_admin_workspace_runtime_accounts(request)

        write_web_audit_event(
            cfg,
            request,
            action=f"user_workspace_{action}",
            target=audit_target,
            detail=audit_detail,
            payload=audit_payload,
        )
        response_payload = {
            "ok": True,
            "workspace": build_user_workspace_payload(
                store,
                user=user,
                paper_store=_user_paper_store(request),
            ),
        }
        response_payload.update(response_extra)
        return web.json_response(response_payload)
    except PermissionError as exc:
        write_web_audit_event(
            cfg,
            request,
            action=f"user_workspace_{action or 'invalid'}",
            status="blocked",
            target=safe_error_target,
            detail="workspace update rejected",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=403)
    except RuntimeError as exc:
        write_web_audit_event(
            cfg,
            request,
            action=f"user_workspace_{action or 'invalid'}",
            status="error",
            target=safe_error_target,
            detail="workspace update unavailable",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=503)
    except (json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        write_web_audit_event(
            cfg,
            request,
            action=f"user_workspace_{action or 'invalid'}",
            status="error",
            target=safe_error_target,
            detail="workspace update failed validation",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=400)


async def api_user_backtests_get(request: web.Request) -> web.Response:
    try:
        user = _require_workspace_user(_request_user(request))
        run_id = str(request.query.get("run_id") or "").strip()
        payload = _user_backtest_service(request).public_payload(
            owner_email=user.email,
            is_admin=False,
            run_id=run_id,
        )
        return web.json_response(payload)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (sqlite3.Error, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def api_user_backtests_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    service = _user_backtest_service(request)
    try:
        user = _require_workspace_user(_request_user(request))
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "create").strip().lower()

        if action == "create":
            project_id = str(payload.get("project_id") or "").strip()
            project = _user_workspace_store(request).get_project(project_id)
            if project is None:
                raise ValueError(f"project not found: {project_id}")
            _require_workspace_owner(user, project.owner_email)
            run = await service.create_run(
                owner_email=project.owner_email,
                project_id=project.id,
                strategy_id=str(payload.get("strategy_id") or "").strip(),
                account_id=str(payload.get("account_id") or "").strip(),
                timeframe=str(payload.get("timeframe") or "1h").strip(),
                history_bars=payload.get("history_bars", 200),
                initial_cash=payload.get("initial_cash", 1000.0),
                initial_base=payload.get("initial_base", 0.0),
                fee_bps=payload.get("fee_bps"),
                slippage_bps=payload.get("slippage_bps", 5.0),
                latency_bars=payload.get("latency_bars", 0),
            )
            write_web_audit_event(
                cfg,
                request,
                action="user_backtest_create",
                target=run["id"],
                detail="queued public historical backtest",
                payload={
                    "run_id": run["id"],
                    "owner_email": project.owner_email,
                    "project_id": project.id,
                    "strategy_id": run["strategy_id"],
                    "account_id": run["account_id"],
                    "timeframe": run["request"]["timeframe"],
                    "history_bars": run["request"]["history_bars"],
                },
            )
            return web.json_response(
                {
                    "ok": True,
                    "run": run,
                    "backtests": service.public_payload(
                        owner_email=user.email,
                        is_admin=False,
                        run_id=run["id"],
                    ),
                }
            )

        if action == "delete":
            run_id = str(payload.get("run_id") or "").strip()
            if not run_id:
                raise ValueError("run_id is required")
            service.delete_run(
                run_id,
                owner_email=user.email,
                is_admin=False,
            )
            write_web_audit_event(
                cfg,
                request,
                action="user_backtest_delete",
                target=run_id,
                detail="deleted historical backtest result",
                payload={"run_id": run_id},
            )
            return web.json_response(
                {
                    "ok": True,
                    "backtests": service.public_payload(
                        owner_email=user.email,
                        is_admin=False,
                    ),
                }
            )

        raise ValueError(f"unsupported backtest action: {action}")
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
