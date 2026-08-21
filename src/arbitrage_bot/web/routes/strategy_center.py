from __future__ import annotations

import hmac
import json
import os
from typing import Any

from aiohttp import web

from ..state import MonitorState
from ..user_scope import (
    _base_asset_from_symbol,
    _require_admin_user,
    _require_user_assets,
)

from ..security import (
    _client_ip,
    _request_user,
    _require_owner_or_admin,
    _strategy_center_store,
    write_system_web_audit_event,
    write_web_audit_event,
)

from ...config import (
    BotConfig,
)
from ...strategy_center import (
    FundingArbitrageSettings,
    SignalBotSettings,
    SignalEvent,
    build_strategy_center_public_payload,
)


from .workspace import (
    _api_account_payload_from_request,
    _strategy_center_existing_row,
    _strategy_center_optional_row,
    _strategy_payload_from_request,
)


async def api_strategy_center(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    store = _strategy_center_store(request)
    user = _request_user(request)
    try:
        _require_admin_user(user)
        if not cfg.strategy_center.enabled:
            raise ValueError("strategy center is disabled")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            raise ValueError("action is required")
        runtime_cfg = await state.runtime_config(cfg)
        store_payload = store.read()

        if action in {"create_strategy", "update_strategy", "upsert_strategy"}:
            existing = None
            strategy_id = str(
                payload.get("id") or payload.get("strategy_id") or ""
            ).strip()
            strategy_raw = payload.get("strategy")
            if isinstance(strategy_raw, dict):
                strategy_id = str(strategy_raw.get("id") or strategy_id).strip()
            if action == "update_strategy" and strategy_id:
                existing = _strategy_center_existing_row(
                    store_payload["strategy_instances"],
                    strategy_id,
                    label="strategy",
                )
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            elif action == "upsert_strategy" and strategy_id:
                existing = _strategy_center_optional_row(
                    store_payload["strategy_instances"],
                    strategy_id,
                )
            if existing is not None:
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            strategy = _strategy_payload_from_request(
                payload,
                user=user,
                existing=existing,
            )
            store_payload = store.upsert_strategy(strategy)
            audit_action = "strategy_center_strategy"
            target = strategy.id
            detail = f"{action} {strategy.name}"
            audit_payload = strategy.summary()
        elif action == "delete_strategy":
            strategy_id = str(
                payload.get("id") or payload.get("strategy_id") or ""
            ).strip()
            if not strategy_id:
                raise ValueError("strategy_id is required")
            existing = _strategy_center_existing_row(
                store_payload["strategy_instances"],
                strategy_id,
                label="strategy",
            )
            _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            store_payload = store.delete_strategy(strategy_id)
            audit_action = "strategy_center_strategy_delete"
            target = strategy_id
            detail = "deleted strategy instance"
            audit_payload = {"strategy_id": strategy_id}
        elif action in {"create_account", "update_account", "upsert_account"}:
            existing = None
            account_id = str(
                payload.get("id") or payload.get("account_id") or ""
            ).strip()
            account_raw = payload.get("account")
            if isinstance(account_raw, dict):
                account_id = str(account_raw.get("id") or account_id).strip()
            if action == "update_account" and account_id:
                existing = _strategy_center_existing_row(
                    store_payload["user_api_accounts"],
                    account_id,
                    label="api account",
                )
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            elif action == "upsert_account" and account_id:
                existing = _strategy_center_optional_row(
                    store_payload["user_api_accounts"],
                    account_id,
                )
            if existing is not None:
                _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            account = _api_account_payload_from_request(
                payload,
                user=user,
                existing=existing,
            )
            store_payload = store.upsert_api_account(account)
            audit_action = "strategy_center_api_account"
            target = account.id
            detail = f"{action} {account.label}"
            audit_payload = account.public_dict()
        elif action == "delete_account":
            account_id = str(
                payload.get("id") or payload.get("account_id") or ""
            ).strip()
            if not account_id:
                raise ValueError("account_id is required")
            existing = _strategy_center_existing_row(
                store_payload["user_api_accounts"],
                account_id,
                label="api account",
            )
            _require_owner_or_admin(user, str(existing.get("owner_email") or ""))
            store_payload = store.delete_api_account(account_id)
            audit_action = "strategy_center_api_account_delete"
            target = account_id
            detail = "deleted api account reference"
            audit_payload = {"account_id": account_id}
        elif action == "update_funding":
            raw = (
                payload.get("funding_arbitrage")
                if isinstance(payload.get("funding_arbitrage"), dict)
                else payload
            )
            funding = FundingArbitrageSettings.from_dict(raw)
            _require_user_assets(
                user,
                [
                    _base_asset_from_symbol(funding.spot_symbol),
                    _base_asset_from_symbol(funding.derivative_symbol),
                ],
            )
            store_payload = store.update_funding(funding)
            audit_action = "strategy_center_funding"
            target = funding.pair_id or funding.spot_symbol
            detail = "updated funding arbitrage settings"
            audit_payload = funding.to_dict()
        elif action == "update_signal_bot":
            raw = (
                payload.get("signal_bot")
                if isinstance(payload.get("signal_bot"), dict)
                else payload
            )
            signal_bot = SignalBotSettings.from_dict(raw)
            store_payload = store.update_signal_bot(signal_bot)
            audit_action = "strategy_center_signal_bot"
            target = "signal_bot"
            detail = "updated signal bot settings"
            audit_payload = signal_bot.to_dict()
        else:
            raise ValueError("unsupported strategy center action")
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    write_web_audit_event(
        runtime_cfg,
        request,
        action=audit_action,
        target=target,
        detail=detail,
        payload=audit_payload,
    )
    return web.json_response(
        {
            "ok": True,
            "strategy_center": build_strategy_center_public_payload(
                store_payload,
                current_user_email=user.email if user else "",
                current_user_role=user.role if user else "admin",
                allowed_assets=user.allowed_assets if user else [],
            ),
        }
    )


async def _json_or_text_payload(request: web.Request) -> dict[str, Any]:
    content_type = request.content_type.lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("signal payload must be an object")
        return payload
    text_payload = (await request.text()).strip()
    if not text_payload:
        return {}
    try:
        payload = json.loads(text_payload)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"message": text_payload}


