from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config_versions import (
    ConfigVersionStore,
    configuration_hash,
)
from .render_payloads import state_payload_for_view
from .strategy_lifecycle import build_strategy_lifecycle_payload

from ..config import (
    BacktestConfig,
    BotConfig,
    CashAndCarryPair,
    CrossExchangeRebalanceConfig,
    DcaConfig,
    ExecutionAlgoConfig,
    MarketMakerConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
    SpotMarketConfig,
)
from ..contract_strategies import build_contract_strategies_payload
from ..execution_protection import summarize_multileg_execution_protections
from ..models import Opportunity
from ..portfolio_metrics import build_market_maker_quality_payload
from ..web_config import (
    _cash_and_carry_pairs_from_payload,
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    _rebalance_symbols_by_exchange,
    _spot_markets_from_payload,
    _spot_symbols_by_exchange,
    backtest_config_to_dict,
    cash_and_carry_pairs_to_list,
    contract_strategies_config_to_dict,
    cross_exchange_rebalance_config_to_dict,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    exchange_configs_to_list,
    market_maker_config_to_dict,
    market_maker_configs_for_runtime,
    market_maker_configs_from_payload,
    market_maker_configs_to_list,
    market_maker_configs_with_ids,
    market_maker_config_with_id,
    market_maker_symbols_for_accounts,
    risk_config_to_dict,
    slow_execution_config_to_dict,
    slow_execution_accounts,
    spot_markets_to_list,
    spot_grid_config_to_dict,
    strategy_universe_to_dict,
)
from . import (
    STRATEGY_IDS,
    _all_account_exchanges,
    _build_initial_payload,
    _load_runtime_overrides,
    _save_runtime_overrides,
    build_operations_payload,
    build_readiness_payload,
    build_trading_console_payload,
)

_STATE_VIEW_CACHE_TTL_SECONDS = 0.75


def _state_view_cache_key(
    view: str | None,
    sections: str | None,
) -> tuple[str, str]:
    section_text = str(sections or "")
    normalized_sections = ",".join(
        sorted({item.strip() for item in section_text.split(",") if item.strip()})
    )
    return (str(view or ""), normalized_sections)


def _execution_protection_from_payloads(payload: dict[str, Any]) -> dict[str, Any]:
    return summarize_multileg_execution_protections(
        funding_basis=payload.get("funding_basis"),
        options_arbitrage=payload.get("options_arbitrage"),
    )


_MARKET_MAKER_STATUS_PRIORITY = [
    "error",
    "open_order_sync_error",
    "execution_error",
    "reconciliation_required",
    "coordination_cancel_retry",
    "cancel_retry",
    "blocked_by_risk",
    "placed",
    "unchanged",
    "planned",
    "starting",
    "disabled",
    "coordinating",
    "paused",
]

_MARKET_MAKER_PROBLEM_STATUSES = {
    "error",
    "open_order_sync_error",
    "execution_error",
    "reconciliation_required",
    "coordination_cancel_retry",
    "cancel_retry",
    "blocked_by_risk",
}


def _first_text(items: Any) -> str | None:
    if not isinstance(items, list):
        return None
    for item in items:
        text = str(item or "").strip()
        if text:
            return text
    return None


def _market_maker_runtime_reason(runtime: dict[str, Any]) -> str | None:
    for field in ("last_error", "open_order_sync_error", "reason"):
        text = str(runtime.get(field) or "").strip()
        if text:
            return text

    last_risk = runtime.get("last_risk")
    if isinstance(last_risk, dict):
        text = _first_text(last_risk.get("reasons")) or _first_text(
            last_risk.get("warnings")
        )
        if text:
            return text

    last_execution = runtime.get("last_execution")
    if isinstance(last_execution, dict):
        text = str(last_execution.get("reason") or "").strip()
        if text:
            return text
        text = _first_text(last_execution.get("reasons")) or _first_text(
            last_execution.get("warnings")
        )
        if text:
            return text

    return None


def _market_maker_payload_reason(instance: dict[str, Any]) -> str | None:
    runtime = instance.get("runtime")
    if isinstance(runtime, dict):
        text = _market_maker_runtime_reason(runtime)
        if text:
            return text

    for field in (
        "last_error",
        "open_order_sync_error",
        "error",
        "status_reason",
        "reason",
    ):
        text = str(instance.get(field) or "").strip()
        if text:
            return text

    last_risk = instance.get("last_risk")
    if isinstance(last_risk, dict):
        text = _first_text(last_risk.get("reasons")) or _first_text(
            last_risk.get("warnings")
        )
        if text:
            return text

    last_execution = instance.get("last_execution")
    if isinstance(last_execution, dict):
        text = str(last_execution.get("reason") or "").strip()
        if text:
            return text
        text = _first_text(last_execution.get("reasons")) or _first_text(
            last_execution.get("warnings")
        )
        if text:
            return text

    safety = instance.get("safety")
    if isinstance(safety, dict):
        text = _first_text(safety.get("reasons")) or _first_text(safety.get("warnings"))
        if text:
            return text

    config = instance.get("config")
    if isinstance(config, dict) and config.get("id_mismatch"):
        instance_id = str(config.get("id") or "").strip()
        expected_id = str(config.get("expected_id") or "").strip()
        if instance_id and expected_id:
            return f"ID mismatch: {instance_id} should be {expected_id}"

    return None


def _market_maker_display_name(instance: dict[str, Any]) -> str:
    config = instance.get("config") if isinstance(instance.get("config"), dict) else {}
    exchange = str(config.get("exchange") or instance.get("exchange") or "account")
    symbol = str(config.get("symbol") or instance.get("symbol") or "symbol")
    return f"{exchange} {symbol}".strip()


def _market_maker_status_priority(status: Any) -> int:
    text = str(status or "")
    try:
        return _MARKET_MAKER_STATUS_PRIORITY.index(text)
    except ValueError:
        return len(_MARKET_MAKER_STATUS_PRIORITY)


