from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Any

from aiohttp import web

from ..state import MonitorState
from ..strategy_preflight import (
    StrategyPreflightService,
    candidate_hash,
)
from ..users import (
    WebUser,
)
from ..user_scope import (
    _base_asset_from_symbol,
    _require_admin_user,
)

from ..security import (
    _request_user,
    write_system_web_audit_event,
    write_web_audit_event,
)

from ...auto_buy_sell_task import (
    AutoBuySellTaskService,
    validate_task_config,
    validate_task_exchange_config,
)
from ...config import (
    BotConfig,
    SlowExecutionConfig,
)
from ...derivatives import normalize_derivative_position
from ...exchanges import ExchangeManager
from ...slow_execution import build_slow_execution_plan
from ...web_config import (
    _auto_buy_sell_symbols_by_exchange,
    _rebalance_symbols_by_exchange,
    _slow_execution_overrides_from_payload,
    auto_buy_sell_exchanges,
    cross_exchange_rebalance_config_from_payload,
    cross_exchange_rebalance_config_to_dict,
    market_maker_config_to_dict,
    market_maker_config_from_payload,
    market_maker_configs_for_runtime,
    market_maker_symbols_for_accounts,
    slow_execution_accounts,
    slow_execution_config_to_dict,
)


from ..core import (
    _all_account_exchanges,
    _config_actor_email,
    _find_exchange_by_key,
)


async def api_control(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        _require_admin_user(_request_user(request))
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)

    running = payload.get("running")
    if not isinstance(running, bool):
        return web.json_response({"error": "running must be a boolean"}, status=400)

    result = await state.set_running(running)
    write_web_audit_event(
        cfg,
        request,
        action="program_control",
        target="program",
        detail="resume scans" if running else "pause scans",
        payload={"running": running},
    )
    return web.json_response(result)


