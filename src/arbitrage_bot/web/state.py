from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from .render_payloads import state_payload_for_view

from ..config import (
    BacktestConfig,
    BotConfig,
    CashAndCarryPair,
    DcaConfig,
    ExecutionAlgoConfig,
    MarketMakerConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
    SpotMarketConfig,
)
from ..models import Opportunity
from ..portfolio_metrics import build_market_maker_quality_payload
from ..web_config import (
    _cash_and_carry_pairs_from_payload,
    _execution_symbols_by_exchange,
    _grid_symbols_by_exchange,
    _market_maker_symbols_by_exchange,
    _spot_markets_from_payload,
    _spot_symbols_by_exchange,
    backtest_config_to_dict,
    cash_and_carry_pairs_to_list,
    dca_config_to_dict,
    execution_algo_config_to_dict,
    exchange_configs_to_list,
    market_maker_config_to_dict,
    risk_config_to_dict,
    slow_execution_accounts,
    spot_markets_to_list,
    spot_grid_config_to_dict,
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


class MonitorState:
    def __init__(
        self,
        cfg: BotConfig,
        poll_seconds: float,
        *,
        runtime_store_path: str | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._program_running = True
        self._program_updated_at = time.time()
        self._auto_stopped = False
        self._auto_stop_reason: str | None = None
        self._auto_stopped_at: float | None = None
        self._runtime_store_path = Path(runtime_store_path) if runtime_store_path else None
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
        self._slow_execution_overrides: dict[str, Any] = dict(
            store_data.get("slow_execution_overrides", {})
        )
        self._spot_grid_overrides: dict[str, Any] = dict(
            store_data.get("spot_grid_overrides", {})
        )
        self._dca_overrides: dict[str, Any] = dict(
            store_data.get("dca_overrides", {})
        )
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
        runtime_cfg = self._runtime_config_unlocked(cfg)
        self._payload = _build_initial_payload(runtime_cfg, poll_seconds)
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
        self._auto_buy_sell_tasks = self._payload["slow_execution"]["tasks"]
        self._recent_opportunities: deque[dict[str, Any]] = deque(maxlen=100)

    def _runtime_store_status_unlocked(self) -> dict[str, Any]:
        return {
            "enabled": self._runtime_store_path is not None,
            "path": str(self._runtime_store_path or ""),
            "loaded": self._runtime_store_loaded,
            "updated_at": self._runtime_store_updated_at,
            "saved_at": self._runtime_store_saved_at,
            "error": self._runtime_store_error,
        }

    def _runtime_store_payload_unlocked(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": time.time(),
            "risk_overrides": self._risk_overrides,
            "market_maker_overrides": self._market_maker_overrides,
            "slow_execution_overrides": self._slow_execution_overrides,
            "spot_grid_overrides": self._spot_grid_overrides,
            "dca_overrides": self._dca_overrides,
            "execution_algo_overrides": self._execution_algo_overrides,
            "backtest_overrides": self._backtest_overrides,
            "strategy_paused": self._strategy_paused,
            "program": self._program_payload_unlocked(),
        }
        if self._spot_markets_override is not None:
            payload["spot_markets"] = spot_markets_to_list(
                self._spot_markets_override
            )
        if self._cash_and_carry_pairs_override is not None:
            payload["cash_and_carry_pairs"] = cash_and_carry_pairs_to_list(
                self._cash_and_carry_pairs_override
            )
        return payload

    def _save_runtime_store_unlocked(self) -> None:
        if self._runtime_store_path is None:
            return
        payload = self._runtime_store_payload_unlocked()
        error = _save_runtime_overrides(self._runtime_store_path, payload)
        self._runtime_store_error = error
        if error is None:
            self._runtime_store_loaded = True
            self._runtime_store_updated_at = float(payload["updated_at"])
            self._runtime_store_saved_at = time.time()
        if "runtime_store" in self._payload:
            self._payload["runtime_store"] = self._runtime_store_status_unlocked()

    def _program_payload_unlocked(self) -> dict[str, Any]:
        return {
            "running": self._program_running,
            "updated_at": self._program_updated_at,
            "auto_stopped": self._auto_stopped,
            "stop_reason": self._auto_stop_reason,
            "stopped_at": self._auto_stopped_at,
        }

    def _runtime_config_unlocked(self, cfg: BotConfig) -> BotConfig:
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
            market_maker=replace(
                cfg.market_maker,
                **self._market_maker_overrides,
            ),
            slow_execution=replace(
                cfg.slow_execution,
                **self._slow_execution_overrides,
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

    async def get(self, view: str | None = None) -> dict[str, Any]:
        async with self._lock:
            payload = state_payload_for_view(self._payload, view)
            return json.loads(json.dumps(payload))

    async def portfolio_payload(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._payload.get("portfolio", {})))

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

    async def spot_grid_config(
        self,
        base_config: SpotGridConfig,
    ) -> SpotGridConfig:
        async with self._lock:
            return replace(base_config, **self._spot_grid_overrides)

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
    ) -> dict[str, Any]:
        async with self._lock:
            self._market_maker_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg)
            if "market_maker" in self._payload:
                current_config = self._payload["market_maker"].get("config", {})
                current_config.update(overrides)
                self._payload["market_maker"]["config"] = current_config
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    _market_maker_symbols_by_exchange(runtime_cfg),
                )
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
            return json.loads(
                json.dumps(
                    {
                        "config": market_maker_config_to_dict(
                            runtime_cfg.market_maker
                        ),
                        "market_maker": self._payload.get("market_maker", {}),
                        "trading_console": self._payload["trading_console"],
                    }
                )
            )

    async def set_spot_grid_overrides(
        self,
        overrides: dict[str, Any],
        *,
        cfg: BotConfig,
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
                )
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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
                )
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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
                )
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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
                )
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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
    ) -> None:
        async with self._lock:
            self._slow_execution_overrides.update(overrides)
            runtime_cfg = self._runtime_config_unlocked(cfg) if cfg else None
            if "slow_execution" in self._payload:
                current_config = self._payload["slow_execution"].get("config", {})
                current_config.update(overrides)
                self._payload["slow_execution"]["config"] = current_config
                if runtime_cfg is not None:
                    self._payload["slow_execution"]["accounts"] = slow_execution_accounts(
                        runtime_cfg.spot_exchanges,
                        _spot_symbols_by_exchange(runtime_cfg),
                    )
            self._save_runtime_store_unlocked()

    async def set_spot_markets(
        self,
        markets: list[SpotMarketConfig],
        *,
        cfg: BotConfig,
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
            if "market_maker" in self._payload:
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    _market_maker_symbols_by_exchange(runtime_cfg),
                )
            if "slow_execution" in self._payload:
                self._payload["slow_execution"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    symbols_by_exchange,
                )
            if "spot_grid" in self._payload:
                self._payload["spot_grid"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _grid_symbols_by_exchange(runtime_cfg),
                )
            if "dca" in self._payload:
                self._payload["dca"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _grid_symbols_by_exchange(runtime_cfg),
                )
            if "execution_algo" in self._payload:
                self._payload["execution_algo"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _execution_symbols_by_exchange(runtime_cfg),
                )
            if "backtest" in self._payload:
                self._payload["backtest"]["accounts"] = slow_execution_accounts(
                    runtime_cfg.spot_exchanges,
                    _execution_symbols_by_exchange(runtime_cfg),
                )
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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
            if "market_maker" in self._payload:
                self._payload["market_maker"]["accounts"] = slow_execution_accounts(
                    _all_account_exchanges(runtime_cfg),
                    _market_maker_symbols_by_exchange(runtime_cfg),
                )
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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
            self._payload["operations"] = build_operations_payload(runtime_cfg)
            self._payload["trading_console"] = build_trading_console_payload(
                runtime_cfg,
                strategy_paused=self._strategy_paused,
                order_activity=self._payload.get("order_activity", {}),
                auto_buy_sell_tasks=self._auto_buy_sell_tasks,
            )
            self._save_runtime_store_unlocked()
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

    async def set_strategy_paused(
        self,
        strategy_id: str,
        paused: bool,
        *,
        cfg: BotConfig,
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
            self._save_runtime_store_unlocked()
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
            self._save_runtime_store_unlocked()
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
            self._save_runtime_store_unlocked()
            return json.loads(json.dumps(self._payload))

    async def set_paused(self) -> None:
        async with self._lock:
            self._payload["program"] = self._program_payload_unlocked()
            if self._auto_stopped:
                self._payload["status"] = "auto_stopped"
                if self._auto_stop_reason:
                    self._payload["warnings"] = [self._auto_stop_reason]
                return
            self._payload["status"] = "paused"
            self._payload["warnings"] = ["Program paused"]

    async def set_order_activity(self, order_activity: dict[str, Any]) -> None:
        async with self._lock:
            self._payload["order_activity"] = order_activity

    async def set_readonly_health(
        self,
        *,
        cfg: BotConfig,
        exec_cfg: SlowExecutionConfig,
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
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
            self._payload["trading_console"] = trading_console
            self._payload["readiness"] = build_readiness_payload(
                cfg,
                account_balances=account_balances,
                order_activity=order_activity,
                trading_console=trading_console,
                market_maker=self._payload.get("market_maker", {}),
                slow_execution=self._payload.get("slow_execution", {}),
                spot_grid=self._payload.get("spot_grid", {}),
                dca=self._payload.get("dca", {}),
                execution_algo=self._payload.get("execution_algo", {}),
                backtest=self._payload.get("backtest", {}),
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
                self._payload["market_maker"]["error"] = runtime.get("last_error")

    async def market_maker_runtime(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._market_maker_runtime))

    async def set_auto_buy_sell_tasks(self, tasks: dict[str, Any]) -> None:
        async with self._lock:
            self._auto_buy_sell_tasks = tasks
            if "slow_execution" in self._payload:
                self._payload["slow_execution"]["tasks"] = tasks

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
            market_maker["runtime"] = self._market_maker_runtime
            if isinstance(self._market_maker_runtime.get("last_plan"), dict):
                market_maker["plan"] = self._market_maker_runtime["last_plan"]
            if self._market_maker_runtime.get("mode"):
                market_maker["mode"] = self._market_maker_runtime["mode"]
            if self._market_maker_runtime.get("status"):
                market_maker["status"] = self._market_maker_runtime["status"]
            if self._market_maker_runtime.get("last_error"):
                market_maker["error"] = self._market_maker_runtime["last_error"]
            market_maker["quality"] = build_market_maker_quality_payload(
                order_activity,
                market_maker,
                portfolio,
            )
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
                    "spot_exchanges": exchange_configs_to_list(cfg.spot_exchanges),
                    "derivative_exchanges": exchange_configs_to_list(
                        cfg.derivative_exchanges
                    ),
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
                "order_activity": order_activity,
                "onchain": onchain,
                "market_maker": market_maker,
                "slow_execution": slow_execution,
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
                    trading_console=trading_console,
                    market_maker=market_maker,
                    slow_execution=slow_execution,
                    spot_grid=spot_grid,
                    dca=dca,
                    execution_algo=execution_algo,
                    backtest=backtest,
                    markets=markets,
                    warnings=warnings,
                ),
                "portfolio": portfolio,
                "program": self._program_payload_unlocked(),
                "runtime_store": self._runtime_store_status_unlocked(),
                "operations": build_operations_payload(cfg),
                "warnings": warnings,
            }

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
                        "spot_exchanges": exchange_configs_to_list(
                            cfg.spot_exchanges
                        ),
                        "derivative_exchanges": exchange_configs_to_list(
                            cfg.derivative_exchanges
                        ),
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


__all__ = ["MonitorState"]
