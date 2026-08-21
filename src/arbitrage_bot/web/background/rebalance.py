from __future__ import annotations

import asyncio
import time
from typing import Any

from ..coordination import (
    market_maker_coordination_status,
    rebalance_coordination_hold_required,
    rebalance_coordination_resources,
    wait_for_market_maker_coordination,
)
from ..state import MonitorState

from ...config import BotConfig
from ...cross_exchange_rebalancer import (
    STRATEGY_ID as CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
    apply_rebalance_cycle_to_runtime,
    load_rebalance_runtime,
    new_rebalance_runtime,
    rebalance_config_fingerprint,
    run_cross_exchange_rebalance_cycle,
    save_rebalance_runtime,
)
from ...exchanges import ExchangeManager
from ...models import OrderBookSnapshot
from ...strategy_timeline import (
    find_latest_strategy_timeline_entry,
    write_strategy_timeline_from_payload,
)
from ...trade_log import write_trade_event
from ..core import (
    _find_exchange_by_key,
    _risk_account_enabled,
)
from .common import _complete_market_maker_cycle_on_shutdown

REBALANCE_MARKET_DATA_TIMEOUT_SECONDS = 15.0


class RebalanceMarketDataTimeout(TimeoutError):
    pass


async def _sleep_for_rebalance_config_change(
    cfg: BotConfig,
    state: MonitorState,
    current_config: Any,
    sleep_for: float,
) -> None:
    """Sleep until the next cycle, but apply control-plane changes promptly."""
    deadline = time.monotonic() + max(0.0, sleep_for)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))
        runtime_cfg = await state.runtime_config(cfg)
        if runtime_cfg.cross_exchange_rebalance != current_config:
            return