def _signal_secret_from_request(
    request: web.Request,
    payload: dict[str, Any],
) -> str:
    return str(
        request.headers.get("X-Signal-Secret")
        or request.headers.get("X-Webhook-Secret")
        or request.query.get("secret")
        or payload.get("secret")
        or ""
    )


def _signal_payload_without_secret(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if str(key).lower() not in {"secret", "token", "webhook_secret"}
    }


async def api_signal_webhook(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    store = _strategy_center_store(request)
    source = str(request.match_info.get("source") or "custom").strip().lower()
    try:
        if not cfg.strategy_center.enabled:
            raise PermissionError("strategy center is disabled")
        payload = await _json_or_text_payload(request)
        store_payload = store.read()
        signal_bot = SignalBotSettings.from_dict(store_payload.get("signal_bot", {}))
        if not signal_bot.enabled:
            raise PermissionError("signal bot is disabled")
        if source == "custom" and not signal_bot.allow_custom_webhook:
            raise PermissionError("custom webhook is disabled")
        if source not in signal_bot.allowed_sources:
            raise PermissionError(f"signal source is not allowed: {source}")
        expected_secret = (
            os.environ.get(signal_bot.webhook_secret_env)
            if signal_bot.webhook_secret_env
            else None
        )
        if not expected_secret:
            raise PermissionError(
                "signal webhook secret environment variable is not set"
            )
        supplied_secret = _signal_secret_from_request(request, payload)
        if not hmac.compare_digest(supplied_secret, expected_secret):
            raise PermissionError("invalid signal webhook secret")

        clean_payload = _signal_payload_without_secret(payload)
        strategy_id = str(
            clean_payload.get("strategy_id") or signal_bot.default_strategy_id or ""
        ).strip()
        strategies = {
            str(item.get("id")): item
            for item in store_payload.get("strategy_instances", [])
            if isinstance(item, dict)
        }
        strategy = strategies.get(strategy_id) if strategy_id else None
        status = "accepted"
        reason = "stored only; execution requires strategy runner and risk approval"
        if strategy_id and strategy is None:
            status = "blocked"
            reason = "strategy_id is not registered"
        elif strategy is not None and not bool(strategy.get("enabled")):
            status = "blocked"
            reason = "strategy is disabled"
        event = SignalEvent.from_payload(
            clean_payload,
            source=source,
            default_strategy_id=signal_bot.default_strategy_id,
            status=status,
            reason=reason,
        )
        store_payload = store.append_signal(event)
    except PermissionError as exc:
        write_system_web_audit_event(
            cfg,
            action="signal_webhook",
            status="blocked",
            target=source,
            detail="rejected signal webhook",
            payload={},
            error=str(exc),
            actor_ip=_client_ip(request, cfg),
            path=request.path,
            method=request.method,
            user_agent=str(request.headers.get("User-Agent", ""))[:160],
        )
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    write_system_web_audit_event(
        cfg,
        action="signal_webhook",
        target=event.id,
        detail=f"received {source} signal",
        payload={
            "id": event.id,
            "source": event.source,
            "strategy_id": event.strategy_id,
            "symbol": event.symbol,
            "side": event.side,
            "action": event.action,
            "status": event.status,
            "reason": event.reason,
        },
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )
    return web.json_response(
        {
            "ok": True,
            "signal": event.to_dict(),
            "strategy_center": build_strategy_center_public_payload(store_payload),
        }
    )
