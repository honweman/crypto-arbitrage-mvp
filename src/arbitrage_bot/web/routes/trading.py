from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Any

from aiohttp import web

from ..constants import (
    LIVE_AUTO_BUY_SELL_CONFIRMATION,
    LIVE_MARKET_MAKER_CONFIRMATION,
    STRATEGY_IDS,
)
from ..state import MonitorState
from ..user_scope import (
    _assets_from_cash_and_carry_pairs,
    _assets_from_spot_markets,
    _base_asset_from_symbol,
    _require_admin_user,
    _require_user_assets,
)

from ..permissions import (
    require_capability,
    require_owned_exchange,
)
from ..security import (
    _request_user,
    _user_workspace_store,
    write_web_audit_event,
)

from ...auto_buy_sell_task import (
    AutoBuySellTaskService,
    TERMINAL_TASK_STATUSES,
    validate_task_config,
    validate_task_exchange_config,
)
from ...config import (
    BotConfig,
    MarketMakerConfig,
    SlowExecutionConfig,
)
from ...exchanges import ExchangeManager
from ...web_config import (
    _auto_buy_sell_symbols_by_exchange,
    _cash_and_carry_pairs_from_payload,
    _risk_overrides_from_payload,
    _slow_execution_overrides_from_payload,
    _spot_markets_from_payload,
    auto_buy_sell_exchanges,
    cash_and_carry_pairs_to_list,
    market_maker_config_to_dict,
    market_maker_config_from_payload,
    market_maker_configs_for_runtime,
    market_maker_configs_from_payload,
    market_maker_configs_to_list,
    market_maker_symbols_for_accounts,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_markets_to_list,
)


from ..core import (
    _all_account_exchanges,
    _config_actor_email,
    _find_exchange_by_key,
    cancel_bulk_orders_payload,
    cancel_order_payload,
    fetch_order_activity_payload,
)
from .control import (
    _consume_strategy_preflight,
    _require_user_owned_execution,
    _schedule_started_config_guard,
)
from .profile import _user_auto_buy_sell_runtime_config


async def _cleanup_market_maker_instance(
    cfg: BotConfig,
    state: MonitorState,
    instance: MarketMakerConfig,
) -> dict[str, Any]:
    runtime_cfg = await state.runtime_config(cfg)
    exchange_cfg = next(
        (
            account
            for account in _all_account_exchanges(runtime_cfg)
            if account.key == instance.exchange
        ),
        None,
    )
    if exchange_cfg is None:
        return {
            "status": "blocked",
            "reason": f"market maker account is not configured: {instance.exchange}",
            "exchange": instance.exchange,
            "symbol": instance.symbol,
        }
    manager = ExchangeManager()
    try:
        result = await manager.cleanup_market_maker_market(
            exchange_cfg,
            symbol=instance.symbol,
            client_order_prefix=instance.client_order_prefix,
        )
        recovery = result.get("recovery")
        if isinstance(recovery, dict):
            await state.set_order_reliability(recovery)
        return result
    finally:
        await manager.close()


def _schedule_market_maker_cleanup(
    request: web.Request,
    *,
    cfg: BotConfig,
    state: MonitorState,
    instances: list[MarketMakerConfig],
) -> None:
    tasks: set[asyncio.Task[Any]] = request.app.setdefault("config_guard_tasks", set())
    for instance in instances:
        task = asyncio.create_task(_cleanup_market_maker_instance(cfg, state, instance))
        tasks.add(task)
        task.add_done_callback(tasks.discard)