def _annotate_market_maker_instance(
    instance: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = runtime if runtime is not None else instance.get("runtime")
    annotated = dict(instance)
    if isinstance(runtime, dict):
        annotated["runtime"] = runtime
        annotated["status"] = runtime.get("status", annotated.get("status"))
        annotated["mode"] = runtime.get("mode", annotated.get("mode"))
        if isinstance(runtime.get("last_plan"), dict):
            annotated["plan"] = runtime["last_plan"]
        runtime_error = runtime.get("last_error") or runtime.get(
            "open_order_sync_error"
        )
        if runtime_error:
            annotated["error"] = runtime_error
    annotated["display_name"] = _market_maker_display_name(annotated)
    annotated["status_reason"] = _market_maker_payload_reason(annotated)
    return annotated


class MonitorState:
    def __init__(
        self,
        cfg: BotConfig,
        poll_seconds: float,
        *,
        runtime_store_path: str | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._base_cfg = cfg
        self._program_running = True
        self._program_updated_at = time.time()
        self._auto_stopped = False
        self._auto_stop_reason: str | None = None
        self._auto_stopped_at: float | None = None
        self._runtime_store_path = (
            Path(runtime_store_path) if runtime_store_path else None
        )
        self._config_version_store = (
            ConfigVersionStore(
                self._runtime_store_path.with_suffix(
                    self._runtime_store_path.suffix + ".versions.sqlite3"
                )
            )
            if self._runtime_store_path is not None
            else None
        )
        self._runtime_store_loaded = False
        self._runtime_store_updated_at: float | None = None
        self._runtime_store_saved_at: float | None = None
        self._runtime_store_error: str | None = None
        store_data: dict[str, Any] = {}
        if self._runtime_store_path is not None:
            loaded = _load_runtime_overrides(self._runtime_store_path, cfg)
            self._runtime_store_loaded = bool(loaded.get("loaded"))
            self._runtime_store_updated_at = (
                float(loaded["updated_at"]) if loaded.get("updated_at") else None
            )
            self._runtime_store_error = loaded.get("error")
            store_data = loaded.get("data", {})
        program = store_data.get("program") if isinstance(store_data, dict) else {}
        if isinstance(program, dict):
            self._program_running = bool(program.get("running", True))
            if isinstance(program.get("updated_at"), (int, float)):
                self._program_updated_at = float(program["updated_at"])
            self._auto_stopped = bool(program.get("auto_stopped", False))
            self._auto_stop_reason = (
                str(program["stop_reason"])
                if isinstance(program.get("stop_reason"), str)
                else None
            )
            self._auto_stopped_at = (
                float(program["stopped_at"])
                if isinstance(program.get("stopped_at"), (int, float))
                else None
            )
            if self._auto_stopped:
                self._program_running = False
        self._risk_overrides: dict[str, Any] = dict(
            store_data.get("risk_overrides", {})
        )
        self._market_maker_overrides: dict[str, Any] = dict(
            store_data.get("market_maker_overrides", {})
        )
        self._market_maker_instances_override: list[MarketMakerConfig] | None = None
        market_maker_instances_raw = store_data.get("market_maker_instances")
        if isinstance(market_maker_instances_raw, list):
            try:
                self._market_maker_instances_override = (
                    market_maker_configs_from_payload(
                        market_maker_instances_raw,
                        base_configs=market_maker_configs_for_runtime(cfg),
                    )
                )
            except ValueError as exc:
                self._runtime_store_error = f"invalid market_maker_instances: {exc}"
        self._slow_execution_overrides: dict[str, Any] = dict(
            store_data.get("slow_execution_overrides", {})
        )
        self._cross_exchange_rebalance_overrides: dict[str, Any] = dict(
            store_data.get("cross_exchange_rebalance_overrides", {})
        )
        self._spot_grid_overrides: dict[str, Any] = dict(
            store_data.get("spot_grid_overrides", {})
        )
        self._dca_overrides: dict[str, Any] = dict(store_data.get("dca_overrides", {}))
        self._execution_algo_overrides: dict[str, Any] = dict(
            store_data.get("execution_algo_overrides", {})
        )
        self._backtest_overrides: dict[str, Any] = dict(
            store_data.get("backtest_overrides", {})
        )
        self._spot_markets_override: list[SpotMarketConfig] | None = (
            _spot_markets_from_payload(
                {"spot_markets": store_data["spot_markets"]},
                allowed_exchanges={exchange.key for exchange in cfg.spot_exchanges},
            )
            if "spot_markets" in store_data
            else None
        )
        self._cash_and_carry_pairs_override: list[CashAndCarryPair] | None = (
            _cash_and_carry_pairs_from_payload(
                {"cash_and_carry_pairs": store_data["cash_and_carry_pairs"]}
            )
            if "cash_and_carry_pairs" in store_data
            else None
        )
        self._strategy_paused: dict[str, bool] = {
            strategy_id: False for strategy_id in STRATEGY_IDS
        }
        self._strategy_paused.update(store_data.get("strategy_paused", {}))
        self._coordination_holds: dict[str, dict[str, Any]] = {}
        runtime_cfg = self._runtime_config_unlocked(cfg)
        self._payload = _build_initial_payload(runtime_cfg, poll_seconds)
        if "market_maker" in self._payload:
            self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                _all_account_exchanges(runtime_cfg),
                market_maker_symbols_for_accounts(runtime_cfg, base_cfg=cfg),
                spot_markets=runtime_cfg.spot_markets,
            )
        if not self._program_running:
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                self._payload["warnings"] = [
                    self._auto_stop_reason or "Program auto-stopped"
                ]
            else:
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused"]
        self._payload["program"] = self._program_payload_unlocked()
        self._payload["runtime_store"] = self._runtime_store_status_unlocked()
        self._market_maker_runtime: dict[str, Any] = self._payload["market_maker"][
            "runtime"
        ]
        self._spot_grid_runtime: dict[str, Any] = self._payload["spot_grid"].get(
            "runtime",
            {
                "status": "starting",
                "mode": "dry_run",
                "open_order_ids": [],
                "open_order_count": 0,
                "updated_at": time.time(),
            },
        )
        self._cross_exchange_rebalance_runtime: dict[str, Any] = self._payload[
            "cross_exchange_rebalance"
        ].get("runtime", {})
        self._auto_buy_sell_tasks = self._payload["slow_execution"]["tasks"]
        self._recent_opportunities: deque[dict[str, Any]] = deque(maxlen=100)
        self._state_view_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._refresh_strategy_lifecycle_unlocked(runtime_cfg)
        if self._config_version_store is not None:
            try:
                existing_version = self._config_version_store.latest()
                self._config_version_store.record(
                    self._runtime_store_payload_unlocked(),
                    actor_email="system",
                    action="service_start",
                    known_good=existing_version is None,
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                self._runtime_store_error = f"config version store: {exc}"

    def _runtime_store_status_unlocked(self) -> dict[str, Any]:
        return {
            "enabled": self._runtime_store_path is not None,
            "path": str(self._runtime_store_path or ""),
            "loaded": self._runtime_store_loaded,
            "updated_at": self._runtime_store_updated_at,
            "saved_at": self._runtime_store_saved_at,
            "error": self._runtime_store_error,
        }

    def _clear_state_view_cache_unlocked(self) -> None:
        self._state_view_cache.clear()

    def _refresh_strategy_lifecycle_unlocked(
        self,
        cfg: BotConfig | None = None,
    ) -> dict[str, Any]:
        runtime_cfg = cfg or self._runtime_config_unlocked(self._base_cfg)
        lifecycle = build_strategy_lifecycle_payload(
            runtime_cfg,
            program=self._program_payload_unlocked(),
            strategy_paused=self._strategy_paused,
            market_maker=self._payload.get("market_maker", {}),
            auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            cross_exchange_rebalance=self._payload.get(
                "cross_exchange_rebalance",
                {},
            ),
            spot_arbitrage=self._payload.get("spot_arbitrage", {}),
        )
        self._payload["strategy_lifecycle"] = lifecycle
        return lifecycle

    def _runtime_store_payload_unlocked(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": time.time(),
            "risk_overrides": self._risk_overrides,
            "market_maker_overrides": self._market_maker_overrides,
            "slow_execution_overrides": self._slow_execution_overrides,
            "cross_exchange_rebalance_overrides": (
                self._cross_exchange_rebalance_overrides
            ),
            "spot_grid_overrides": self._spot_grid_overrides,
            "dca_overrides": self._dca_overrides,
            "execution_algo_overrides": self._execution_algo_overrides,
            "backtest_overrides": self._backtest_overrides,
            "strategy_paused": self._strategy_paused,
            "program": self._program_payload_unlocked(),
        }
        if self._market_maker_instances_override is not None:
            payload["market_maker_instances"] = market_maker_configs_to_list(
                self._market_maker_instances_override
            )
        if self._spot_markets_override is not None:
            payload["spot_markets"] = spot_markets_to_list(self._spot_markets_override)
        if self._cash_and_carry_pairs_override is not None:
            payload["cash_and_carry_pairs"] = cash_and_carry_pairs_to_list(
                self._cash_and_carry_pairs_override
            )
        return payload

    def _save_runtime_store_unlocked(
        self,
        *,
        actor_email: str = "system",
        action: str = "runtime_update",
        known_good: bool = False,
    ) -> None:
        if self._runtime_store_path is None:
            return
        payload = self._runtime_store_payload_unlocked()
        error = _save_runtime_overrides(self._runtime_store_path, payload)
        self._runtime_store_error = error
        if error is None:
            self._runtime_store_loaded = True
            self._runtime_store_updated_at = float(payload["updated_at"])
            self._runtime_store_saved_at = time.time()
            if self._config_version_store is not None:
                try:
                    self._config_version_store.record(
                        payload,
                        actor_email=actor_email,
                        action=action,
                        known_good=known_good,
                    )
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self._runtime_store_error = f"config version store: {exc}"
        if "runtime_store" in self._payload:
            self._payload["runtime_store"] = self._runtime_store_status_unlocked()
        self._clear_state_view_cache_unlocked()

    def _refresh_config_payloads_unlocked(self, runtime_cfg: BotConfig) -> None:
        if "config" in self._payload:
            self._payload["config"]["spot_markets"] = spot_markets_to_list(
                runtime_cfg.spot_markets
            )
            self._payload["config"]["spot_exchanges"] = exchange_configs_to_list(
                runtime_cfg.spot_exchanges
            )
            self._payload["config"]["cash_and_carry_pairs"] = (
                cash_and_carry_pairs_to_list(runtime_cfg.cash_and_carry_pairs)
            )
            self._payload["config"]["strategy_universe"] = strategy_universe_to_dict(
                runtime_cfg
            )
        if "market_maker" in self._payload:
            self._payload["market_maker"]["config"] = market_maker_config_to_dict(
                runtime_cfg.market_maker
            )
            self._payload["market_maker"]["instances"] = market_maker_configs_to_list(
                market_maker_configs_for_runtime(runtime_cfg)
            )
            self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                _all_account_exchanges(runtime_cfg),
                market_maker_symbols_for_accounts(
                    runtime_cfg,
                    base_cfg=self._base_cfg,
                ),
                spot_markets=runtime_cfg.spot_markets,
            )
        symbols_by_exchange = _spot_symbols_by_exchange(runtime_cfg)
        if "slow_execution" in self._payload:
            self._payload["slow_execution"]["config"] = slow_execution_config_to_dict(
                runtime_cfg.slow_execution
            )
            self._payload["slow_execution"]["accounts"] = slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                symbols_by_exchange,
                spot_markets=runtime_cfg.spot_markets,
            )
        if "cross_exchange_rebalance" in self._payload:
            self._payload["cross_exchange_rebalance"]["config"] = (
                cross_exchange_rebalance_config_to_dict(
                    runtime_cfg.cross_exchange_rebalance
                )
            )
            self._payload["cross_exchange_rebalance"]["accounts"] = (
                slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _rebalance_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            )
        section_configs = (
            ("spot_grid", spot_grid_config_to_dict(runtime_cfg.spot_grid)),
            ("dca", dca_config_to_dict(runtime_cfg.dca)),
            (
                "execution_algo",
                execution_algo_config_to_dict(runtime_cfg.execution_algo),
            ),
            ("backtest", backtest_config_to_dict(runtime_cfg.backtest)),
        )
        for section, config in section_configs:
            if section not in self._payload:
                continue
            self._payload[section]["config"] = config
            self._payload[section]["accounts"] = slow_execution_accounts(
                runtime_cfg.spot_exchanges,
                (
                    _grid_symbols_by_exchange(runtime_cfg)
                    if section in {"spot_grid", "dca"}
                    else _execution_symbols_by_exchange(runtime_cfg)
                ),
                spot_markets=runtime_cfg.spot_markets,
            )
        self._refresh_operations_controls_unlocked(runtime_cfg)
        self._payload["trading_console"] = build_trading_console_payload(
            runtime_cfg,
            strategy_paused=self._strategy_paused,
            order_activity=self._payload.get("order_activity", {}),
            auto_buy_sell_tasks=self._auto_buy_sell_tasks,
        )
        self._refresh_strategy_lifecycle_unlocked(runtime_cfg)

    def _refresh_operations_controls_unlocked(self, runtime_cfg: BotConfig) -> None:
        """Update control-plane fields without rereading large append-only logs."""
        operations = self._payload.get("operations")
        if not isinstance(operations, dict):
            operations = {}
            self._payload["operations"] = operations
        operations["risk"] = risk_config_to_dict(runtime_cfg.risk)

    async def config_versions(self, *, limit: int = 30) -> dict[str, Any]:
        async with self._lock:
            current = self._runtime_store_payload_unlocked()
            if self._config_version_store is None:
                return {
                    "enabled": False,
                    "current_hash": configuration_hash(current),
                    "versions": [],
                }
            versions = self._config_version_store.list(limit=limit)
            return {
                "enabled": True,
                "path": str(self._config_version_store.path),
                "current_hash": configuration_hash(current),
                "current_version_id": versions[0]["id"] if versions else None,
                "versions": versions,
            }

    async def startup_config_guard_candidate(self) -> dict[str, Any] | None:
        async with self._lock:
            if self._config_version_store is None:
                return None
            current_hash = configuration_hash(self._runtime_store_payload_unlocked())
            latest = self._config_version_store.latest()
            if latest is None or latest["hash"] != current_hash or latest["known_good"]:
                return None
            previous = self._config_version_store.latest_known_good(
                before_id=int(latest["id"]),
            )
            if previous is None:
                return None
            return {
                "version_id": int(latest["id"]),
                "hash": current_hash,
                "previous_known_good_id": int(previous["id"]),
            }

    async def mark_current_config_known_good(
        self,
        *,
        expected_current_hash: str = "",
    ) -> dict[str, Any] | None:
        async with self._lock:
            if self._config_version_store is None:
                return None
            current = self._runtime_store_payload_unlocked()
            current_hash = configuration_hash(current)
            if expected_current_hash and expected_current_hash != current_hash:
                return None
            version = self._config_version_store.record(
                current,
                actor_email="system",
                action="preflight_passed",
                known_good=True,
            )
            return version

    async def rollback_config_version(
        self,
        version_id: int,
        *,
        expected_current_hash: str,
        actor_email: str,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._config_version_store is None:
                raise ValueError("configuration versioning is not enabled")
            current_payload = self._runtime_store_payload_unlocked()
            current_hash = configuration_hash(current_payload)
            if not expected_current_hash or expected_current_hash != current_hash:
                raise ValueError(
                    "configuration changed after this page loaded; refresh before rollback"
                )
            target = self._config_version_store.get(
                int(version_id),
                include_payload=True,
            )
            if target is None:
                raise ValueError(f"configuration version not found: {version_id}")
            data = target["payload"]
            snapshot = {
                "risk": self._risk_overrides,
                "market_maker": self._market_maker_overrides,
                "market_maker_instances": self._market_maker_instances_override,
                "slow_execution": self._slow_execution_overrides,
                "rebalance": self._cross_exchange_rebalance_overrides,
                "spot_grid": self._spot_grid_overrides,
                "dca": self._dca_overrides,
                "execution_algo": self._execution_algo_overrides,
                "backtest": self._backtest_overrides,
                "spot_markets": self._spot_markets_override,
                "cash_and_carry": self._cash_and_carry_pairs_override,
                "strategy_paused": self._strategy_paused,
            }
            try:
                self._risk_overrides = dict(data.get("risk_overrides", {}))
                self._market_maker_overrides = dict(
                    data.get("market_maker_overrides", {})
                )
                mm_raw = data.get("market_maker_instances")
                self._market_maker_instances_override = (
                    market_maker_configs_from_payload(
                        mm_raw,
                        base_configs=market_maker_configs_for_runtime(self._base_cfg),
                    )
                    if isinstance(mm_raw, list)
                    else None
                )
                self._slow_execution_overrides = dict(
                    data.get("slow_execution_overrides", {})
                )
                self._cross_exchange_rebalance_overrides = dict(
                    data.get("cross_exchange_rebalance_overrides", {})
                )
                self._spot_grid_overrides = dict(data.get("spot_grid_overrides", {}))
                self._dca_overrides = dict(data.get("dca_overrides", {}))
                self._execution_algo_overrides = dict(
                    data.get("execution_algo_overrides", {})
                )
                self._backtest_overrides = dict(data.get("backtest_overrides", {}))
                self._spot_markets_override = (
                    _spot_markets_from_payload(
                        {"spot_markets": data["spot_markets"]},
                        allowed_exchanges={
                            exchange.key for exchange in self._base_cfg.spot_exchanges
                        },
                    )
                    if isinstance(data.get("spot_markets"), list)
                    else None
                )
                self._cash_and_carry_pairs_override = (
                    _cash_and_carry_pairs_from_payload(
                        {"cash_and_carry_pairs": data["cash_and_carry_pairs"]}
                    )
                    if isinstance(data.get("cash_and_carry_pairs"), list)
                    else None
                )
                pauses = {
                    strategy_id: bool(value)
                    for strategy_id, value in dict(
                        data.get("strategy_paused", {})
                    ).items()
                    if strategy_id in STRATEGY_IDS
                }
                self._strategy_paused = {
                    strategy_id: pauses.get(strategy_id, False)
                    for strategy_id in STRATEGY_IDS
                }
                runtime_cfg = self._runtime_config_unlocked(self._base_cfg)
                target_live_enabled = bool(
                    runtime_cfg.risk.allow_live_trading
                    or any(
                        item.enabled and item.live_enabled
                        for item in market_maker_configs_for_runtime(runtime_cfg)
                    )
                    or (
                        runtime_cfg.cross_exchange_rebalance.enabled
                        and runtime_cfg.cross_exchange_rebalance.live_enabled
                    )
                    or (
                        runtime_cfg.spot_grid.enabled
                        and runtime_cfg.spot_grid.live_enabled
                    )
                    or (runtime_cfg.dca.enabled and runtime_cfg.dca.live_enabled)
                    or (
                        runtime_cfg.execution_algo.enabled
                        and runtime_cfg.execution_algo.live_enabled
                    )
                )
                if target_live_enabled and not bool(target.get("known_good")):
                    raise ValueError(
                        "an unverified configuration version cannot enable live trading"
                    )
            except (KeyError, TypeError, ValueError):
                self._risk_overrides = snapshot["risk"]
                self._market_maker_overrides = snapshot["market_maker"]
                self._market_maker_instances_override = snapshot[
                    "market_maker_instances"
                ]
                self._slow_execution_overrides = snapshot["slow_execution"]
                self._cross_exchange_rebalance_overrides = snapshot["rebalance"]
                self._spot_grid_overrides = snapshot["spot_grid"]
                self._dca_overrides = snapshot["dca"]
                self._execution_algo_overrides = snapshot["execution_algo"]
                self._backtest_overrides = snapshot["backtest"]
                self._spot_markets_override = snapshot["spot_markets"]
                self._cash_and_carry_pairs_override = snapshot["cash_and_carry"]
                self._strategy_paused = snapshot["strategy_paused"]
                raise
            self._refresh_config_payloads_unlocked(runtime_cfg)
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=f"rollback_to_version_{version_id}",
                known_good=bool(target.get("known_good")),
            )
            versions = self._config_version_store.list(limit=30)
            return {
                "ok": True,
                "rolled_back_to": int(version_id),
                "current_hash": configuration_hash(
                    self._runtime_store_payload_unlocked()
                ),
                "current_version_id": versions[0]["id"] if versions else None,
                "versions": versions,
            }

    def _program_payload_unlocked(self) -> dict[str, Any]:
        return {
            "running": self._program_running,
            "updated_at": self._program_updated_at,
            "auto_stopped": self._auto_stopped,
            "stop_reason": self._auto_stop_reason,
            "stopped_at": self._auto_stopped_at,
        }

    def _runtime_config_unlocked(self, cfg: BotConfig) -> BotConfig:
        legacy_market_maker = market_maker_config_with_id(
            replace(
                cfg.market_maker,
                **self._market_maker_overrides,
            )
        )
        if self._market_maker_instances_override is not None:
            market_maker_instances = self._market_maker_instances_override
        elif self._market_maker_overrides:
            market_maker_instances = [legacy_market_maker]
        else:
            market_maker_instances = market_maker_configs_for_runtime(cfg)
        primary_market_maker = (
            market_maker_instances[0] if market_maker_instances else legacy_market_maker
        )
        return replace(
            cfg,
            spot_markets=(
                self._spot_markets_override
                if self._spot_markets_override is not None
                else cfg.spot_markets
            ),
            cash_and_carry_pairs=(
                self._cash_and_carry_pairs_override
                if self._cash_and_carry_pairs_override is not None
                else cfg.cash_and_carry_pairs
            ),
            risk=replace(cfg.risk, **self._risk_overrides),
            market_maker=primary_market_maker,
            market_makers=market_maker_instances,
            slow_execution=replace(
                cfg.slow_execution,
                **self._slow_execution_overrides,
            ),
            cross_exchange_rebalance=replace(
                cfg.cross_exchange_rebalance,
                **self._cross_exchange_rebalance_overrides,
            ),
            spot_grid=replace(
                cfg.spot_grid,
                **self._spot_grid_overrides,
            ),
            dca=replace(
                cfg.dca,
                **self._dca_overrides,
            ),
            execution_algo=replace(
                cfg.execution_algo,
                **self._execution_algo_overrides,
            ),
            backtest=replace(
                cfg.backtest,
                **self._backtest_overrides,
            ),
        )

    async def get(
        self,
        view: str | None = None,
        sections: str | None = None,
    ) -> dict[str, Any]:
        cache_key = _state_view_cache_key(view, sections)
        now = time.monotonic()
        async with self._lock:
            cached = self._state_view_cache.get(cache_key)
            if cached is not None and now - cached[0] <= _STATE_VIEW_CACHE_TTL_SECONDS:
                payload_text = cached[1]
            else:
                self._refresh_strategy_lifecycle_unlocked()
                payload = state_payload_for_view(
                    self._payload,
                    view,
                    sections=sections,
                )
                payload_text = json.dumps(payload, separators=(",", ":"))
                self._state_view_cache[cache_key] = (now, payload_text)
        return json.loads(payload_text)

    async def strategy_lifecycle(self) -> dict[str, Any]:
        async with self._lock:
            lifecycle = self._refresh_strategy_lifecycle_unlocked()
            return json.loads(json.dumps(lifecycle))

    async def portfolio_payload(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._payload.get("portfolio", {})))

    async def strategy_preflight_payload(self) -> dict[str, Any]:
        """Return only the live state needed by strategy safety checks."""
        async with self._lock:
            payload = {
                "quote_rates": self._payload.get("quote_rates", {}),
                "markets": self._payload.get("markets", []),
                "account_balances": self._payload.get("account_balances", {}),
                "order_activity": self._payload.get("order_activity", {}),
                "market_maker": self._payload.get("market_maker", {}),
                "slow_execution": self._payload.get("slow_execution", {}),
            }
            return json.loads(json.dumps(payload))

    async def quote_rates(self) -> dict[str, float]:
        async with self._lock:
            raw = self._payload.get("quote_rates", self._base_cfg.quote_rates)
            if not isinstance(raw, dict):
                raw = self._base_cfg.quote_rates
            return {
                str(currency).upper(): float(rate)
                for currency, rate in raw.items()
                if isinstance(rate, (int, float)) and float(rate) > 0
            }

    async def is_running(self) -> bool:
        async with self._lock:
            return self._program_running

    async def program_updated_at(self) -> float:
        async with self._lock:
            return self._program_updated_at

    async def slow_execution_config(
        self,
        base_config: SlowExecutionConfig,
    ) -> SlowExecutionConfig:
        async with self._lock:
            return replace(base_config, **self._slow_execution_overrides)

    async def market_maker_config(
        self,
        base_config: MarketMakerConfig,
    ) -> MarketMakerConfig:
        async with self._lock:
            return replace(base_config, **self._market_maker_overrides)

    async def market_maker_configs(
        self,
        base_configs: list[MarketMakerConfig],
    ) -> list[MarketMakerConfig]:
        async with self._lock:
            if self._market_maker_instances_override is not None:
                return market_maker_configs_with_ids(
                    self._market_maker_instances_override
                )
            if self._market_maker_overrides:
                base = base_configs[0] if base_configs else MarketMakerConfig()
                return [
                    market_maker_config_with_id(
                        replace(base, **self._market_maker_overrides)
                    )
                ]
            return market_maker_configs_with_ids(base_configs)

    async def spot_grid_config(
        self,
        base_config: SpotGridConfig,
    ) -> SpotGridConfig:
        async with self._lock:
            return replace(base_config, **self._spot_grid_overrides)

    async def cross_exchange_rebalance_config(
        self,
        base_config: CrossExchangeRebalanceConfig,
    ) -> CrossExchangeRebalanceConfig:
        async with self._lock:
            return replace(
                base_config,
                **self._cross_exchange_rebalance_overrides,
            )

    async def dca_config(
        self,
        base_config: DcaConfig,
    ) -> DcaConfig:
        async with self._lock:
            return replace(base_config, **self._dca_overrides)

    async def execution_algo_config(
        self,
        base_config: ExecutionAlgoConfig,
    ) -> ExecutionAlgoConfig:
        async with self._lock:
            return replace(base_config, **self._execution_algo_overrides)

    async def backtest_config(
        self,
        base_config: BacktestConfig,
    ) -> BacktestConfig:
        async with self._lock:
            return replace(base_config, **self._backtest_overrides)

    async def set_market_maker_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "market_maker_update",
    ) -> dict[str, Any]:
        async with self._lock:
            if self._market_maker_instances_override is not None:
                runtime_cfg_before = self._runtime_config_unlocked(cfg)
                instances = market_maker_configs_for_runtime(runtime_cfg_before)
                target_id = str(overrides.get("id") or instances[0].id)
                updated: list[MarketMakerConfig] = []
                replaced_target = False
                for instance in instances:
                    if instance.id == target_id:
                        updated.append(
                            market_maker_config_with_id(replace(instance, **overrides))
                        )
                        replaced_target = True
                    else:
                        updated.append(instance)
                if not replaced_target:
                    updated.append(
                        market_maker_config_with_id(
                            replace(MarketMakerConfig(), **overrides)
                        )
                    )
                self._market_maker_instances_override = market_maker_configs_with_ids(
                    updated
                )
                self._market_maker_overrides = {}
            else:
                self._market_maker_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "market_maker" in self._payload:
                self._payload["market_maker"]["config"] = market_maker_config_to_dict(
                    runtime_cfg.market_maker
                )
                self._payload["market_maker"]["instances"] = (
                    market_maker_configs_to_list(
                        market_maker_configs_for_runtime(runtime_cfg)
                    )
                )
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    market_maker_symbols_for_accounts(runtime_cfg, base_cfg=cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._payload["config"]["strategy_universe"] = strategy_universe_to_dict(
                runtime_cfg
            )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": market_maker_config_to_dict(runtime_cfg.market_maker),
                        "instances": market_maker_configs_to_list(
                            market_maker_configs_for_runtime(runtime_cfg)
                        ),
                        "market_maker": self._payload.get("market_maker", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_market_maker_instances(
        self,
        instances: list[MarketMakerConfig],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "market_maker_instances_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._market_maker_instances_override = market_maker_configs_with_ids(
                instances
            )
            self._market_maker_overrides = {}
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "market_maker" in self._payload:
                self._payload["market_maker"]["config"] = market_maker_config_to_dict(
                    runtime_cfg.market_maker
                )
                self._payload["market_maker"]["instances"] = (
                    market_maker_configs_to_list(
                        market_maker_configs_for_runtime(runtime_cfg)
                    )
                )
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    market_maker_symbols_for_accounts(runtime_cfg, base_cfg=cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._payload["config"]["strategy_universe"] = strategy_universe_to_dict(
                runtime_cfg
            )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": market_maker_config_to_dict(runtime_cfg.market_maker),
                        "instances": market_maker_configs_to_list(
                            market_maker_configs_for_runtime(runtime_cfg)
                        ),
                        "market_maker": self._payload.get("market_maker", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_cross_exchange_rebalance_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "cross_exchange_rebalance_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._cross_exchange_rebalance_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            payload = self._payload.get("cross_exchange_rebalance")
            if isinstance(payload, dict):
                payload["config"] = cross_exchange_rebalance_config_to_dict(
                    runtime_cfg.cross_exchange_rebalance
                )
                payload["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _rebalance_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": cross_exchange_rebalance_config_to_dict(
                            runtime_cfg.cross_exchange_rebalance
                        ),
                        "cross_exchange_rebalance": self._payload.get(
                            "cross_exchange_rebalance",
                            {},
                        ),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_spot_grid_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "spot_grid_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._spot_grid_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "spot_grid" in self._payload:
                current_config = self._payload["spot_grid"].get("config", {})
                current_config.update(overrides)
                self._payload["spot_grid"]["config"] = current_config
                self._payload["spot_grid"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _grid_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": spot_grid_config_to_dict(runtime_cfg.spot_grid),
                        "spot_grid": self._payload.get("spot_grid", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_dca_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "dca_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._dca_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "dca" in self._payload:
                current_config = self._payload["dca"].get("config", {})
                current_config.update(overrides)
                self._payload["dca"]["config"] = current_config
                self._payload["dca"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _grid_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": dca_config_to_dict(runtime_cfg.dca),
                        "dca": self._payload.get("dca", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_execution_algo_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "execution_algo_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._execution_algo_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "execution_algo" in self._payload:
                current_config = self._payload["execution_algo"].get("config", {})
                current_config.update(overrides)
                self._payload["execution_algo"]["config"] = current_config
                self._payload["execution_algo"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _execution_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": execution_algo_config_to_dict(
                            runtime_cfg.execution_algo
                        ),
                        "execution_algo": self._payload.get("execution_algo", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_backtest_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "backtest_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._backtest_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "backtest" in self._payload:
                current_config = self._payload["backtest"].get("config", {})
                current_config.update(overrides)
                self._payload["backtest"]["config"] = current_config
                self._payload["backtest"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _execution_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "config": backtest_config_to_dict(runtime_cfg.backtest),
                        "backtest": self._payload.get("backtest", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def risk_config(
        self,
        base_config: RiskConfig,
    ) -> RiskConfig:
        async with self._lock:
            return replace(base_config, **self._risk_overrides)

    async def runtime_config(self, cfg: BotConfig) -> BotConfig:
        async with self._lock:
            return self._runtime_config_unlocked(cfg)

    async def set_slow_execution_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig | None = None,
        actor_email: str = "system",
        action: str = "auto_buy_sell_update",
    ) -> None:
        async with self._lock:
            self._slow_execution_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg) if cfg else None
            if "slow_execution" in self._payload:
                current_config = self._payload["slow_execution"].get("config", {})
                current_config.update(overrides)
                self._payload["slow_execution"]["config"] = current_config
                if runtime_cfg is not None:
                    self._payload["slow_execution"]["accounts"] = (
                        slow_execution_accounts(
                            runtime_cfg.spot_exchanges,
                            _spot_symbols_by_exchange(runtime_cfg),
                            spot_markets=runtime_cfg.spot_markets,
                        )
                    )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )

    async def set_spot_markets(
        self,
        markets: list[SpotMarketConfig],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "spot_markets_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._spot_markets_override = markets
            runtime_cfg = self._runtime_config_unlocked(cfg)
            symbols_by_exchange = _spot_symbols_by_exchange(runtime_cfg)
            if "config" in self._payload:
                self._payload["config"]["spot_markets"] = spot_markets_to_list(
                    runtime_cfg.spot_markets
                )
                self._payload["config"]["spot_exchanges"] = exchange_configs_to_list(
                    runtime_cfg.spot_exchanges
                )
                self._payload["config"]["strategy_universe"] = (
                    strategy_universe_to_dict(runtime_cfg)
                )
            if "market_maker" in self._payload:
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    market_maker_symbols_for_accounts(runtime_cfg, base_cfg=cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            if "cross_exchange_rebalance" in self._payload:
                self._payload["cross_exchange_rebalance"]["accounts"] = (
                    slow_execution_accounts(
                        runtime_cfg.spot_exchanges,
                        _rebalance_symbols_by_exchange(runtime_cfg),
                        spot_markets=runtime_cfg.spot_markets,
                    )
                )
            if "slow_execution" in self._payload:
                self._payload["slow_execution"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    symbols_by_exchange,
                    spot_markets=runtime_cfg.spot_markets,
                )
            if "spot_grid" in self._payload:
                self._payload["spot_grid"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _grid_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            if "dca" in self._payload:
                self._payload["dca"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _grid_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            if "execution_algo" in self._payload:
                self._payload["execution_algo"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _execution_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            if "backtest" in self._payload:
                self._payload["backtest"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _execution_symbols_by_exchange(runtime_cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "ok": True,
                        "spot_markets": spot_markets_to_list(runtime_cfg.spot_markets),
                        "market_maker": self._payload.get("market_maker", {}),
                        "slow_execution": self._payload.get("slow_execution", {}),
                        "spot_grid": self._payload.get("spot_grid", {}),
                        "dca": self._payload.get("dca", {}),
                        "execution_algo": self._payload.get("execution_algo", {}),
                        "backtest": self._payload.get("backtest", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_cash_and_carry_pairs(
        self,
        pairs: list[CashAndCarryPair],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "cash_and_carry_update",
    ) -> dict[str, Any]:
        async with self._lock:
            self._cash_and_carry_pairs_override = pairs
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "config" in self._payload:
                self._payload["config"]["cash_and_carry_pairs"] = (
                    cash_and_carry_pairs_to_list(runtime_cfg.cash_and_carry_pairs)
                )
                self._payload["config"]["derivative_exchanges"] = (
                    exchange_configs_to_list(runtime_cfg.derivative_exchanges)
                )
                self._payload["config"]["strategy_universe"] = (
                    strategy_universe_to_dict(runtime_cfg)
                )
            if "market_maker" in self._payload:
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    market_maker_symbols_for_accounts(runtime_cfg, base_cfg=cfg),
                    spot_markets=runtime_cfg.spot_markets,
                )
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "ok": True,
                        "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                            runtime_cfg.cash_and_carry_pairs
                        ),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_risk_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "risk_update",
    ) -> dict[str, Any]:
        async with self._lock:
            current_risk = self._runtime_config_unlocked(cfg).risk
            for field in ("account_enabled", "strategy_enabled"):
                if field in overrides:
                    merged = dict(getattr(current_risk, field))
                    merged.update(overrides[field])
                    overrides[field] = merged
            self._risk_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            self._refresh_operations_controls_unlocked(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(
                json.dumps(
                    {
                        "risk": risk_config_to_dict(runtime_cfg.risk),
                        "trading_console": self._payload["trading_console"],
                        "operations": self._payload["operations"],
                    }
                )
            )

    async def strategy_pauses(self) -> dict[str, bool]:
        async with self._lock:
            return dict(self._strategy_paused)

    def _prune_coordination_holds_unlocked(self) -> None:
        now = time.time()
        self._coordination_holds = {
            owner: hold
            for owner, hold in self._coordination_holds.items()
            if float(hold.get("expires_at") or 0.0) > now
        }

    async def acquire_coordination_hold(
        self,
        owner: str,
        resources: list[tuple[str, str] | tuple[str, str, str]],
        *,
        reason: str,
        ttl_seconds: float,
    ) -> dict[str, Any]:
        normalized: set[tuple[str, str, str]] = set()
        for resource in resources:
            if len(resource) == 2:
                exchange, symbol = resource
                side = ""
            else:
                exchange, symbol, side = resource
            exchange = str(exchange).strip()
            symbol = str(symbol).strip()
            side = str(side).strip().lower()
            if side not in {"", "buy", "sell"}:
                raise ValueError("coordination resource side must be buy or sell")
            if exchange and symbol:
                normalized.add((exchange, symbol, side))
        normalized_rows = sorted(normalized)
        if not owner.strip():
            raise ValueError("coordination owner is required")
        if not normalized:
            raise ValueError("at least one coordination resource is required")
        now = time.time()
        async with self._lock:
            self._prune_coordination_holds_unlocked()
            previous = self._coordination_holds.get(owner)
            hold = {
                "owner": owner,
                "reason": reason,
                "resources": [
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        **({"side": side} if side else {}),
                    }
                    for exchange, symbol, side in normalized_rows
                ],
                "acquired_at": (
                    float(previous.get("acquired_at") or now) if previous else now
                ),
                "updated_at": now,
                "expires_at": now + max(1.0, float(ttl_seconds)),
            }
            self._coordination_holds[owner] = hold
            return json.loads(json.dumps(hold))

    async def release_coordination_hold(self, owner: str) -> bool:
        async with self._lock:
            self._prune_coordination_holds_unlocked()
            return self._coordination_holds.pop(owner, None) is not None

    async def coordination_hold_for(
        self,
        exchange: str,
        symbol: str,
        *,
        requester: str = "",
    ) -> dict[str, Any] | None:
        async with self._lock:
            self._prune_coordination_holds_unlocked()
            for owner, hold in sorted(self._coordination_holds.items()):
                if owner == requester:
                    continue
                if any(
                    resource.get("exchange") == exchange
                    and resource.get("symbol") == symbol
                    for resource in hold.get("resources", [])
                    if isinstance(resource, dict)
                ):
                    return json.loads(json.dumps(hold))
            return None

    async def coordination_holds(self) -> list[dict[str, Any]]:
        async with self._lock:
            self._prune_coordination_holds_unlocked()
            return json.loads(
                json.dumps(
                    [
                        self._coordination_holds[owner]
                        for owner in sorted(self._coordination_holds)
                    ]
                )
            )

    async def set_strategy_paused(
        self,
        strategy_id: str,
        paused: bool,
        *,
        cfg: BotConfig,
        actor_email: str = "system",
        action: str = "strategy_pause_update",
    ) -> dict[str, Any]:
        if strategy_id not in STRATEGY_IDS:
            raise ValueError(f"unknown strategy: {strategy_id}")
        async with self._lock:
            self._strategy_paused[strategy_id] = paused
            runtime_cfg = self._runtime_config_unlocked(cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._refresh_strategy_lifecycle_unlocked(runtime_cfg)
            self._save_runtime_store_unlocked(
                actor_email=actor_email,
                action=action,
            )
            return json.loads(json.dumps(self._payload["trading_console"]))

    async def set_running(self, running: bool) -> dict[str, Any]:
        async with self._lock:
            self._program_running = running
            self._program_updated_at = time.time()
            if running:
                self._auto_stopped = False
                self._auto_stop_reason = None
                self._auto_stopped_at = None
                self._payload["status"] = "starting"
                self._payload["warnings"] = ["Resuming scans"]
            else:
                self._auto_stopped = False
                self._auto_stop_reason = None
                self._auto_stopped_at = None
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused"]
            self._payload["program"] = self._program_payload_unlocked()
            self._refresh_strategy_lifecycle_unlocked()
            self._save_runtime_store_unlocked()
            self._clear_state_view_cache_unlocked()
            return json.loads(json.dumps(self._payload))

    async def set_auto_stopped(
        self,
        *,
        reason: str,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._program_running = False
            self._program_updated_at = time.time()
            self._auto_stopped = True
            self._auto_stop_reason = reason
            self._auto_stopped_at = self._program_updated_at
            self._payload["status"] = "auto_stopped"
            self._payload["program"] = self._program_payload_unlocked()
            self._payload["warnings"] = list(warnings or [reason])
            self._refresh_strategy_lifecycle_unlocked()
            self._save_runtime_store_unlocked()
            self._clear_state_view_cache_unlocked()
            return json.loads(json.dumps(self._payload))

    async def set_paused(self) -> None:
        async with self._lock:
            self._payload["program"] = self._program_payload_unlocked()
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                if self._auto_stop_reason:
                    self._payload["warnings"] = [self._auto_stop_reason]
                self._refresh_strategy_lifecycle_unlocked()
                self._clear_state_view_cache_unlocked()
                return
            self._payload["status"] = "paused"
            self._payload["warnings"] = ["Program paused"]
            self._refresh_strategy_lifecycle_unlocked()
            self._clear_state_view_cache_unlocked()

    async def set_order_activity(self, order_activity: dict[str, Any]) -> None:
        async with self._lock:
            self._payload["order_activity"] = order_activity
            reliability = order_activity.get("reliability")
            if isinstance(reliability, dict):
                existing = self._payload.get("order_reliability")
                self._payload["order_reliability"] = {
                    **(existing if isinstance(existing, dict) else {}),
                    **reliability,
                }
            self._clear_state_view_cache_unlocked()

    async def set_order_reliability(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._payload["order_reliability"] = json.loads(json.dumps(payload))
            order_activity = self._payload.get("order_activity")
            if isinstance(order_activity, dict):
                order_activity["reliability"] = json.loads(json.dumps(payload))
            self._clear_state_view_cache_unlocked()

    async def set_readonly_health(
        self,
        *,
        cfg: BotConfig,
        exec_cfg: SlowExecutionConfig,
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
        derivatives: dict[str, Any] | None = None,
        funding_basis: dict[str, Any] | None = None,
        options_arbitrage: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        async with self._lock:
            warning_messages = list(warnings or [])
            trading_console = build_trading_console_payload(
                cfg,
                exec_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=order_activity,
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._payload["account_balances"] = account_balances
            self._payload["order_activity"] = order_activity
            if derivatives is not None:
                self._payload["derivatives"] = derivatives
            if funding_basis is not None:
                self._payload["funding_basis"] = funding_basis
            if options_arbitrage is not None:
                self._payload["options_arbitrage"] = options_arbitrage
            self._payload["execution_protection"] = _execution_protection_from_payloads(
                self._payload
            )
            self._payload["contract_strategies"] = build_contract_strategies_payload(
                cfg,
                funding_basis=self._payload.get("funding_basis", {}),
                derivatives=self._payload.get("derivatives", {}),
                market_maker=self._payload.get("market_maker", {}),
                order_activity=order_activity,
            )
            self._payload["trading_console"] = trading_console
            self._payload["readiness"] = build_readiness_payload(
                cfg,
                account_balances=account_balances,
                order_activity=order_activity,
                derivatives=self._payload.get("derivatives", {}),
                trading_console=trading_console,
                market_maker=self._payload.get("market_maker", {}),
                slow_execution=self._payload.get("slow_execution", {}),
                spot_grid=self._payload.get("spot_grid", {}),
                dca=self._payload.get("dca", {}),
                execution_algo=self._payload.get("execution_algo", {}),
                backtest=self._payload.get("backtest", {}),
                execution_protection=self._payload.get("execution_protection", {}),
                markets=self._payload.get("markets", []),
                warnings=warning_messages,
            )
            self._payload["program"] = self._program_payload_unlocked()
            self._payload["runtime_store"] = self._runtime_store_status_unlocked()
            self._payload["operations"] = build_operations_payload(cfg)
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                self._payload["warnings"] = [
                    item
                    for item in [
                        self._auto_stop_reason or "Program auto-stopped",
                        *warning_messages,
                    ]
                    if item
                ]
            elif not self._program_running:
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused", *warning_messages]
            self._clear_state_view_cache_unlocked()

    async def set_market_maker_runtime(self, runtime: dict[str, Any]) -> None:
        async with self._lock:
            self._market_maker_runtime = runtime
            if "market_maker" in self._payload:
                self._payload["market_maker"]["runtime"] = runtime
                if isinstance(runtime.get("last_plan"), dict):
                    self._payload["market_maker"]["plan"] = runtime["last_plan"]
                if runtime.get("mode"):
                    self._payload["market_maker"]["mode"] = runtime["mode"]
                if runtime.get("status"):
                    self._payload["market_maker"]["status"] = runtime["status"]
                self._payload["market_maker"]["status_reason"] = runtime.get(
                    "status_reason"
                )
                self._payload["market_maker"]["error"] = runtime.get(
                    "last_error"
                ) or runtime.get("status_reason")
            self._refresh_strategy_lifecycle_unlocked()
            self._clear_state_view_cache_unlocked()

    def _aggregate_market_maker_runtime_unlocked(
        self,
        instances: list[dict[str, Any]],
    ) -> dict[str, Any]:
        instances = [_annotate_market_maker_instance(item) for item in instances]
        active_instances = [
            item
            for item in instances
            if item.get("status") not in {"disabled", "paused"}
        ]
        selected = min(
            instances,
            key=lambda item: _market_maker_status_priority(item.get("status")),
            default={},
        )
        aggregate_status = str(selected.get("status") or "disabled")
        problem_instances = [
            {
                "id": item.get("id"),
                "display_name": item.get("display_name"),
                "status": item.get("status"),
                "reason": item.get("status_reason"),
            }
            for item in instances
            if item.get("status") in _MARKET_MAKER_PROBLEM_STATUSES
        ]
        return {
            "status": aggregate_status,
            "mode": "live"
            if any(item.get("mode") == "live" for item in instances)
            else selected.get("mode", "dry_run"),
            "instances": instances,
            "instance_count": len(instances),
            "active_instance_count": len(active_instances),
            "problem_instance_count": len(problem_instances),
            "problem_instances": problem_instances,
            "open_order_count": sum(
                int(item.get("open_order_count", 0) or 0) for item in instances
            ),
            "placed_count": sum(
                int(item.get("placed_count", 0) or 0) for item in instances
            ),
            "canceled_count": sum(
                int(item.get("canceled_count", 0) or 0) for item in instances
            ),
            "cycle_count": sum(
                int(item.get("cycle_count", 0) or 0) for item in instances
            ),
            "last_plan": selected.get("last_plan"),
            "last_risk": selected.get("last_risk"),
            "last_execution": selected.get("last_execution"),
            "last_error": selected.get("last_error"),
            "status_reason": selected.get("status_reason"),
            "market_data": selected.get("market_data"),
            "updated_at": time.time(),
        }

    def _sync_market_maker_payload_runtime_unlocked(
        self,
        runtime: dict[str, Any],
    ) -> None:
        if "market_maker" not in self._payload:
            return
        market_maker = self._payload["market_maker"]
        market_maker["runtime"] = runtime
        if isinstance(runtime.get("last_plan"), dict):
            market_maker["plan"] = runtime["last_plan"]
        if runtime.get("mode"):
            market_maker["mode"] = runtime["mode"]
        if runtime.get("status"):
            market_maker["status"] = runtime["status"]
        market_maker["status_reason"] = runtime.get("status_reason")
        market_maker["error"] = runtime.get("last_error") or runtime.get(
            "status_reason"
        )
        runtime_by_id = {
            str(item.get("id") or ""): item
            for item in runtime.get("instances", [])
            if isinstance(item, dict)
        }
        instances = market_maker.get("instances")
        if isinstance(instances, list):
            updated_instances: list[dict[str, Any]] = []
            for instance in instances:
                if not isinstance(instance, dict):
                    continue
                config = (
                    instance.get("config")
                    if isinstance(instance.get("config"), dict)
                    else {}
                )
                instance_id = str(config.get("id") or instance.get("id") or "")
                instance_runtime = runtime_by_id.get(instance_id)
                if instance_runtime is not None:
                    updated_instances.append(
                        _annotate_market_maker_instance(
                            instance,
                            runtime=instance_runtime,
                        )
                    )
                else:
                    updated_instances.append(_annotate_market_maker_instance(instance))
            market_maker["instances"] = updated_instances

    async def set_market_maker_instance_runtime(
        self,
        instance_id: str,
        runtime: dict[str, Any],
    ) -> None:
        async with self._lock:
            instance_id = str(instance_id)
            runtime = {**runtime, "id": instance_id}
            current_instances = {
                str(item.get("id") or ""): item
                for item in self._market_maker_runtime.get("instances", [])
                if isinstance(item, dict)
            }
            current_instances[instance_id] = runtime
            instances = list(current_instances.values())
            aggregate = self._aggregate_market_maker_runtime_unlocked(instances)
            self._market_maker_runtime = aggregate
            self._sync_market_maker_payload_runtime_unlocked(aggregate)
            self._refresh_strategy_lifecycle_unlocked()
            self._clear_state_view_cache_unlocked()

    async def market_maker_runtime(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._market_maker_runtime))

    async def set_cross_exchange_rebalance_runtime(
        self,
        runtime: dict[str, Any],
    ) -> None:
        async with self._lock:
            self._cross_exchange_rebalance_runtime = runtime
            payload = self._payload.get("cross_exchange_rebalance")
            if isinstance(payload, dict):
                payload["runtime"] = runtime
                last_payload = (
                    runtime.get("last_payload")
                    if isinstance(runtime.get("last_payload"), dict)
                    else {}
                )
                payload["status"] = runtime.get("status", payload.get("status"))
                payload["mode"] = last_payload.get("mode", payload.get("mode"))
                payload["plan"] = last_payload.get("plan", payload.get("plan"))
                payload["risk"] = last_payload.get("risk")
                payload["execution"] = last_payload.get("execution")
                payload["error"] = runtime.get("last_error")
            self._refresh_strategy_lifecycle_unlocked()
            self._clear_state_view_cache_unlocked()

    async def cross_exchange_rebalance_runtime(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._cross_exchange_rebalance_runtime))

    async def set_spot_grid_runtime(self, runtime: dict[str, Any]) -> None:
        async with self._lock:
            self._spot_grid_runtime = runtime
            if "spot_grid" in self._payload:
                self._payload["spot_grid"]["runtime"] = runtime
                if isinstance(runtime.get("last_plan"), dict):
                    self._payload["spot_grid"]["plan"] = runtime["last_plan"]
                if runtime.get("mode"):
                    self._payload["spot_grid"]["mode"] = runtime["mode"]
                if runtime.get("status"):
                    self._payload["spot_grid"]["status"] = runtime["status"]
                self._payload["spot_grid"]["error"] = runtime.get("last_error")
            self._clear_state_view_cache_unlocked()

    async def spot_grid_runtime(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._spot_grid_runtime))

    async def set_auto_buy_sell_tasks(self, tasks: dict[str, Any]) -> None:
        async with self._lock:
            self._auto_buy_sell_tasks = tasks
            if "slow_execution" in self._payload:
                self._payload["slow_execution"]["tasks"] = tasks
            self._refresh_strategy_lifecycle_unlocked()
            self._clear_state_view_cache_unlocked()

    async def auto_buy_sell_tasks(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._auto_buy_sell_tasks))

    async def set_scan_result(
        self,
        *,
        cfg: BotConfig,
        poll_seconds: float,
        scan_count: int,
        started_at: float,
        elapsed_ms: int,
        markets: list[dict[str, Any]],
        quote_rates: dict[str, float],
        opportunities: list[Opportunity],
        warnings: list[str],
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
        derivatives: dict[str, Any],
        funding_basis: dict[str, Any],
        options_arbitrage: dict[str, Any],
        onchain: dict[str, Any],
        market_maker: dict[str, Any],
        slow_execution: dict[str, Any],
        spot_grid: dict[str, Any],
        dca: dict[str, Any],
        execution_algo: dict[str, Any],
        backtest: dict[str, Any],
        spot_arbitrage: dict[str, Any],
        trading_console: dict[str, Any],
        portfolio: dict[str, Any],
    ) -> None:
        opportunity_dicts = [item.to_dict() for item in opportunities]
        for item in opportunity_dicts:
            self._recent_opportunities.appendleft(item)

        status = "running" if not warnings else "degraded"
        async with self._lock:
            slow_execution["tasks"] = self._auto_buy_sell_tasks
            market_maker["accounts"] = slow_execution_accounts(
                _all_account_exchanges(cfg),
                market_maker_symbols_for_accounts(cfg, base_cfg=self._base_cfg),
                spot_markets=cfg.spot_markets,
            )
            market_maker["runtime"] = self._market_maker_runtime
            if isinstance(self._market_maker_runtime.get("last_plan"), dict):
                market_maker["plan"] = self._market_maker_runtime["last_plan"]
            if self._market_maker_runtime.get("mode"):
                market_maker["mode"] = self._market_maker_runtime["mode"]
            if self._market_maker_runtime.get("status"):
                market_maker["status"] = self._market_maker_runtime["status"]
            market_maker["status_reason"] = self._market_maker_runtime.get(
                "status_reason"
            )
            if self._market_maker_runtime.get(
                "last_error"
            ) or self._market_maker_runtime.get("status_reason"):
                market_maker["error"] = self._market_maker_runtime.get(
                    "last_error"
                ) or self._market_maker_runtime.get("status_reason")
            runtime_by_id = {
                str(item.get("id") or ""): item
                for item in self._market_maker_runtime.get("instances", [])
                if isinstance(item, dict)
            }
            if isinstance(market_maker.get("instances"), list):
                merged_instances: list[dict[str, Any]] = []
                for instance in market_maker["instances"]:
                    if not isinstance(instance, dict):
                        continue
                    config = (
                        instance.get("config")
                        if isinstance(instance.get("config"), dict)
                        else {}
                    )
                    instance_id = str(config.get("id") or instance.get("id") or "")
                    instance_runtime = runtime_by_id.get(instance_id)
                    if instance_runtime is None:
                        merged_instances.append(
                            _annotate_market_maker_instance(instance)
                        )
                        continue
                    merged_instances.append(
                        _annotate_market_maker_instance(
                            instance,
                            runtime=instance_runtime,
                        )
                    )
                market_maker["instances"] = merged_instances
            market_maker["quality"] = build_market_maker_quality_payload(
                order_activity,
                market_maker,
                portfolio,
            )
            contract_strategies = build_contract_strategies_payload(
                cfg,
                funding_basis=funding_basis,
                derivatives=derivatives,
                market_maker=market_maker,
                order_activity=order_activity,
                now=started_at,
            )
            spot_grid["runtime"] = self._spot_grid_runtime
            if isinstance(self._spot_grid_runtime.get("last_plan"), dict):
                spot_grid["plan"] = self._spot_grid_runtime["last_plan"]
            if self._spot_grid_runtime.get("mode"):
                spot_grid["mode"] = self._spot_grid_runtime["mode"]
            if self._spot_grid_runtime.get("status"):
                spot_grid["status"] = self._spot_grid_runtime["status"]
            if self._spot_grid_runtime.get("last_error"):
                spot_grid["error"] = self._spot_grid_runtime["last_error"]
            execution_protection = summarize_multileg_execution_protections(
                funding_basis=funding_basis,
                options_arbitrage=options_arbitrage,
            )
            cross_exchange_rebalance = dict(
                self._payload.get("cross_exchange_rebalance", {})
            )
            cross_exchange_rebalance["config"] = (
                cross_exchange_rebalance_config_to_dict(cfg.cross_exchange_rebalance)
            )
            cross_exchange_rebalance["accounts"] = slow_execution_accounts(
                cfg.spot_exchanges,
                _rebalance_symbols_by_exchange(cfg),
                spot_markets=cfg.spot_markets,
            )
            cross_exchange_rebalance["runtime"] = self._cross_exchange_rebalance_runtime
            self._payload = {
                "status": status,
                "config": {
                    "poll_seconds": poll_seconds,
                    "notional_quote": cfg.notional_quote,
                    "min_profit_quote": cfg.min_profit_quote,
                    "min_profit_bps": cfg.min_profit_bps,
                    "common_quote_currency": cfg.common_quote_currency,
                    "spot_markets": spot_markets_to_list(cfg.spot_markets),
                    "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                        cfg.cash_and_carry_pairs
                    ),
                    "contract_strategies": contract_strategies_config_to_dict(
                        cfg.contract_strategies
                    ),
                    "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
                    "derivative_exchanges": exchange_configs_to_list(
                        cfg.derivative_exchanges
                    ),
                    "strategy_universe": strategy_universe_to_dict(cfg),
                },
                "scan": {
                    "count": scan_count,
                    "elapsed_ms": elapsed_ms,
                    "last_started": started_at,
                    "last_finished": time.time(),
                },
                "markets": markets,
                "quote_rates": quote_rates,
                "opportunities": opportunity_dicts,
                "recent_opportunities": list(self._recent_opportunities),
                "account_balances": account_balances,
                "derivatives": derivatives,
                "funding_basis": funding_basis,
                "options_arbitrage": options_arbitrage,
                "contract_strategies": contract_strategies,
                "execution_protection": execution_protection,
                "order_activity": order_activity,
                "onchain": onchain,
                "market_maker": market_maker,
                "slow_execution": slow_execution,
                "cross_exchange_rebalance": cross_exchange_rebalance,
                "spot_grid": spot_grid,
                "dca": dca,
                "execution_algo": execution_algo,
                "backtest": backtest,
                "spot_arbitrage": spot_arbitrage,
                "trading_console": trading_console,
                "readiness": build_readiness_payload(
                    cfg,
                    account_balances=account_balances,
                    order_activity=order_activity,
                    derivatives=derivatives,
                    trading_console=trading_console,
                    market_maker=market_maker,
                    slow_execution=slow_execution,
                    spot_grid=spot_grid,
                    dca=dca,
                    execution_algo=execution_algo,
                    backtest=backtest,
                    execution_protection=execution_protection,
                    markets=markets,
                    warnings=warnings,
                ),
                "portfolio": portfolio,
                "program": self._program_payload_unlocked(),
                "runtime_store": self._runtime_store_status_unlocked(),
                "operations": build_operations_payload(cfg),
                "warnings": warnings,
            }
            self._refresh_strategy_lifecycle_unlocked(cfg)
            self._clear_state_view_cache_unlocked()

    async def set_error(
        self,
        *,
        cfg: BotConfig,
        poll_seconds: float,
        scan_count: int,
        started_at: float,
        elapsed_ms: int,
        error: str,
    ) -> None:
        async with self._lock:
            self._payload.update(
                {
                    "status": "error",
                    "config": {
                        "poll_seconds": poll_seconds,
                        "notional_quote": cfg.notional_quote,
                        "min_profit_quote": cfg.min_profit_quote,
                        "min_profit_bps": cfg.min_profit_bps,
                        "common_quote_currency": cfg.common_quote_currency,
                        "spot_markets": spot_markets_to_list(cfg.spot_markets),
                        "cash_and_carry_pairs": cash_and_carry_pairs_to_list(
                            cfg.cash_and_carry_pairs
                        ),
                        "contract_strategies": contract_strategies_config_to_dict(
                            cfg.contract_strategies
                        ),
                        "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
                        "derivative_exchanges": exchange_configs_to_list(
                            cfg.derivative_exchanges
                        ),
                        "strategy_universe": strategy_universe_to_dict(cfg),
                    },
                    "scan": {
                        "count": scan_count,
                        "elapsed_ms": elapsed_ms,
                        "last_started": started_at,
                        "last_finished": time.time(),
                    },
                    "warnings": [error],
                    "readiness": build_readiness_payload(cfg, warnings=[error]),
                    "program": self._program_payload_unlocked(),
                    "operations": build_operations_payload(cfg),
                }
            )
            self._refresh_strategy_lifecycle_unlocked(cfg)
            self._clear_state_view_cache_unlocked()


__all__ = ["MonitorState"]
