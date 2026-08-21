from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

from aiohttp import web

from ..state import MonitorState
from ..strategy_preflight import (
    StrategyPreflightService,
    build_strategy_preflight,
)
from ..user_scope import (
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
    validate_task_config,
    validate_task_exchange_config,
)
from ...config import (
    BotConfig,
    SlowExecutionConfig,
)
from ...cross_exchange_rebalancer import (
    load_rebalance_runtime,
    new_rebalance_runtime,
    save_rebalance_runtime,
)
from ...strategy_timeline import (
    find_latest_strategy_timeline_entry,
)
from ...web_config import (
    _backtest_overrides_from_payload,
    _auto_buy_sell_symbols_by_exchange,
    _dca_overrides_from_payload,
    _execution_algo_overrides_from_payload,
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    _rebalance_symbols_by_exchange,
    _slow_execution_overrides_from_payload,
    _spot_grid_overrides_from_payload,
    backtest_config_to_dict,
    auto_buy_sell_exchanges,
    cross_exchange_rebalance_config_from_payload,
    cross_exchange_rebalance_config_to_dict,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    slow_execution_accounts,
    slow_execution_config_to_dict,
    spot_grid_config_to_dict,
)


from ..core import _config_actor_email, _find_exchange_by_key
from .control import (
    _consume_strategy_preflight,
    _preflight_candidate_from_payload,
    _require_user_owned_execution,
    _schedule_started_config_guard,
    _user_execution_preflight,
)
from .profile import _user_auto_buy_sell_runtime_config


async def api_strategy_preflight(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        user = _request_user(request)
        require_capability(user, "strategy.manage")
        require_capability(user, "account.trade")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        strategy_id = str(payload.get("strategy_id") or "").strip()
        candidate_payload = (
            dict(payload["candidate"])
            if isinstance(payload.get("candidate"), dict)
            else dict(payload)
        )
        candidate_payload.pop("strategy_id", None)
        if user is not None and user.role != "admin":
            if strategy_id != "slow_execution":
                _require_admin_user(user)
            platform_cfg = await state.runtime_config(cfg)
            runtime_cfg = _user_auto_buy_sell_runtime_config(
                request,
                user,
                platform_cfg,
            )
            symbols_by_exchange = _auto_buy_sell_symbols_by_exchange(runtime_cfg)
            accounts = slow_execution_accounts(
                auto_buy_sell_exchanges(runtime_cfg),
                symbols_by_exchange,
                spot_markets=runtime_cfg.spot_markets,
            )
            overrides = _slow_execution_overrides_from_payload(
                candidate_payload,
                allowed_exchanges={row["key"] for row in accounts},
                symbols_by_exchange=symbols_by_exchange,
            )
            task_config = replace(
                SlowExecutionConfig(),
                **{**overrides, "enabled": True},
            )
            validate_task_config(task_config)
            validate_task_exchange_config(runtime_cfg, task_config)
            _require_user_owned_execution(user, task_config)
            candidate = slow_execution_config_to_dict(task_config)
            require_owned_exchange(
                user, _find_exchange_by_key(runtime_cfg, task_config.exchange)
            )
            result = await _user_execution_preflight(
                runtime_cfg,
                task_config,
                expected_owner_email=user.email,
            )
        else:
            _require_admin_user(user)
            candidate, assets = await _preflight_candidate_from_payload(
                state,
                cfg,
                strategy_id=strategy_id,
                payload=candidate_payload,
            )
            _require_user_assets(user, assets)
            runtime_cfg = await state.runtime_config(cfg)
            state_payload = await state.strategy_preflight_payload()
            result = build_strategy_preflight(
                runtime_cfg,
                strategy_id=strategy_id,
                candidate=candidate,
                state_payload=state_payload,
            )
        if result["ready"]:
            service: StrategyPreflightService = request.app[
                "strategy_preflight_service"
            ]
            grant = service.issue(
                owner_email=_config_actor_email(request),
                strategy_id=strategy_id,
                candidate=candidate,
            )
            result["token"] = grant.token
            result["expires_at"] = grant.expires_at
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="strategy_preflight",
        target=strategy_id,
        status="ok" if result["ready"] else "blocked",
        detail=(
            "strategy preflight passed"
            if result["ready"]
            else result["blockers"][0]
            if result["blockers"]
            else "strategy preflight blocked"
        ),
        payload={
            "strategy_id": strategy_id,
            "candidate_hash": result["candidate_hash"],
            "ready": result["ready"],
            "blockers": result["blockers"],
        },
    )
    return web.json_response({"ok": True, "preflight": result})


