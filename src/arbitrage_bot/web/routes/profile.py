from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from aiohttp import web

from ..assets import HTML
from ..state import MonitorState
from ..render_payloads import (
    strategy_center_payload_for_view,
)
from ..users import (
    WebUser,
    normalize_email,
)
from ..verification import (
    VerificationRateLimited,
)
from ..user_scope import (
    _configured_assets,
    _filter_state_payload_for_user,
    _require_admin_user,
)

from ..permissions import (
    build_permission_scope,
)
from ..security import (
    SESSION_COOKIE,
    _client_ip,
    _purge_user_data,
    _reassign_user_data,
    _request_user,
    _user_paper_store,
    _user_store,
    _user_workspace_store,
    _verification_email_sender,
    _verification_manager,
    write_web_audit_event,
)

from ...auto_buy_sell_task import (
    AutoBuySellTaskService,
    slow_execution_config_from_dict,
    validate_task_config,
    validate_task_exchange_config,
)
from ...config import (
    BotConfig,
    SlowExecutionConfig,
)
from ...workspace_runtime import (
    build_workspace_runtime_accounts,
    isolated_workspace_runtime_config,
)
from ...web_config import (
    _auto_buy_sell_symbols_by_exchange,
    auto_buy_sell_exchanges,
    risk_config_to_dict,
    slow_execution_accounts,
    slow_execution_config_to_dict,
)


from ..core import (
    _merge_workspace_account_balances,
    _owner_live_market_maker_order_activity,
    _owner_live_trading_console,
    _sync_portfolio_with_account_balances,
    build_strategy_center_payload,
    build_user_workspace_payload,
)