async def _preflight_candidate_from_payload(
    state: MonitorState,
    cfg: BotConfig,
    *,
    strategy_id: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    runtime_cfg = await state.runtime_config(cfg)
    if strategy_id == "market_maker":
        symbols_by_exchange = market_maker_symbols_for_accounts(
            runtime_cfg,
            base_cfg=cfg,
        )
        current_instances = market_maker_configs_for_runtime(runtime_cfg)
        target_id = str(payload.get("id") or "").strip()
        base_config = next(
            (instance for instance in current_instances if instance.id == target_id),
            current_instances[0] if current_instances else None,
        )
        candidate = market_maker_config_from_payload(
            payload,
            base_config=base_config,
            allowed_exchanges={
                exchange.key for exchange in _all_account_exchanges(runtime_cfg)
            },
            symbols_by_exchange=symbols_by_exchange,
            repair_stale_identity_id=True,
            normalize_identity_id=bool(
                payload.get("cleanup_recoverable_state") is True
            ),
        )
        row = market_maker_config_to_dict(candidate)
        return row, [_base_asset_from_symbol(candidate.symbol)]
    if strategy_id == "slow_execution":
        symbols_by_exchange = _auto_buy_sell_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            auto_buy_sell_exchanges(runtime_cfg),
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        base = await state.slow_execution_config(runtime_cfg.slow_execution)
        candidate = replace(base, **{**overrides, "enabled": True})
        validate_task_config(candidate)
        validate_task_exchange_config(runtime_cfg, candidate)
        return slow_execution_config_to_dict(candidate), [
            _base_asset_from_symbol(candidate.symbol)
        ]
    if strategy_id == "cross_exchange_rebalance":
        symbols_by_exchange = _rebalance_symbols_by_exchange(runtime_cfg)
        accounts = slow_execution_accounts(
            runtime_cfg.spot_exchanges,
            symbols_by_exchange,
            spot_markets=runtime_cfg.spot_markets,
        )
        candidate = cross_exchange_rebalance_config_from_payload(
            payload,
            base_config=runtime_cfg.cross_exchange_rebalance,
            allowed_exchanges={account["key"] for account in accounts},
            symbols_by_exchange=symbols_by_exchange,
        )
        return cross_exchange_rebalance_config_to_dict(candidate), [
            _base_asset_from_symbol(candidate.buy_symbol),
            _base_asset_from_symbol(candidate.sell_symbol),
        ]
    if strategy_id == "spot_spread":
        return {
            "notional_quote": float(
                payload.get("notional_quote") or runtime_cfg.notional_quote
            )
        }, [market.asset for market in runtime_cfg.spot_markets]
    raise ValueError(f"preflight is not supported for strategy: {strategy_id}")


def _consume_strategy_preflight(
    request: web.Request,
    *,
    strategy_id: str,
    candidate: dict[str, Any],
    token: str,
) -> None:
    service = request.app.get("strategy_preflight_service")
    if not isinstance(service, StrategyPreflightService):
        raise ValueError("strategy preflight service is unavailable")
    service.consume(
        token,
        owner_email=_config_actor_email(request),
        strategy_id=strategy_id,
        candidate=candidate,
    )


async def _watch_started_config(
    app: web.Application,
    *,
    strategy_id: str,
    instance_id: str,
    previous_version_id: int | None,
    expected_current_hash: str,
    timeout_seconds: float = 35.0,
) -> None:
    if previous_version_id is None or not expected_current_hash:
        return
    state: MonitorState = app["monitor_state"]
    cfg: BotConfig = app["config"]
    guard_started_at = time.time()
    deadline = time.monotonic() + max(5.0, timeout_seconds)
    last_row: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        versions = await state.config_versions(limit=1)
        if versions.get("current_hash") != expected_current_hash:
            return
        lifecycle = await state.strategy_lifecycle()
        rows = [
            row
            for row in lifecycle.get("instances", [])
            if isinstance(row, dict)
            and row.get("strategy_id") == strategy_id
            and (not instance_id or str(row.get("instance_id") or "") == instance_id)
        ]
        last_row = rows[0] if rows else None
        runtime_updated_at = float((last_row or {}).get("updated_at") or 0.0)
        if (
            last_row
            and runtime_updated_at >= guard_started_at
            and last_row.get("converged")
            and last_row.get("actual_state") in {"running", "waiting"}
        ):
            await state.mark_current_config_known_good(
                expected_current_hash=expected_current_hash,
            )
            return
        if last_row and last_row.get("convergence_state") in {"blocked", "error"}:
            break

    versions = await state.config_versions(limit=1)
    if versions.get("current_hash") != expected_current_hash:
        return
    try:
        result = await state.rollback_config_version(
            previous_version_id,
            expected_current_hash=expected_current_hash,
            actor_email="automatic-start-guard",
        )
    except (OSError, TypeError, ValueError) as exc:
        write_system_web_audit_event(
            cfg,
            action="config_auto_rollback",
            status="error",
            target=strategy_id,
            detail="automatic rollback failed",
            error=str(exc),
        )
        return
    if strategy_id == "slow_execution" and instance_id:
        try:
            task_service: AutoBuySellTaskService = app["auto_buy_sell_tasks"]
            await task_service.set_paused(instance_id, True)
            await state.set_auto_buy_sell_tasks(await task_service.snapshot())
        except (KeyError, ValueError):
            pass
    reason = str((last_row or {}).get("reason") or "strategy did not become healthy")
    write_system_web_audit_event(
        cfg,
        action="config_auto_rollback",
        status="ok",
        target=strategy_id,
        detail=f"restored version {previous_version_id}: {reason}",
        payload={
            "strategy_id": strategy_id,
            "instance_id": instance_id,
            "restored_version_id": previous_version_id,
            "result_version_id": result.get("current_version_id"),
        },
    )


async def _watch_startup_configuration(
    app: web.Application,
    *,
    timeout_seconds: float = 60.0,
) -> None:
    state: MonitorState = app["monitor_state"]
    cfg: BotConfig = app["config"]
    candidate = await state.startup_config_guard_candidate()
    if candidate is None:
        return

    expected_hash = str(candidate["hash"])
    previous_version_id = int(candidate["previous_known_good_id"])
    guard_started_at = time.time()
    deadline = time.monotonic() + max(10.0, timeout_seconds)
    healthy_cycles = 0
    rollback_reason = "startup health checks timed out"

    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        versions = await state.config_versions(limit=1)
        if versions.get("current_hash") != expected_hash:
            return
        payload = await state.get(view="status")
        if not bool(payload.get("program", {}).get("running")):
            healthy_cycles += 1
            if healthy_cycles >= 2:
                await state.mark_current_config_known_good(
                    expected_current_hash=expected_hash,
                )
                return
            continue
        scan_finished = float(payload.get("scan", {}).get("last_finished") or 0.0)
        if scan_finished < guard_started_at:
            continue

        lifecycle = payload.get("strategy_lifecycle", {})
        desired_rows = [
            row
            for row in lifecycle.get("instances", [])
            if isinstance(row, dict) and row.get("desired_state") == "running"
        ]
        stale_rows = [
            row
            for row in desired_rows
            if float(row.get("updated_at") or 0.0) > 0.0
            and float(row.get("updated_at") or 0.0) < guard_started_at
        ]
        if stale_rows:
            continue
        failed_rows = [
            row
            for row in desired_rows
            if row.get("convergence_state") in {"blocked", "error"}
        ]
        if failed_rows:
            failed = failed_rows[0]
            rollback_reason = str(
                failed.get("reason")
                or f"{failed.get('strategy_id')} became {failed.get('actual_state')}"
            )
            break

        all_healthy = all(
            bool(row.get("converged"))
            and row.get("actual_state") in {"running", "waiting", "complete"}
            for row in desired_rows
        )
        if payload.get("status") in {"running", "degraded"} and all_healthy:
            healthy_cycles += 1
            if healthy_cycles >= 2:
                marked = await state.mark_current_config_known_good(
                    expected_current_hash=expected_hash,
                )
                if marked is not None:
                    write_system_web_audit_event(
                        cfg,
                        action="startup_config_verified",
                        status="ok",
                        target="runtime_config",
                        detail=f"verified configuration version {candidate['version_id']}",
                    )
                return
        else:
            healthy_cycles = 0

    versions = await state.config_versions(limit=1)
    if versions.get("current_hash") != expected_hash:
        return
    try:
        result = await state.rollback_config_version(
            previous_version_id,
            expected_current_hash=expected_hash,
            actor_email="automatic-startup-guard",
        )
    except (OSError, TypeError, ValueError) as exc:
        write_system_web_audit_event(
            cfg,
            action="startup_config_auto_rollback",
            status="error",
            target="runtime_config",
            detail="startup configuration rollback failed",
            error=str(exc),
        )
        return
    write_system_web_audit_event(
        cfg,
        action="startup_config_auto_rollback",
        status="ok",
        target="runtime_config",
        detail=f"restored version {previous_version_id}: {rollback_reason}",
        payload={
            "failed_version_id": candidate["version_id"],
            "restored_version_id": previous_version_id,
            "result_version_id": result.get("current_version_id"),
        },
    )


def _schedule_started_config_guard(
    request: web.Request,
    *,
    strategy_id: str,
    instance_id: str,
    previous_version_id: int | None,
    expected_current_hash: str,
    timeout_seconds: float = 35.0,
) -> None:
    tasks = request.app.get("config_guard_tasks")
    if not isinstance(tasks, set):
        return
    task = asyncio.create_task(
        _watch_started_config(
            request.app,
            strategy_id=strategy_id,
            instance_id=instance_id,
            previous_version_id=previous_version_id,
            expected_current_hash=expected_current_hash,
            timeout_seconds=timeout_seconds,
        )
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _require_user_owned_execution(
    user: WebUser | None,
    task_config: SlowExecutionConfig,
) -> None:
    if user is None or user.role == "admin":
        return
    if task_config.instrument_type not in {"spot", "perpetual"}:
        raise PermissionError("unsupported owner trading instrument")


def _preflight_check(
    check_id: str,
    label: str,
    passed: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": "passed" if passed else "blocked",
        "detail": detail,
        "scope": "",
        "blocking": not passed,
    }


def _private_balance_free(balance: dict[str, Any], currency: str) -> float:
    row = balance.get(currency)
    if isinstance(row, dict):
        try:
            return float(row.get("free") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    free = balance.get("free")
    if isinstance(free, dict):
        try:
            return float(free.get(currency) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


async def _user_execution_preflight(
    runtime_cfg: BotConfig,
    task_config: SlowExecutionConfig,
    *,
    expected_owner_email: str = "",
) -> dict[str, Any]:
    candidate = slow_execution_config_to_dict(task_config)
    exchange = _find_exchange_by_key(runtime_cfg, task_config.exchange)
    manager = ExchangeManager()
    checks: list[dict[str, Any]] = []
    try:
        checks.append(
            _preflight_check(
                "live_gate",
                "Owner live gate",
                runtime_cfg.risk.enabled
                and runtime_cfg.risk.trading_enabled
                and runtime_cfg.risk.allow_live_trading,
                "Owner trading profile is enabled"
                if runtime_cfg.risk.trading_enabled
                else "Owner trading profile is disabled",
            )
        )
        checks.append(
            _preflight_check(
                "owner_account",
                "Owner account scope",
                bool(exchange.credential_owner_email)
                and (
                    not expected_owner_email
                    or exchange.credential_owner_email == expected_owner_email
                ),
                "Order uses the signed-in user's encrypted API connection",
            )
        )
        market = await manager.fetch_market_info(exchange, symbol=task_config.symbol)
        market = market if isinstance(market, dict) else {}
        expected_market_type = (
            market.get("swap") is not False
            if task_config.instrument_type == "perpetual"
            else market.get("spot") is not False
        )
        market_ready = bool(
            market and market.get("active") is not False and expected_market_type
        )
        checks.append(
            _preflight_check(
                "market",
                "Live market",
                market_ready,
                f"Active {task_config.instrument_type} market"
                if market_ready
                else "Selected market is unavailable, inactive, or the wrong type",
            )
        )
        book = await manager.fetch_order_book(
            exchange,
            task_config.symbol,
            runtime_cfg.order_book_depth,
        )
        book_ready = bool(book and book.bids and book.asks)
        reference_price = 0.0
        if book_ready and book is not None:
            reference_price = float(
                book.asks[0].price if task_config.side == "buy" else book.bids[0].price
            )
        checks.append(
            _preflight_check(
                "order_book",
                "Live order book",
                book_ready and reference_price > 0,
                f"Executable reference price {reference_price:.12g}"
                if reference_price > 0
                else "Order book is unavailable",
            )
        )
        forced_plan = build_slow_execution_plan(
            book,
            replace(task_config, start_price=0.0, stop_price=0.0),
            random_fn=lambda: 1.0,
        )
        if forced_plan.order is None:
            raise ValueError(
                f"could not build an executable order: {forced_plan.status}"
            )
        order_base = float(forced_plan.order.amount)
        order_quote = float(forced_plan.order.quote_notional)
        if task_config.instrument_type == "perpetual":
            prepared = await manager.prepare_linear_contract_order(
                exchange,
                symbol=task_config.symbol,
                side=task_config.side,
                base_amount=order_base,
                price=forced_plan.order.price,
            )
        else:
            prepared = await manager.prepare_limit_order(
                exchange,
                symbol=task_config.symbol,
                side=task_config.side,
                amount=order_base,
                price=forced_plan.order.price,
            )
        checks.append(
            _preflight_check(
                "order_validation",
                "Exchange order limits",
                bool(prepared),
                "Amount, precision, and exchange minimums are valid",
            )
        )
        quote_currency = task_config.symbol.split("/", 1)[-1].split(":", 1)[0]
        quote_rate = (
            1.0
            if quote_currency.upper() == runtime_cfg.common_quote_currency.upper()
            else float(runtime_cfg.quote_rates.get(quote_currency) or 0.0)
        )
        order_common = order_quote * quote_rate
        order_within_risk = all(
            limit <= 0 or order_common <= limit + 1e-9
            for limit in (
                runtime_cfg.risk.max_order_quote,
                runtime_cfg.risk.max_cycle_quote,
            )
        )
        checks.append(
            _preflight_check(
                "order_size",
                "Per-order size",
                order_within_risk,
                (
                    f"Per-order maximum {order_base:.12g} base / "
                    f"{order_quote:.12g} {quote_currency}; risk limit "
                    f"{runtime_cfg.risk.max_order_quote:.12g}"
                ),
            )
        )

        total_base = float(task_config.total_base)
        total_quote = float(task_config.total_quote)
        target_base = total_base if total_base > 0 else total_quote / reference_price
        target_quote = total_quote if total_quote > 0 else total_base * reference_price
        if task_config.unlimited_total:
            target_base = order_base
            target_quote = order_quote
        increases_exposure = (
            task_config.instrument_type == "spot" and task_config.side == "buy"
        ) or (
            task_config.instrument_type == "perpetual"
            and task_config.position_effect == "open"
        )
        target_common = target_quote * quote_rate
        exposure_ready = (
            not increases_exposure
            or runtime_cfg.risk.max_exposure_quote <= 0
            or target_common <= runtime_cfg.risk.max_exposure_quote + 1e-9
        )
        checks.append(
            _preflight_check(
                "total_exposure",
                "Task exposure limit",
                exposure_ready,
                (
                    f"Task target {target_common:.12g} "
                    f"{runtime_cfg.common_quote_currency}; maximum "
                    f"{runtime_cfg.risk.max_exposure_quote:.12g}"
                ),
            )
        )

        if task_config.instrument_type == "spot":
            balance = await manager.fetch_balance(exchange)
            base_currency = task_config.symbol.split("/", 1)[0].upper()
            balance_currency = (
                quote_currency.upper() if task_config.side == "buy" else base_currency
            )
            required_balance = (
                target_quote if task_config.side == "buy" else target_base
            )
            free_balance = _private_balance_free(balance, balance_currency)
            balance_ready = (
                required_balance <= free_balance + max(free_balance, 1.0) * 1e-9
            )
            checks.append(
                _preflight_check(
                    "balance",
                    "Available spot balance",
                    balance_ready,
                    (
                        f"Requires {required_balance:.12g} {balance_currency}; "
                        f"free {free_balance:.12g} {balance_currency}"
                    ),
                )
            )
        else:
            raw_positions = await manager.fetch_positions(
                exchange, [task_config.symbol]
            )
            positions = [
                row
                for raw in raw_positions
                if isinstance(raw, dict)
                for row in [
                    normalize_derivative_position(exchange, raw, risk=runtime_cfg.risk)
                ]
                if row is not None
                and str(row.get("symbol") or "").upper() == task_config.symbol.upper()
            ]
            matching = [
                row for row in positions if row.get("side") == task_config.position_side
            ]
            opposite = [
                row
                for row in positions
                if row.get("side") in {"long", "short"}
                and row.get("side") != task_config.position_side
            ]
            current_base = sum(float(row.get("base_amount") or 0.0) for row in matching)
            current_quote = sum(
                float(row.get("notional_quote") or 0.0) for row in matching
            )
            position_modes = {
                str(row.get("margin_mode") or "").lower()
                for row in matching
                if str(row.get("margin_mode") or "").strip()
            }
            margin_matches = (
                not position_modes or task_config.margin_mode in position_modes
            )
            checks.append(
                _preflight_check(
                    "margin_mode",
                    "Margin mode",
                    margin_matches,
                    (
                        f"Configured {task_config.margin_mode} matches the position"
                        if margin_matches
                        else "Position uses " + ", ".join(sorted(position_modes))
                    ),
                )
            )
            if task_config.position_effect == "reduce_only":
                total_within_position = (
                    current_base > 0
                    and target_base <= current_base + max(current_base, 1.0) * 1e-9
                )
                checks.append(
                    _preflight_check(
                        "position",
                        "Reduce-only position target",
                        total_within_position,
                        (
                            f"Close target {target_base:.12g} base; current "
                            f"{task_config.position_side} {current_base:.12g} base"
                        ),
                    )
                )
            else:
                leverage_limit = runtime_cfg.risk.max_derivative_leverage
                leverage_ready = (
                    leverage_limit > 0 and task_config.leverage <= leverage_limit + 1e-9
                )
                projected_quote = current_quote + target_quote
                position_limit_ready = (
                    not opposite
                    and task_config.max_position_quote > 0
                    and projected_quote
                    <= task_config.max_position_quote
                    + max(task_config.max_position_quote, 1.0) * 1e-9
                )
                checks.append(
                    _preflight_check(
                        "leverage",
                        "Leverage limit",
                        leverage_ready,
                        (
                            "Perpetual opening is disabled: set Risk Controls > "
                            f"Max Leverage to at least {task_config.leverage:.12g}x"
                            if leverage_limit <= 0
                            else f"Requested {task_config.leverage:.12g}x; maximum "
                            f"{leverage_limit:.12g}x"
                        ),
                    )
                )
                checks.append(
                    _preflight_check(
                        "position",
                        "Perpetual position target",
                        position_limit_ready,
                        (
                            f"Projected {projected_quote:.12g} {quote_currency}; "
                            f"task maximum {task_config.max_position_quote:.12g}"
                            if not opposite
                            else "An opposite one-way position must be closed first"
                        ),
                    )
                )
        open_orders = await manager.fetch_open_orders(
            exchange,
            symbol=task_config.symbol,
        )
        checks.append(
            _preflight_check(
                "open_orders",
                "Existing orders on the market",
                not open_orders,
                "No existing open orders"
                if not open_orders
                else f"Cancel {len(open_orders)} existing order(s) before starting",
            )
        )
        checks.append(
            _preflight_check(
                "quote_rate",
                "Quote conversion",
                quote_rate > 0,
                f"{quote_currency} conversion rate {quote_rate:.12g}"
                if quote_rate > 0
                else f"No conversion rate for {quote_currency}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _preflight_check(
                "private_api",
                "Private account verification",
                False,
                f"{exc.__class__.__name__}: {exc}",
            )
        )
    finally:
        await manager.close()
    blockers = [row["detail"] for row in checks if row["status"] == "blocked"]
    now = time.time()
    return {
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "strategy_id": "slow_execution",
        "candidate_hash": candidate_hash("slow_execution", candidate),
        "checks": checks,
        "blockers": blockers,
        "warnings": [],
        "summary": {"planned_order_count": 1},
        "checked_at": now,
        "expires_at": now + 45.0,
    }