async def api_slow_execution(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        user = _request_user(request)
        require_capability(user, "strategy.manage")
        require_capability(user, "account.trade")
        payload = await request.json()
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
            else await state.slow_execution_config(runtime_cfg.slow_execution)
        )
        target_symbol = str(overrides.get("symbol") or base_config.symbol)
        candidate = replace(base_config, **overrides)
        validate_task_config(candidate)
        validate_task_exchange_config(runtime_cfg, candidate)
        _require_user_owned_execution(user, candidate)
        if user is None or user.role == "admin":
            _require_user_assets(user, [_base_asset_from_symbol(target_symbol)])
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if user is None or user.role == "admin":
        await state.set_slow_execution_overrides(
            overrides,
            cfg=cfg,
            actor_email=_config_actor_email(request),
            action="auto_buy_sell_defaults_update",
        )
        current_config = await state.slow_execution_config(cfg.slow_execution)
        runtime_cfg = await state.runtime_config(cfg)
    else:
        current_config = candidate
        _user_workspace_store(request).upsert_strategy_default(
            user.email,
            "auto_buy_sell",
            slow_execution_config_to_dict(current_config),
        )
    write_web_audit_event(
        runtime_cfg,
        request,
        action="auto_buy_sell_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated Auto Buy/Sell defaults",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": slow_execution_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                auto_buy_sell_exchanges(runtime_cfg),
                _auto_buy_sell_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
        }
    )