async def api_market_maker(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    guard_baseline: dict[str, Any] | None = None
    guard_instance_id = ""
    start_cleanup: dict[str, Any] | None = None
    stopping_instances: list[MarketMakerConfig] = []
    cleanup_recoverable_state = False
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        cleanup_recoverable_state = bool(
            isinstance(payload, dict)
            and payload.get("cleanup_recoverable_state") is True
        )
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = market_maker_symbols_for_accounts(
            runtime_cfg,
            base_cfg=cfg,
        )
        allowed_exchanges = {
            exchange.key for exchange in _all_account_exchanges(runtime_cfg)
        }
        current_instances = market_maker_configs_for_runtime(runtime_cfg)
        action = "upsert"
        if isinstance(payload, dict) and "instances" in payload:
            updated_instances = market_maker_configs_from_payload(
                payload["instances"],
                base_configs=current_instances,
                allowed_exchanges=allowed_exchanges,
                symbols_by_exchange=symbols_by_exchange,
                repair_stale_identity_id=True,
                normalize_identity_id=cleanup_recoverable_state,
            )
            action = "replace"
        elif isinstance(payload, dict) and payload.get("copy_id"):
            copy_id = str(payload["copy_id"]).strip()
            source = next(
                (instance for instance in current_instances if instance.id == copy_id),
                None,
            )
            if source is None:
                raise ValueError(f"market maker instance not found: {copy_id}")
            new_id = str(
                payload.get("new_id") or f"{source.id[:52]}-copy-{int(time.time())}"
            ).strip()
            if any(instance.id == new_id for instance in current_instances):
                raise ValueError(f"market maker instance already exists: {new_id}")
            copied = replace(
                source,
                id=new_id,
                enabled=False,
                live_enabled=False,
            )
            updated_instances = [*current_instances, copied]
            action = "copy"
        elif isinstance(payload, dict) and payload.get("delete_id"):
            delete_id = str(payload["delete_id"]).strip()
            updated_instances = [
                instance for instance in current_instances if instance.id != delete_id
            ]
            action = "delete"
        else:
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            target_id = str(payload.get("id") or "").strip()
            base_config = next(
                (
                    instance
                    for instance in current_instances
                    if instance.id == target_id
                ),
                current_instances[0] if current_instances else None,
            )
            updated_config = market_maker_config_from_payload(
                payload,
                base_config=base_config,
                allowed_exchanges=allowed_exchanges,
                symbols_by_exchange=symbols_by_exchange,
                repair_stale_identity_id=True,
                normalize_identity_id=cleanup_recoverable_state,
            )
            replaced_instance = False
            updated_instances = []
            for instance in current_instances:
                if (target_id and instance.id == target_id) or (
                    not target_id and instance.id == updated_config.id
                ):
                    updated_instances.append(updated_config)
                    replaced_instance = True
                else:
                    updated_instances.append(instance)
            if not replaced_instance:
                updated_instances.append(updated_config)
        current_by_id = {instance.id: instance for instance in current_instances}
        stopping_instances = [
            instance
            for instance_id, instance in current_by_id.items()
            if instance.enabled
            and instance.live_enabled
            and (
                not any(
                    updated.enabled
                    and updated.live_enabled
                    and (
                        updated.id == instance_id
                        or (
                            updated.exchange == instance.exchange
                            and updated.symbol == instance.symbol
                        )
                    )
                    for updated in updated_instances
                )
            )
        ]
        live_changes_requiring_confirmation = [
            instance
            for instance in updated_instances
            if instance.enabled
            and instance.live_enabled
            and not (
                current_by_id.get(instance.id)
                and current_by_id[instance.id].enabled
                and current_by_id[instance.id].live_enabled
                and current_by_id[instance.id] == instance
            )
        ]
        if live_changes_requiring_confirmation and (
            not isinstance(payload, dict)
            or payload.get("confirm_live") != LIVE_MARKET_MAKER_CONFIRMATION
        ):
            raise ValueError(
                "starting or changing live Market Maker requires "
                f"confirm_live={LIVE_MARKET_MAKER_CONFIRMATION}"
            )
        if live_changes_requiring_confirmation and not cleanup_recoverable_state:
            raise ValueError(
                "starting or changing live Market Maker requires "
                "cleanup_recoverable_state=true"
            )
        if len(live_changes_requiring_confirmation) > 1:
            raise ValueError("start one live Market Maker instance at a time")
        if live_changes_requiring_confirmation:
            _consume_strategy_preflight(
                request,
                strategy_id="market_maker",
                candidate=market_maker_config_to_dict(
                    live_changes_requiring_confirmation[0]
                ),
                token=str(payload.get("preflight_token") or ""),
            )
            guard_baseline = await state.config_versions(limit=1)
            guard_instance_id = live_changes_requiring_confirmation[0].id
        _require_user_assets(
            _request_user(request),
            [
                _base_asset_from_symbol(instance.symbol)
                for instance in updated_instances
                if instance.symbol
            ],
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if live_changes_requiring_confirmation and cleanup_recoverable_state:
        start_cleanup = await _cleanup_market_maker_instance(
            cfg,
            state,
            live_changes_requiring_confirmation[0],
        )
        if start_cleanup.get("status") != "ok":
            return web.json_response(
                {
                    "error": str(
                        start_cleanup.get("reason")
                        or "market maker restart cleanup did not complete"
                    ),
                    "cleanup": start_cleanup,
                },
                status=409,
            )

    update = await state.set_market_maker_instances(
        updated_instances,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action=f"market_maker_{action}",
    )
    if stopping_instances and cleanup_recoverable_state:
        _schedule_market_maker_cleanup(
            request,
            cfg=cfg,
            state=state,
            instances=stopping_instances,
        )
    if guard_baseline is not None:
        current_version = await state.config_versions(limit=1)
        if current_version.get("current_version_id") != guard_baseline.get(
            "current_version_id"
        ):
            _schedule_started_config_guard(
                request,
                strategy_id="market_maker",
                instance_id=guard_instance_id,
                previous_version_id=guard_baseline.get("current_version_id"),
                expected_current_hash=str(current_version.get("current_hash") or ""),
            )
    runtime_cfg = await state.runtime_config(cfg)
    current_instances = market_maker_configs_for_runtime(runtime_cfg)
    current_config = (
        current_instances[0] if current_instances else runtime_cfg.market_maker
    )
    write_web_audit_event(
        runtime_cfg,
        request,
        action="market_maker_config",
        target=", ".join(
            f"{instance.id}:{instance.exchange} {instance.symbol}".strip()
            for instance in current_instances
        ),
        detail=f"{action} Market Maker config",
        payload={
            "action": action,
            "instances": market_maker_configs_to_list(current_instances),
        },
    )
    return web.json_response(
        {
            "ok": True,
            "config": market_maker_config_to_dict(current_config),
            "instances": market_maker_configs_to_list(current_instances),
            "cleanup": start_cleanup,
            **update,
        }
    )


async def api_markets(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        markets = _spot_markets_from_payload(
            payload,
            allowed_exchanges={exchange.key for exchange in cfg.spot_exchanges},
        )
        _require_user_assets(_request_user(request), _assets_from_spot_markets(markets))
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    result = await state.set_spot_markets(
        markets,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="spot_markets_update",
    )
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="markets_config",
        target="spot_markets",
        detail=f"set {len(markets)} spot market(s)",
        payload={"spot_markets": spot_markets_to_list(markets)},
    )
    return web.json_response(result)


async def api_cash_and_carry_pairs(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        pairs = _cash_and_carry_pairs_from_payload(payload)
        _require_user_assets(
            _request_user(request),
            _assets_from_cash_and_carry_pairs(pairs),
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    result = await state.set_cash_and_carry_pairs(
        pairs,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="cash_and_carry_pairs_update",
    )
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="cash_and_carry_config",
        target="cash_and_carry_pairs",
        detail=f"set {len(pairs)} pair(s)",
        payload={"cash_and_carry_pairs": cash_and_carry_pairs_to_list(pairs)},
    )
    return web.json_response(result)


async def api_create_auto_buy_sell_task(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    guard_baseline: dict[str, Any] | None = None
    try:
        user = _request_user(request)
        require_capability(user, "strategy.manage")
        require_capability(user, "account.trade")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if payload.get("confirm_live") != LIVE_AUTO_BUY_SELL_CONFIRMATION:
            raise ValueError(
                "starting Auto Buy/Sell requires "
                f"confirm_live={LIVE_AUTO_BUY_SELL_CONFIRMATION}"
            )
        platform_cfg = await state.runtime_config(cfg)
        runtime_cfg = (
            _user_auto_buy_sell_runtime_config(request, user, platform_cfg)
            if user is not None and user.role != "admin"
            else platform_cfg
        )
        symbols_by_exchange = _auto_buy_sell_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            auto_buy_sell_exchanges(runtime_cfg),
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        allowed_exchanges = {account["key"] for account in accounts}
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges=allowed_exchanges,
            symbols_by_exchange=symbols_by_exchange,
        )
        base_config = (
            SlowExecutionConfig()
            if user is not None and user.role != "admin"
            else await state.slow_execution_config(cfg.slow_execution)
        )
        task_config = replace(base_config, **{**overrides, "enabled": True})
        _require_user_owned_execution(user, task_config)
        if user is None or user.role == "admin":
            _require_user_assets(user, [_base_asset_from_symbol(task_config.symbol)])
        validate_task_config(task_config)
        validate_task_exchange_config(runtime_cfg, task_config)
        require_owned_exchange(
            user, _find_exchange_by_key(runtime_cfg, task_config.exchange)
        )
        if user is not None and user.role != "admin":
            profile = _user_workspace_store(request).risk_profile(user.email)
            if profile.max_active_strategies > 0:
                owner_tasks = await tasks.snapshot(
                    owner_email=user.email,
                    is_admin=False,
                )
                if (
                    int(owner_tasks.get("active_count") or 0)
                    >= profile.max_active_strategies
                ):
                    active_rows = [
                        row
                        for row in owner_tasks.get("tasks", [])
                        if isinstance(row, dict)
                        and row.get("status") not in TERMINAL_TASK_STATUSES
                    ]
                    active_summary = ", ".join(
                        f"{str(row.get('id') or '')[:8]} "
                        f"{(row.get('config') or {}).get('symbol') or '--'} "
                        f"({row.get('status') or 'unknown'})"
                        for row in active_rows[:4]
                    )
                    raise ValueError(
                        "your active Auto Buy/Sell task limit is "
                        f"{profile.max_active_strategies}; your unfinished tasks: "
                        f"{active_summary or 'unavailable'}. Stop or finish one "
                        "before starting another"
                    )
        _consume_strategy_preflight(
            request,
            strategy_id="slow_execution",
            candidate=slow_execution_config_to_dict(task_config),
            token=str(payload.get("preflight_token") or ""),
        )
        if user is None or user.role == "admin":
            guard_baseline = await state.config_versions(limit=1)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    try:
        task = await tasks.create_task(
            task_config,
            owner_email=(
                user.email if user is not None and user.role != "admin" else ""
            ),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if user is None or user.role == "admin":
        await state.set_slow_execution_overrides(
            {
                **overrides,
                "enabled": True,
            },
            cfg=cfg,
            actor_email=_config_actor_email(request),
            action="auto_buy_sell_task_create",
        )
    if guard_baseline is not None:
        current_version = await state.config_versions(limit=1)
        if current_version.get("current_version_id") != guard_baseline.get(
            "current_version_id"
        ):
            _schedule_started_config_guard(
                request,
                strategy_id="slow_execution",
                instance_id=str(task.get("id") or ""),
                previous_version_id=guard_baseline.get("current_version_id"),
                expected_current_hash=str(current_version.get("current_hash") or ""),
            )
    all_tasks_snapshot = await tasks.snapshot()
    await state.set_auto_buy_sell_tasks(all_tasks_snapshot)
    snapshot = await tasks.snapshot(
        owner_email=user.email if user is not None else None,
        is_admin=user is None or user.role == "admin",
    )
    runtime_cfg = (
        await state.runtime_config(cfg)
        if user is None or user.role == "admin"
        else runtime_cfg
    )
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_task_create",
        target=f"{task_config.exchange} {task_config.symbol}",
        detail=f"created task {task.get('id', '')}",
        payload={
            "task_id": task.get("id"),
            "config": slow_execution_config_to_dict(task_config),
        },
    )
    return web.json_response(
        {
            "ok": True,
            "task": task,
            "tasks": snapshot,
            "config": slow_execution_config_to_dict(task_config),
        }
    )


async def api_control_auto_buy_sell_task(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    task_id = request.match_info.get("task_id", "")
    try:
        user = _request_user(request)
        require_capability(user, "strategy.manage")
        require_capability(user, "account.trade")
        payload = await request.json()
        action = str(payload.get("action", "")).strip().lower()
        if action not in {"pause", "resume", "stop", "enable_mm_coordination"}:
            raise ValueError(
                "action must be pause, resume, stop, or enable_mm_coordination"
            )
        task_snapshot = await tasks.snapshot(
            owner_email=user.email if user is not None else None,
            is_admin=user is None or user.role == "admin",
        )
        task_row = next(
            (
                item
                for item in task_snapshot.get("tasks", [])
                if isinstance(item, dict) and item.get("id") == task_id
            ),
            None,
        )
        if isinstance(task_row, dict):
            task_config = (
                task_row.get("config")
                if isinstance(task_row.get("config"), dict)
                else {}
            )
            if user is None or user.role == "admin":
                _require_user_assets(
                    user,
                    [_base_asset_from_symbol(str(task_config.get("symbol") or ""))],
                )
        if action == "enable_mm_coordination":
            if payload.get("confirm_live") != LIVE_AUTO_BUY_SELL_CONFIRMATION:
                raise ValueError(
                    "enabling live MM coordination requires "
                    f"confirm_live={LIVE_AUTO_BUY_SELL_CONFIRMATION}"
                )
            task = await tasks.enable_market_maker_coordination(
                task_id,
                owner_email=user.email if user is not None else None,
                is_admin=user is None or user.role == "admin",
            )
        elif action == "stop":
            manager = ExchangeManager()
            platform_cfg = await state.runtime_config(cfg)
            runtime_cfg = (
                _user_auto_buy_sell_runtime_config(request, user, platform_cfg)
                if user is not None and user.role != "admin"
                else platform_cfg
            )
            cancel_open_orders = bool(payload.get("cancel_open_orders", True))
            try:
                task = await tasks.stop_task(
                    task_id,
                    runtime_cfg,
                    manager,
                    cancel_open_orders=cancel_open_orders,
                    owner_email=user.email if user is not None else None,
                    is_admin=user is None or user.role == "admin",
                )
            finally:
                await manager.close()
        else:
            task = await tasks.set_paused(
                task_id,
                action == "pause",
                owner_email=user.email if user is not None else None,
                is_admin=user is None or user.role == "admin",
            )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    all_tasks_snapshot = await tasks.snapshot()
    await state.set_auto_buy_sell_tasks(all_tasks_snapshot)
    snapshot = await tasks.snapshot(
        owner_email=user.email if user is not None else None,
        is_admin=user is None or user.role == "admin",
    )
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_task_control",
        target=task_id,
        detail=f"{action} task",
        payload={"task_id": task_id, "action": action},
    )
    return web.json_response(
        {
            "ok": True,
            "task": task,
            "tasks": snapshot,
        }
    )


async def api_cleanup_auto_buy_sell_tasks(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    tasks: AutoBuySellTaskService = request.app["auto_buy_sell_tasks"]
    try:
        user = _request_user(request)
        require_capability(user, "strategy.manage")
        payload = await request.json()
        if not bool(payload.get("terminal_only", True)):
            raise ValueError("only terminal task cleanup is supported")
        preview_only = bool(payload.get("preview") or payload.get("dry_run"))
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if preview_only:
        result = await tasks.preview_terminal_tasks(
            owner_email=user.email if user is not None else None,
            is_admin=user is None or user.role == "admin",
        )
        return web.json_response({"ok": True, "preview": True, **result})

    result = await tasks.clear_terminal_tasks(
        owner_email=user.email if user is not None else None,
        is_admin=user is None or user.role == "admin",
    )
    await state.set_auto_buy_sell_tasks(await tasks.snapshot())
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_task_cleanup",
        target="terminal_tasks",
        detail=f"removed {result['removed_count']} terminal task(s)",
        payload={
            "removed_count": result["removed_count"],
            "removed_task_ids": result["removed_task_ids"],
        },
    )
    return web.json_response({"ok": True, **result})


async def api_risk(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        allowed_accounts = {
            exchange.key for exchange in _all_account_exchanges(runtime_cfg)
        }
        overrides = _risk_overrides_from_payload(
            payload,
            allowed_accounts=allowed_accounts,
            allowed_strategies=STRATEGY_IDS,
        )
        enabling_live = bool(
            overrides.get("allow_live_trading") is True
            and not runtime_cfg.risk.allow_live_trading
        )
        enabling_auto_hedge = bool(
            overrides.get("auto_hedge_live_enabled") is True
            and not runtime_cfg.risk.auto_hedge_live_enabled
        )
        if (enabling_live or enabling_auto_hedge) and payload.get(
            "confirm_live_risk"
        ) is not True:
            raise ValueError(
                "enabling live trading or automatic hedge requires "
                "confirm_live_risk=true"
            )
        effective_risk = replace(runtime_cfg.risk, **overrides)
        if effective_risk.auto_hedge_live_enabled:
            if effective_risk.max_auto_hedge_quote <= 0:
                raise ValueError(
                    "max_auto_hedge_quote must be positive when auto hedge is live"
                )
            if effective_risk.auto_hedge_max_attempts <= 0:
                raise ValueError(
                    "auto_hedge_max_attempts must be positive when auto hedge is live"
                )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_risk_overrides(
        overrides,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="risk_update",
    )
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="risk_config",
        target="risk",
        detail="updated risk controls",
        payload=overrides,
    )
    return web.json_response({"ok": True, **update})


async def api_config_versions_get(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    try:
        _require_admin_user(_request_user(request))
        limit = int(request.query.get("limit", "30"))
        payload = await state.config_versions(limit=limit)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"ok": True, **payload})


async def api_config_versions_post(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        user = _request_user(request)
        _require_admin_user(user)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(payload.get("action") or "").strip().lower()
        if action != "rollback":
            raise ValueError("action must be rollback")
        if payload.get("confirm") is not True:
            raise ValueError("rollback requires confirm=true")
        version_id = int(payload.get("version_id") or 0)
        if version_id <= 0:
            raise ValueError("version_id must be positive")
        result = await state.rollback_config_version(
            version_id,
            expected_current_hash=str(payload.get("current_hash") or ""),
            actor_email=user.email if user is not None else "legacy-admin",
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="config_rollback",
        target=str(result["rolled_back_to"]),
        detail=f"rolled back runtime configuration to version {result['rolled_back_to']}",
        payload={
            "version_id": result["rolled_back_to"],
            "current_hash": result["current_hash"],
        },
    )
    return web.json_response(result)


async def api_cancel_order(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        user = _request_user(request)
        require_capability(user, "account.trade")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        platform_cfg = await state.runtime_config(cfg)
        runtime_cfg = (
            _user_auto_buy_sell_runtime_config(request, user, platform_cfg)
            if user is not None and user.role != "admin"
            else platform_cfg
        )
        exchange = _find_exchange_by_key(
            runtime_cfg,
            str(payload.get("exchange") or "").strip(),
        )
        require_owned_exchange(user, exchange)
        if user is None or user.role == "admin":
            _require_user_assets(
                user,
                [_base_asset_from_symbol(str(payload.get("symbol") or ""))],
            )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    manager = ExchangeManager()
    try:
        runtime_slow_execution = runtime_cfg.slow_execution
        result = await cancel_order_payload(
            runtime_cfg,
            manager,
            payload,
            runtime_slow_execution,
        )
        order_activity = await fetch_order_activity_payload(
            runtime_cfg,
            manager,
            runtime_slow_execution,
        )
        if user is None or user.role == "admin":
            await state.set_order_activity(order_activity)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"{exc.__class__.__name__}: {exc}"},
            status=500,
        )
    finally:
        await manager.close()

    result["order_activity"] = order_activity
    return web.json_response(result)


async def api_cancel_bulk_orders(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        user = _request_user(request)
        require_capability(user, "account.trade")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        platform_cfg = await state.runtime_config(cfg)
        runtime_cfg = (
            _user_auto_buy_sell_runtime_config(request, user, platform_cfg)
            if user is not None and user.role != "admin"
            else platform_cfg
        )
        exchange_key = str(payload.get("exchange") or "").strip()
        if exchange_key:
            require_owned_exchange(
                user, _find_exchange_by_key(runtime_cfg, exchange_key)
            )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    manager = ExchangeManager()
    try:
        runtime_slow_execution = runtime_cfg.slow_execution
        result = await cancel_bulk_orders_payload(
            runtime_cfg,
            manager,
            payload,
            runtime_slow_execution,
        )
        order_activity = await fetch_order_activity_payload(
            runtime_cfg,
            manager,
            runtime_slow_execution,
        )
        if user is None or user.role == "admin":
            await state.set_order_activity(order_activity)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"{exc.__class__.__name__}: {exc}"},
            status=500,
        )
    finally:
        await manager.close()

    result["order_activity"] = order_activity
    return web.json_response(result)


async def api_strategy_control(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        strategy_id = str(payload.get("strategy", "")).strip()
        paused = payload.get("paused")
        if not strategy_id:
            raise ValueError("strategy is required")
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean")
        trading_console = await state.set_strategy_paused(
            strategy_id,
            paused,
            cfg=cfg,
            actor_email=_config_actor_email(request),
            action="strategy_pause" if paused else "strategy_resume",
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    runtime_cfg = await state.runtime_config(cfg)
    lifecycle = await state.strategy_lifecycle()
    write_web_audit_event(
        runtime_cfg,
        request,
        action="strategy_control",
        target=strategy_id,
        detail="paused strategy" if paused else "resumed strategy",
        payload={"strategy": strategy_id, "paused": paused},
    )
    return web.json_response(
        {
            "ok": True,
            "strategy": strategy_id,
            "paused": paused,
            "trading_console": trading_console,
            "strategy_lifecycle": lifecycle,
        }
    )