async def _fetch_rebalance_books(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    timeout_seconds: float = REBALANCE_MARKET_DATA_TIMEOUT_SECONDS,
) -> dict[tuple[str, str], OrderBookSnapshot]:
    rebalance = cfg.cross_exchange_rebalance
    buy_exchange = _find_exchange_by_key(cfg, rebalance.buy_exchange)
    sell_exchange = _find_exchange_by_key(cfg, rebalance.sell_exchange)
    timeout_seconds = max(0.1, float(timeout_seconds))
    try:
        buy_book, sell_book = await asyncio.wait_for(
            asyncio.gather(
                manager.fetch_order_book(
                    buy_exchange,
                    rebalance.buy_symbol,
                    cfg.order_book_depth,
                ),
                manager.fetch_order_book(
                    sell_exchange,
                    rebalance.sell_symbol,
                    cfg.order_book_depth,
                ),
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise RebalanceMarketDataTimeout(
            "order book refresh exceeded "
            f"{timeout_seconds:.1f}s for {rebalance.buy_exchange} "
            f"{rebalance.buy_symbol} and {rebalance.sell_exchange} "
            f"{rebalance.sell_symbol}"
        ) from exc
    books = {}
    if buy_book is not None:
        books[(rebalance.buy_exchange, rebalance.buy_symbol)] = buy_book
    if sell_book is not None:
        books[(rebalance.sell_exchange, rebalance.sell_symbol)] = sell_book
    return books


def _cross_exchange_rebalance_live_gate(
    cfg: BotConfig,
    *,
    program_running: bool,
    strategy_paused: bool,
) -> tuple[bool, str, list[str]]:
    rebalance = cfg.cross_exchange_rebalance
    if not rebalance.enabled:
        return False, "disabled", ["cross-exchange rebalance is disabled"]
    if strategy_paused:
        return False, "paused", ["cross-exchange rebalance is paused"]
    if not program_running:
        return False, "program_paused", ["program is paused"]
    if not rebalance.live_enabled:
        return False, "dry_run", []
    reasons = []
    if not cfg.risk.enabled or not cfg.risk.trading_enabled:
        reasons.append("risk trading is disabled")
    if not cfg.risk.allow_live_trading:
        reasons.append("risk.allow_live_trading is false")
    if not cfg.risk.strategy_enabled.get(
        CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
        False,
    ):
        reasons.append(
            "risk.strategy_enabled.cross_exchange_rebalance is not explicitly true"
        )
    for exchange_key in (rebalance.buy_exchange, rebalance.sell_exchange):
        if exchange_key and not _risk_account_enabled(cfg, exchange_key):
            reasons.append(f"{exchange_key} account is disabled")
    return (not reasons), "blocked_by_risk" if reasons else "live", reasons


async def _refresh_rebalance_runtime_from_state(
    state: MonitorState,
    runtime: dict[str, Any],
    *,
    config_fingerprint: str,
) -> dict[str, Any]:
    """Honor an API reset or acknowledgement before a stale loop rewrites it."""
    reader = getattr(state, "cross_exchange_rebalance_runtime", None)
    if not callable(reader):
        return runtime
    published = await reader()
    if not isinstance(published, dict):
        return runtime
    if published.get("config_fingerprint") != config_fingerprint:
        return runtime
    try:
        published_at = float(published.get("updated_at") or 0.0)
        runtime_at = float(runtime.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return runtime
    return published if published_at > runtime_at else runtime


async def cross_exchange_rebalance_task_loop(
    cfg: BotConfig,
    state: MonitorState,
) -> None:
    manager = ExchangeManager()
    coordination_owner = CROSS_EXCHANGE_REBALANCE_STRATEGY_ID
    coordination_active = False
    current_path, runtime = await _load_initial_rebalance_runtime(cfg, state)
    last_logged_status: str | None = None
    try:
        await state.set_cross_exchange_rebalance_runtime(runtime)
        while True:
            runtime_cfg = await state.runtime_config(cfg)
            rebalance = runtime_cfg.cross_exchange_rebalance
            started = time.monotonic()
            interval = max(1.0, rebalance.interval_seconds)
            fingerprint = rebalance_config_fingerprint(
                rebalance,
                common_quote_currency=runtime_cfg.common_quote_currency,
            )
            if current_path != rebalance.runtime_path:
                current_path = rebalance.runtime_path
                runtime = load_rebalance_runtime(
                    current_path,
                    rebalance,
                    common_quote_currency=runtime_cfg.common_quote_currency,
                )
            elif runtime.get("config_fingerprint") != fingerprint:
                runtime = new_rebalance_runtime(
                    rebalance,
                    common_quote_currency=runtime_cfg.common_quote_currency,
                )
            runtime = await _refresh_rebalance_runtime_from_state(
                state,
                runtime,
                config_fingerprint=fingerprint,
            )

            # Releases before residual tracking did not persist the imbalance. Recover
            # that audit-only detail once so the operator can acknowledge it explicitly.
            if (
                runtime.get("halted")
                and runtime.get("halt_reason") == "hedge_required"
                and not isinstance(runtime.get("residual_exposure"), dict)
            ):
                recovered_residual: dict[str, Any] | None = None
                entry = find_latest_strategy_timeline_entry(
                    runtime_cfg.strategy_timeline,
                    strategy=CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                    status="hedge_required",
                )
                if entry is not None:
                    try:
                        imbalance = float(entry.metrics.get("imbalance_base") or 0.0)
                    except (TypeError, ValueError):
                        imbalance = 0.0
                    if abs(imbalance) > 1e-12:
                        recovered_residual = {
                            "asset": str(rebalance.buy_symbol).split("/", 1)[0].upper(),
                            "side": "sell" if imbalance > 0 else "buy",
                            "quantity_base": abs(imbalance),
                            "detected_at": entry.logged_at,
                            "source": "strategy_timeline",
                        }
                if recovered_residual:
                    runtime = {
                        **runtime,
                        "residual_exposure": recovered_residual,
                        "updated_at": time.time(),
                    }
                    save_rebalance_runtime(current_path, runtime)

            strategy_pauses = await state.strategy_pauses()
            program_running = await state.is_running()
            live_allowed, gate_status, gate_reasons = (
                _cross_exchange_rebalance_live_gate(
                    runtime_cfg,
                    program_running=program_running,
                    strategy_paused=strategy_pauses.get(
                        CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                        False,
                    ),
                )
            )
            shutdown_requested = False
            payload: dict[str, Any]
            if not rebalance.enabled:
                if coordination_active:
                    await state.release_coordination_hold(coordination_owner)
                    coordination_active = False
                payload = {
                    "type": "cross_exchange_rebalance_execution",
                    "strategy": CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                    "mode": "dry_run",
                    "status": "disabled",
                }
            elif runtime.get("residual_exposure_acknowledged"):
                if coordination_active:
                    await state.release_coordination_hold(coordination_owner)
                    coordination_active = False
                residual = (
                    runtime.get("residual_exposure")
                    if isinstance(runtime.get("residual_exposure"), dict)
                    else {}
                )
                payload = {
                    "type": "cross_exchange_rebalance_execution",
                    "strategy": CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                    "mode": "live" if rebalance.live_enabled else "dry_run",
                    "status": "acknowledged_exposure",
                    "risk": {
                        "approved": False,
                        "level": "blocked",
                        "reasons": [
                            "residual exposure was acknowledged; reset progress and "
                            "complete a new live confirmation before restarting"
                        ],
                    },
                    "residual_exposure": residual,
                }
            elif runtime.get("halted") and gate_status not in {
                "paused",
                "program_paused",
            }:
                coordination = None
                if rebalance.live_enabled and rebalance.coordinate_market_maker:
                    coordination = await state.acquire_coordination_hold(
                        coordination_owner,
                        rebalance_coordination_resources(runtime_cfg),
                        reason="rebalance halted; MM held for exposure review",
                        ttl_seconds=max(
                            60.0,
                            interval + rebalance.coordination_timeout_seconds + 10.0,
                        ),
                    )
                    coordination_active = True
                elif coordination_active:
                    await state.release_coordination_hold(coordination_owner)
                    coordination_active = False
                payload = {
                    "type": "cross_exchange_rebalance_execution",
                    "strategy": CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                    "mode": "live" if rebalance.live_enabled else "dry_run",
                    "status": "halted",
                    "risk": {
                        "approved": False,
                        "level": "blocked",
                        "reasons": [
                            str(
                                runtime.get("halt_reason")
                                or "manual review is required"
                            )
                        ],
                    },
                }
                if coordination is not None:
                    payload["coordination"] = {
                        "status": "held_for_safety",
                        "lease": coordination,
                    }
            elif gate_status in {"paused", "program_paused"}:
                if coordination_active:
                    await state.release_coordination_hold(coordination_owner)
                    coordination_active = False
                payload = {
                    "type": "cross_exchange_rebalance_execution",
                    "strategy": CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                    "mode": "paused",
                    "status": gate_status,
                    "risk": {
                        "approved": False,
                        "level": "blocked",
                        "reasons": gate_reasons,
                    },
                }
            else:
                try:
                    books = await _fetch_rebalance_books(runtime_cfg, manager)
                    quote_rates = await state.quote_rates()
                    preview = await run_cross_exchange_rebalance_cycle(
                        runtime_cfg,
                        manager,
                        books=books,
                        quote_rates=quote_rates,
                        completed_quote_common=float(
                            runtime.get("completed_quote_common") or 0.0
                        ),
                        live=False,
                    )
                    if (
                        live_allowed
                        and rebalance.coordinate_market_maker
                        and preview.get("status") == "planned"
                    ):
                        lease = await state.acquire_coordination_hold(
                            coordination_owner,
                            rebalance_coordination_resources(runtime_cfg),
                            reason="cross-exchange rebalance preparing a live cycle",
                            ttl_seconds=max(
                                60.0,
                                interval
                                + rebalance.coordination_timeout_seconds
                                + 10.0,
                            ),
                        )
                        coordination_active = True
                        coordination = await wait_for_market_maker_coordination(
                            state,
                            runtime_cfg,
                            owner=coordination_owner,
                            timeout_seconds=rebalance.coordination_timeout_seconds,
                        )
                        coordination["lease"] = lease
                        if not coordination["ready"]:
                            payload = {
                                **preview,
                                "mode": "live",
                                "status": "waiting_for_coordination",
                                "coordination": coordination,
                                "risk": {
                                    "approved": False,
                                    "level": "blocked",
                                    "reasons": coordination["reasons"],
                                },
                            }
                        else:
                            fresh_books = await _fetch_rebalance_books(
                                runtime_cfg,
                                manager,
                            )
                            fresh_quote_rates = await state.quote_rates()
                            cycle = run_cross_exchange_rebalance_cycle(
                                runtime_cfg,
                                manager,
                                books=fresh_books,
                                quote_rates=fresh_quote_rates,
                                completed_quote_common=float(
                                    runtime.get("completed_quote_common") or 0.0
                                ),
                                live=True,
                            )
                            (
                                payload,
                                shutdown_requested,
                            ) = await _complete_market_maker_cycle_on_shutdown(cycle)
                            coordination["status"] = "ready"
                            payload["coordination"] = coordination
                            if rebalance_coordination_hold_required(payload):
                                payload["coordination"]["status"] = "held_for_safety"
                            else:
                                await state.release_coordination_hold(
                                    coordination_owner
                                )
                                coordination_active = False
                                payload["coordination"]["released"] = True
                    elif live_allowed:
                        if rebalance.coordinate_market_maker:
                            payload = preview
                            if coordination_active:
                                coordination = market_maker_coordination_status(
                                    runtime_cfg,
                                    await state.market_maker_runtime(),
                                    owner=coordination_owner,
                                )
                                if coordination["ready"]:
                                    await state.release_coordination_hold(
                                        coordination_owner
                                    )
                                    coordination_active = False
                                    coordination["status"] = "released"
                                    coordination["released"] = True
                                else:
                                    lease = await state.acquire_coordination_hold(
                                        coordination_owner,
                                        rebalance_coordination_resources(runtime_cfg),
                                        reason=(
                                            "waiting for the previous MM cancellation "
                                            "handoff to finish"
                                        ),
                                        ttl_seconds=max(
                                            60.0,
                                            interval
                                            + rebalance.coordination_timeout_seconds
                                            + 10.0,
                                        ),
                                    )
                                    coordination["status"] = "held_until_clear"
                                    coordination["lease"] = lease
                                payload["coordination"] = coordination
                        else:
                            if coordination_active:
                                await state.release_coordination_hold(
                                    coordination_owner
                                )
                                coordination_active = False
                            cycle = run_cross_exchange_rebalance_cycle(
                                runtime_cfg,
                                manager,
                                books=books,
                                quote_rates=quote_rates,
                                completed_quote_common=float(
                                    runtime.get("completed_quote_common") or 0.0
                                ),
                                live=True,
                            )
                            (
                                payload,
                                shutdown_requested,
                            ) = await _complete_market_maker_cycle_on_shutdown(cycle)
                    else:
                        if coordination_active:
                            await state.release_coordination_hold(coordination_owner)
                            coordination_active = False
                        payload = preview
                    if rebalance.live_enabled and gate_reasons:
                        payload["mode"] = "live"
                        payload["status"] = "blocked_by_risk"
                        payload["risk"] = {
                            "approved": False,
                            "level": "blocked",
                            "reasons": gate_reasons,
                        }
                except RebalanceMarketDataTimeout as exc:
                    if coordination_active:
                        await state.release_coordination_hold(coordination_owner)
                        coordination_active = False
                    payload = {
                        "type": "cross_exchange_rebalance_execution",
                        "strategy": CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                        "mode": "live" if live_allowed else "dry_run",
                        "status": "waiting_for_market_data",
                        "market_data": {
                            "status": "timeout",
                            "timeout_seconds": REBALANCE_MARKET_DATA_TIMEOUT_SECONDS,
                        },
                        "risk": {
                            "approved": False,
                            "level": "waiting",
                            "reasons": [str(exc)],
                        },
                        "warnings": [str(exc)],
                    }
                except Exception as exc:  # noqa: BLE001
                    payload = {
                        "type": "cross_exchange_rebalance_execution",
                        "strategy": CROSS_EXCHANGE_REBALANCE_STRATEGY_ID,
                        "mode": "live" if live_allowed else "dry_run",
                        "status": "error",
                        "errors": [f"{exc.__class__.__name__}: {exc}"],
                    }
                    if coordination_active and live_allowed:
                        payload["halt_required"] = True
                        payload["coordination"] = {
                            "status": "held_for_safety",
                            "reasons": [
                                "coordination or live execution failed unexpectedly"
                            ],
                        }

            if payload.get("status") in {"disabled", "paused", "program_paused"}:
                runtime = {
                    **runtime,
                    "status": payload["status"],
                    "last_payload": payload,
                    "last_error": None,
                    "updated_at": time.time(),
                }
            elif payload.get("status") in {"halted", "acknowledged_exposure"}:
                runtime = {
                    **runtime,
                    "status": str(payload["status"]),
                    "last_payload": payload,
                    "updated_at": time.time(),
                }
            else:
                runtime = apply_rebalance_cycle_to_runtime(
                    runtime,
                    payload,
                    rebalance,
                )
                runtime["last_error"] = (
                    (payload.get("errors") or [None])[0]
                    if payload.get("status") == "error"
                    else None
                )
            runtime["config_fingerprint"] = fingerprint
            runtime["mode"] = payload.get("mode", "dry_run")
            save_rebalance_runtime(current_path, runtime)
            await state.set_cross_exchange_rebalance_runtime(runtime)

            status = str(payload.get("status") or "unknown")
            if live_allowed or status != last_logged_status:
                write_trade_event(runtime_cfg.trade_log, payload)
                write_strategy_timeline_from_payload(
                    runtime_cfg.strategy_timeline,
                    payload,
                    source="cross_exchange_rebalance_task",
                )
                last_logged_status = status
            if shutdown_requested:
                raise asyncio.CancelledError
            sleep_for = max(0.0, interval - (time.monotonic() - started))
            if sleep_for > 0:
                await _sleep_for_rebalance_config_change(
                    cfg,
                    state,
                    rebalance,
                    sleep_for,
                )
    finally:
        await state.release_coordination_hold(coordination_owner)
        await manager.close()


async def _load_initial_rebalance_runtime(
    cfg: BotConfig,
    state: MonitorState,
) -> tuple[str, dict[str, Any]]:
    runtime_cfg = await state.runtime_config(cfg)
    rebalance = runtime_cfg.cross_exchange_rebalance
    current_path = rebalance.runtime_path
    runtime = load_rebalance_runtime(
        current_path,
        rebalance,
        common_quote_currency=runtime_cfg.common_quote_currency,
    )
    return current_path, runtime