async def api_cross_exchange_rebalance(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    guard_baseline: dict[str, Any] | None = None
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        runtime_cfg = await state.runtime_config(cfg)
        current_config = runtime_cfg.cross_exchange_rebalance
        action = str(payload.get("action") or "update").strip().lower()
        if action == "acknowledge_exposure":
            if (
                payload.get("confirm_acknowledgement")
                != "ACKNOWLEDGE RESIDUAL EXPOSURE"
            ):
                raise ValueError(
                    "acknowledgement requires "
                    "confirm_acknowledgement=ACKNOWLEDGE RESIDUAL EXPOSURE"
                )
            runtime = await state.cross_exchange_rebalance_runtime()
            if not runtime:
                runtime = load_rebalance_runtime(
                    current_config.runtime_path,
                    current_config,
                    common_quote_currency=runtime_cfg.common_quote_currency,
                )
            if (
                not runtime.get("halted")
                or runtime.get("halt_reason") != "hedge_required"
            ):
                raise ValueError("only a hedge_required stop can be acknowledged")
            residual = runtime.get("residual_exposure")
            if (
                not isinstance(residual, dict)
                or float(residual.get("quantity_base") or 0.0) <= 0
            ):
                entry = find_latest_strategy_timeline_entry(
                    runtime_cfg.strategy_timeline,
                    strategy="cross_exchange_rebalance",
                    status="hedge_required",
                )
                if entry is not None:
                    try:
                        imbalance = float(entry.metrics.get("imbalance_base") or 0.0)
                    except (TypeError, ValueError):
                        imbalance = 0.0
                    if abs(imbalance) > 1e-12:
                        residual = {
                            "asset": _base_asset_from_symbol(current_config.buy_symbol),
                            "side": "sell" if imbalance > 0 else "buy",
                            "quantity_base": abs(imbalance),
                            "detected_at": entry.logged_at,
                            "source": "strategy_timeline",
                        }
            if (
                not isinstance(residual, dict)
                or float(residual.get("quantity_base") or 0.0) <= 0
            ):
                raise ValueError(
                    "the residual exposure amount is unavailable; do not acknowledge it"
                )
            acknowledged_at = time.time()
            residual = {
                **residual,
                "acknowledged_at": acknowledged_at,
                "acknowledged_by": _config_actor_email(request),
            }
            runtime = {
                **runtime,
                "halted": False,
                "halt_reason": None,
                "status": "acknowledged_exposure",
                "residual_exposure": residual,
                "residual_exposure_acknowledged": True,
                "updated_at": acknowledged_at,
            }
            save_rebalance_runtime(current_config.runtime_path, runtime)
            await state.set_cross_exchange_rebalance_runtime(runtime)
            await state.release_coordination_hold("cross_exchange_rebalance")
            write_web_audit_event(
                runtime_cfg,
                request,
                action="cross_exchange_rebalance_residual_acknowledged",
                target=(
                    f"{current_config.buy_exchange} -> {current_config.sell_exchange}"
                ),
                detail="acknowledged residual exposure; automatic rebalance remains blocked",
                payload={"residual_exposure": residual},
            )
            return web.json_response({"ok": True, "runtime": runtime})
        if action == "stop_and_release":
            if payload.get("confirm_stop") != "STOP REBALANCE AND RELEASE MM":
                raise ValueError(
                    "stop and release requires "
                    "confirm_stop=STOP REBALANCE AND RELEASE MM"
                )
            runtime = await state.cross_exchange_rebalance_runtime()
            if not runtime:
                runtime = load_rebalance_runtime(
                    current_config.runtime_path,
                    current_config,
                    common_quote_currency=runtime_cfg.common_quote_currency,
                )
            stopped_at = time.time()
            residual = runtime.get("residual_exposure")
            if isinstance(residual, dict):
                residual = {
                    **residual,
                    "acknowledged_at": stopped_at,
                    "acknowledged_by": _config_actor_email(request),
                    "disposition": "stopped_and_released",
                }
            runtime = {
                **runtime,
                "halted": False,
                "halt_reason": None,
                "status": "stopped_by_operator",
                "residual_exposure": residual,
                "residual_exposure_acknowledged": isinstance(residual, dict),
                "updated_at": stopped_at,
            }
            overrides = {
                **cross_exchange_rebalance_config_to_dict(current_config),
                "enabled": False,
                "live_enabled": False,
            }
            await state.set_cross_exchange_rebalance_overrides(
                overrides,
                cfg=cfg,
                actor_email=_config_actor_email(request),
                action="cross_exchange_rebalance_stop_and_release",
            )
            save_rebalance_runtime(current_config.runtime_path, runtime)
            await state.set_cross_exchange_rebalance_runtime(runtime)
            await state.release_coordination_hold("cross_exchange_rebalance")
            write_web_audit_event(
                runtime_cfg,
                request,
                action="cross_exchange_rebalance_stopped_and_released",
                target=(
                    f"{current_config.buy_exchange} -> {current_config.sell_exchange}"
                ),
                detail="stopped rebalance and released matching MM coordination",
                payload={"residual_exposure": residual},
            )
            return web.json_response({"ok": True, "runtime": runtime})
        if action == "reset":
            _require_admin_user(_request_user(request))
            if current_config.live_enabled:
                raise ValueError("disable Live Ready before resetting progress")
            if payload.get("confirm_reset") != "RESET REBALANCE":
                raise ValueError("reset requires confirm_reset=RESET REBALANCE")
            runtime = new_rebalance_runtime(
                current_config,
                common_quote_currency=runtime_cfg.common_quote_currency,
            )
            save_rebalance_runtime(current_config.runtime_path, runtime)
            await state.set_cross_exchange_rebalance_runtime(runtime)
            write_web_audit_event(
                runtime_cfg,
                request,
                action="cross_exchange_rebalance_reset",
                target=(
                    f"{current_config.buy_exchange} -> {current_config.sell_exchange}"
                ),
                detail="reset cross-exchange rebalance progress",
                payload={"action": "reset"},
            )
            return web.json_response({"ok": True, "runtime": runtime})
        if action != "update":
            raise ValueError(
                "action must be update, reset, acknowledge_exposure, or stop_and_release"
            )

        symbols_by_exchange = _rebalance_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        updated_config = cross_exchange_rebalance_config_from_payload(
            payload,
            base_config=current_config,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        runtime = await state.cross_exchange_rebalance_runtime()
        if (
            updated_config.enabled
            and updated_config.live_enabled
            and runtime.get("residual_exposure_acknowledged")
        ):
            raise ValueError(
                "residual exposure was acknowledged; disable Live Ready, reset progress, "
                "and complete a new live confirmation before restarting"
            )
        if (
            updated_config.live_enabled
            and payload.get("confirm_live") != "ENABLE LIVE REBALANCE"
        ):
            raise ValueError(
                "saving live config requires confirm_live=ENABLE LIVE REBALANCE"
            )
        if updated_config.enabled and updated_config.live_enabled:
            _consume_strategy_preflight(
                request,
                strategy_id="cross_exchange_rebalance",
                candidate=cross_exchange_rebalance_config_to_dict(updated_config),
                token=str(payload.get("preflight_token") or ""),
            )
            guard_baseline = await state.config_versions(limit=1)
        _require_user_assets(
            _request_user(request),
            [
                _base_asset_from_symbol(updated_config.buy_symbol),
                _base_asset_from_symbol(updated_config.sell_symbol),
            ],
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    overrides = cross_exchange_rebalance_config_to_dict(updated_config)
    update = await state.set_cross_exchange_rebalance_overrides(
        overrides,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="cross_exchange_rebalance_update",
    )
    if guard_baseline is not None:
        current_version = await state.config_versions(limit=1)
        if current_version.get("current_version_id") != guard_baseline.get(
            "current_version_id"
        ):
            _schedule_started_config_guard(
                request,
                strategy_id="cross_exchange_rebalance",
                instance_id="default",
                previous_version_id=guard_baseline.get("current_version_id"),
                expected_current_hash=str(current_version.get("current_hash") or ""),
                timeout_seconds=max(35.0, updated_config.interval_seconds + 15.0),
            )
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="cross_exchange_rebalance_config",
        target=(
            f"{updated_config.buy_exchange} {updated_config.buy_symbol} -> "
            f"{updated_config.sell_exchange} {updated_config.sell_symbol}"
        ),
        detail="updated cross-exchange rebalance config",
        payload={
            key: value
            for key, value in overrides.items()
            if key not in {"client_order_prefix", "runtime_path"}
        },
    )
    return web.json_response(
        {
            "ok": True,
            "config": cross_exchange_rebalance_config_to_dict(
                runtime_cfg.cross_exchange_rebalance
            ),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _rebalance_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_spot_grid(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _grid_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _spot_grid_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.spot_grid_config(runtime_cfg.spot_grid)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(
            _request_user(request), [_base_asset_from_symbol(target_symbol)]
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_spot_grid_overrides(
        overrides,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="spot_grid_update",
    )
    current_config = await state.spot_grid_config(cfg.spot_grid)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="spot_grid_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated Spot Grid config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": spot_grid_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _grid_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_dca(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _grid_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _dca_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.dca_config(runtime_cfg.dca)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(
            _request_user(request), [_base_asset_from_symbol(target_symbol)]
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_dca_overrides(
        overrides,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="dca_update",
    )
    current_config = await state.dca_config(cfg.dca)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="dca_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated DCA Bot config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": dca_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _grid_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_execution_algo(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _execution_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _execution_algo_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.execution_algo_config(runtime_cfg.execution_algo)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(
            _request_user(request), [_base_asset_from_symbol(target_symbol)]
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_execution_algo_overrides(
        overrides,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="execution_algo_update",
    )
    current_config = await state.execution_algo_config(cfg.execution_algo)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="execution_algo_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated TWAP/VWAP/POV config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": execution_algo_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _execution_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )


async def api_backtest(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
        runtime_cfg = await state.runtime_config(cfg)
        symbols_by_exchange = _execution_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _backtest_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        current_config = await state.backtest_config(runtime_cfg.backtest)
        target_symbol = str(overrides.get("symbol") or current_config.symbol)
        _require_user_assets(
            _request_user(request), [_base_asset_from_symbol(target_symbol)]
        )
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    update = await state.set_backtest_overrides(
        overrides,
        cfg=cfg,
        actor_email=_config_actor_email(request),
        action="backtest_update",
    )
    current_config = await state.backtest_config(cfg.backtest)
    runtime_cfg = await state.runtime_config(cfg)
    write_web_audit_event(
        runtime_cfg,
        request,
        action="backtest_config",
        target=f"{current_config.exchange} {current_config.symbol}".strip(),
        detail="updated backtest config",
        payload=overrides,
    )
    return web.json_response(
        {
            "ok": True,
            "config": backtest_config_to_dict(current_config),
            "accounts": slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                _execution_symbols_by_exchange(runtime_cfg),
                spot_markets=runtime_cfg.spot_markets,
            ),
            **update,
        }
    )