async def index(_: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


def _user_auto_buy_sell_runtime_config(
    request: web.Request,
    user: WebUser,
    cfg: BotConfig,
) -> BotConfig:
    store = _user_workspace_store(request)
    workspace = build_workspace_runtime_accounts(
        store,
        owner_emails=[user.email],
        include_unbound_market_types=True,
    )
    return isolated_workspace_runtime_config(
        cfg,
        workspace,
        risk_profile=store.risk_profile(user.email),
    )


async def _user_auto_buy_sell_payload(
    request: web.Request,
    user: WebUser,
    cfg: BotConfig,
    *,
    runtime_cfg: BotConfig | None = None,
) -> dict[str, Any]:
    runtime_cfg = runtime_cfg or _user_auto_buy_sell_runtime_config(
        request,
        user,
        cfg,
    )
    accounts = slow_execution_accounts(
        auto_buy_sell_exchanges(runtime_cfg),
        _auto_buy_sell_symbols_by_exchange(runtime_cfg),
        spot_markets=runtime_cfg.spot_markets,
    )
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    task_snapshot = await tasks.snapshot(
        owner_email=user.email,
        is_admin=False,
    )
    task_rows = task_snapshot.get("tasks") or []
    stored_default = _user_workspace_store(request).strategy_default(
        user.email,
        "auto_buy_sell",
    )
    default_config: SlowExecutionConfig | None = None
    if stored_default:
        try:
            candidate = slow_execution_config_from_dict(stored_default)
            validate_task_config(candidate)
            validate_task_exchange_config(runtime_cfg, candidate)
            default_config = candidate
        except ValueError:
            default_config = None
    if default_config is None and task_rows:
        latest = max(
            (row for row in task_rows if isinstance(row, dict)),
            key=lambda row: float(row.get("updated_at") or 0.0),
            default={},
        )
        raw_config = latest.get("config") if isinstance(latest, dict) else {}
        if isinstance(raw_config, dict) and raw_config:
            try:
                candidate = slow_execution_config_from_dict(raw_config)
                validate_task_config(candidate)
                validate_task_exchange_config(runtime_cfg, candidate)
                default_config = candidate
            except ValueError:
                default_config = None
    if default_config is None:
        preferred_account = next(
            (row for row in accounts if row.get("market_type") == "swap"),
            accounts[0] if accounts else None,
        )
        market_type = str((preferred_account or {}).get("market_type") or "spot")
        symbols = (preferred_account or {}).get("symbols") or []
        symbol = str(
            (preferred_account or {}).get("symbol") or (symbols[0] if symbols else "")
        )
        default_config = replace(
            SlowExecutionConfig(),
            exchange=str((preferred_account or {}).get("key") or ""),
            symbol=symbol,
            instrument_type="perpetual" if market_type == "swap" else "spot",
            position_effect="reduce_only",
            position_side="long",
            side="sell",
        )
    config_payload = slow_execution_config_to_dict(default_config)
    config_payload["accounts"] = accounts
    return {
        "status": "ready" if accounts else "account_required",
        "mode": "owner_live",
        "config": config_payload,
        "accounts": accounts,
        "tasks": task_snapshot,
        "plan": None,
        "runtime": {},
        "error": None if accounts else "add and test a trading API account",
    }


async def _state_payload_for_request(request: web.Request) -> dict[str, Any]:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    view = request.query.get("view")
    sections = request.query.get("sections")
    payload = await state.get(view=view, sections=sections)
    runtime_cfg = await state.runtime_config(cfg)
    requesting_user = _request_user(request)
    owner_runtime_cfg: BotConfig | None = None
    owner_auto_buy_sell_payload: dict[str, Any] | None = None
    if requesting_user is not None and requesting_user.role != "admin":
        owner_runtime_cfg = _user_auto_buy_sell_runtime_config(
            request,
            requesting_user,
            runtime_cfg,
        )
        owner_auto_buy_sell_payload = await _user_auto_buy_sell_payload(
            request,
            requesting_user,
            runtime_cfg,
            runtime_cfg=owner_runtime_cfg,
        )
    payload["strategy_center"] = strategy_center_payload_for_view(
        build_strategy_center_payload(
            runtime_cfg,
            request.app["strategy_center_store"],
            user=requesting_user,
        ),
        view=view,
        sections=sections,
    )
    requested_sections = {
        item.strip() for item in str(sections or "").split(",") if item.strip()
    }
    workspace_payload: dict[str, Any] | None = None
    if (
        view in (None, "settings", "records")
        or (
            view == "trading"
            and (sections is None or "user-market-maker" in requested_sections)
        )
        or (
            view == "quant"
            and (
                sections is None
                or "backtest-points" in requested_sections
                or "user-quant-strategies" in requested_sections
            )
        )
    ):
        workspace_payload = build_user_workspace_payload(
            _user_workspace_store(request),
            user=requesting_user,
            paper_store=_user_paper_store(request),
        )
    elif requesting_user is not None:
        workspace_payload = _user_workspace_store(request).public_connections_payload(
            owner_email=requesting_user.email,
            is_admin=False,
        )
    if workspace_payload is not None:
        payload["user_workspace"] = workspace_payload
        market_maker_runtime = await state.market_maker_runtime()
        runtime_by_id = {
            str(row.get("id") or ""): row
            for row in market_maker_runtime.get("instances", [])
            if isinstance(row, dict) and row.get("id")
        }
        for strategy in workspace_payload.get("strategies", []):
            if not isinstance(strategy, dict):
                continue
            runtime_id = str(strategy.get("runtime_instance_id") or "")
            runtime = runtime_by_id.get(runtime_id)
            if runtime is not None:
                strategy["live_runtime"] = runtime
            elif strategy.get("enabled"):
                blockers = (strategy.get("readiness") or {}).get("blockers") or []
                strategy["live_runtime"] = {
                    "id": runtime_id,
                    "status": "blocked" if blockers else "starting",
                    "mode": "live",
                    "reason": blockers[0] if blockers else "runtime is starting",
                    "open_order_count": 0,
                }
        if requesting_user is not None and requesting_user.role != "admin":
            owner_order_activity = _owner_live_market_maker_order_activity(
                owner_runtime_cfg or runtime_cfg,
                workspace_payload,
            )
            payload["order_activity"] = owner_order_activity
            payload["trading_console"] = _owner_live_trading_console(
                owner_runtime_cfg or runtime_cfg,
                workspace_payload,
                owner_order_activity,
                owner_auto_buy_sell_payload or {},
            )
    permission_scope = (
        build_permission_scope(requesting_user, workspace_payload)
        if requesting_user is not None
        else None
    )
    if workspace_payload is not None and permission_scope is not None:
        workspace_payload["permissions"] = permission_scope
    if (
        requesting_user is not None
        and requesting_user.role == "admin"
        and view in (None, "settings")
    ):
        payload["admin_users"] = [
            _public_admin_user_dict(item) for item in _user_store(request).list_users()
        ]
    filtered = _filter_state_payload_for_user(
        payload,
        cfg=runtime_cfg,
        user=requesting_user,
    )
    if permission_scope is not None:
        filtered["auth"]["permission_model"] = permission_scope["model"]
        filtered["auth"]["permissions"] = permission_scope
    if requesting_user is not None and requesting_user.role != "admin":
        filtered["slow_execution"] = owner_auto_buy_sell_payload or {}
        config_payload = filtered.get("config")
        if isinstance(config_payload, dict):
            config_payload["risk"] = risk_config_to_dict(
                (owner_runtime_cfg or runtime_cfg).risk
            )
    if requesting_user is not None and workspace_payload is not None:
        filtered["account_balances"] = _merge_workspace_account_balances(
            filtered.get("account_balances"),
            workspace_payload,
        )
        filtered["portfolio"] = _sync_portfolio_with_account_balances(
            filtered.get("portfolio"),
            filtered.get("account_balances"),
            quote_rates=runtime_cfg.quote_rates,
        )
    return filtered


async def api_profile(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    user = _request_user(request)
    runtime_cfg = await state.runtime_config(cfg)
    if user is None:
        return web.json_response(
            {
                "mode": "legacy",
                "available_assets": _configured_assets(runtime_cfg),
            }
        )
    try:
        payload = await request.json()
        preferred_asset = str(payload.get("preferred_asset", "")).strip().upper()
        if preferred_asset and preferred_asset not in _configured_assets(runtime_cfg):
            raise ValueError(f"unknown asset: {preferred_asset}")
        updated = _user_store(request).update_profile(
            email=user.email,
            preferred_asset=preferred_asset,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="user_profile",
        target=updated.email,
        detail="updated preferred asset",
        payload={"preferred_asset": updated.preferred_asset},
    )
    return web.json_response(
        {
            "ok": True,
            "profile": updated.public_dict(
                available_assets=_configured_assets(runtime_cfg)
            ),
        }
    )


async def api_account(request: web.Request) -> web.Response:
    """Self-service account management: change email, delete account."""
    cfg: BotConfig = request.app["config"]
    user = _request_user(request)
    if user is None:
        return web.json_response(
            {"error": "a registered user session is required"},
            status=403,
        )
    store = _user_store(request)
    try:
        payload = await request.json()
        action = str(payload.get("action") or "")
        if action == "request_email_change":
            password = str(payload.get("password") or "")
            totp = str(payload.get("totp") or "")
            new_email = normalize_email(str(payload.get("new_email") or ""))
            if (
                store.authenticate(email=user.email, password=password, totp=totp)
                is None
            ):
                raise PermissionError("password confirmation failed")
            if store.get_user(new_email) is not None:
                raise ValueError("an account with the new email already exists")
            sender = _verification_email_sender(request)
            if not sender.configured():
                raise RuntimeError("email verification service is not configured")
            code = _verification_manager(request).issue(
                email=new_email,
                purpose="change_email",
                client_key=_client_ip(request, cfg) or "unknown",
            )
            try:
                await sender.send_code(
                    email=new_email,
                    code=code,
                    purpose="change_email",
                )
            except Exception:
                _verification_manager(request).discard(
                    email=new_email,
                    purpose="change_email",
                )
                raise RuntimeError("verification email could not be sent") from None
            write_web_audit_event(
                cfg,
                request,
                action="account_email_change_requested",
                target=user.email,
                detail=f"verification code sent to {new_email}",
            )
            return web.json_response({"ok": True, "code_sent": True})
        if action == "confirm_email_change":
            new_email = normalize_email(str(payload.get("new_email") or ""))
            code = str(payload.get("code") or "")
            if not _verification_manager(request).verify(
                email=new_email,
                purpose="change_email",
                code=code,
            ):
                raise PermissionError("verification code is invalid or expired")
            old_email = user.email
            moved = store.change_email(email=old_email, new_email=new_email)
            _reassign_user_data(request, old_email, new_email)
            write_web_audit_event(
                cfg,
                request,
                action="account_email_changed",
                target=new_email,
                detail=f"email changed from {old_email}",
            )
            response = web.json_response(
                {
                    "ok": True,
                    "email": moved.email,
                    # Sessions for the old identity stop validating and
                    # stored exchange credentials must be re-entered (their
                    # encryption is bound to the account email).
                    "reauth_required": True,
                    "credentials_reset": True,
                }
            )
            response.del_cookie(SESSION_COOKIE)
            return response
        if action == "delete_account":
            password = str(payload.get("password") or "")
            totp = str(payload.get("totp") or "")
            store.delete_own_account(
                email=user.email,
                password=password,
                totp=totp,
            )
            _purge_user_data(request, user.email)
            write_web_audit_event(
                cfg,
                request,
                action="account_deleted",
                target=user.email,
                detail="user deleted their own account",
            )
            response = web.json_response({"ok": True, "deleted": True})
            response.del_cookie(SESSION_COOKIE)
            return response
        raise ValueError("unsupported account action")
    except VerificationRateLimited as exc:
        response = web.json_response({"error": str(exc)}, status=429)
        response.headers["Retry-After"] = str(int(exc.retry_after + 0.999))
        return response
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


def _public_admin_user_dict(user: WebUser) -> dict[str, Any]:
    return {
        "email": user.email,
        "username": user.username,
        "role": user.role,
        "totp_enabled": user.totp_enabled,
        "allowed_assets": user.allowed_assets,
        "preferred_asset": user.preferred_asset,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


async def api_admin_users(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    store = _user_store(request)
    audit_action = ""
    audit_target = ""
    audit_detail = ""
    audit_payload: dict[str, Any] = {}
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "").strip().lower()
        email = str(payload.get("email") or "").strip()
        username = str(payload.get("username") or "").strip()

        if action == "list":
            pass
        elif action == "create_user":
            created = store.admin_create_user(
                email=email,
                username=username,
                password=str(payload.get("password") or ""),
                role=str(payload.get("role") or "user"),
                allowed_assets=payload.get("allowed_assets"),
                preferred_asset=str(payload.get("preferred_asset") or ""),
            )
            audit_action = "admin_user_create"
            audit_target = created.email
            audit_detail = f"created user with role {created.role}"
            audit_payload = {"email": created.email, "role": created.role}
        elif action == "update_user":
            if not email:
                raise ValueError("email is required")
            role_provided = "role" in payload
            username_provided = "username" in payload
            allowed_assets_provided = "allowed_assets" in payload
            preferred_asset_provided = "preferred_asset" in payload
            new_password = str(payload.get("new_password") or "")
            updated = store.admin_update_user(
                email=email,
                username=username if username_provided else None,
                role=str(payload.get("role") or "") if role_provided else None,
                allowed_assets=payload.get("allowed_assets"),
                allowed_assets_provided=allowed_assets_provided,
                preferred_asset=(
                    str(payload.get("preferred_asset") or "")
                    if preferred_asset_provided
                    else None
                ),
                preferred_asset_provided=preferred_asset_provided,
                new_password=new_password or None,
            )
            changes = [
                name
                for name, touched in (
                    ("role", role_provided),
                    ("username", username_provided),
                    ("assets", allowed_assets_provided or preferred_asset_provided),
                    ("password", bool(new_password)),
                )
                if touched
            ]
            audit_action = "admin_user_update"
            audit_target = updated.email
            audit_detail = "updated " + ", ".join(changes)
            audit_payload = {
                "email": updated.email,
                "role": updated.role,
                "allowed_assets": updated.allowed_assets,
                "preferred_asset": updated.preferred_asset,
                "changed_fields": changes,
            }
        elif action == "delete_user":
            if not email:
                raise ValueError("email is required")
            store.admin_delete_user(email=email)
            _purge_user_data(request, email)
            audit_action = "admin_user_delete"
            audit_target = email
            audit_detail = "deleted user"
            audit_payload = {"email": email}
        else:
            raise ValueError("unsupported admin users action")
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if audit_action:
        write_web_audit_event(
            cfg,
            request,
            action=audit_action,
            target=audit_target,
            detail=audit_detail,
            payload=audit_payload,
        )
    return web.json_response(
        {
            "ok": True,
            "users": [_public_admin_user_dict(item) for item in store.list_users()],
        }
    )
