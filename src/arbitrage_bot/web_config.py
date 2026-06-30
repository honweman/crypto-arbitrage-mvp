from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import Any

from .config import (
    BotConfig,
    BacktestConfig,
    CashAndCarryPair,
    ContractStrategiesConfig,
    DcaConfig,
    ExchangeConfig,
    ExecutionAlgoConfig,
    MarketMakerConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
    SpotMarketConfig,
)


def spot_grid_config_to_dict(cfg: SpotGridConfig) -> dict[str, Any]:
    return asdict(cfg)


def dca_config_to_dict(cfg: DcaConfig) -> dict[str, Any]:
    return asdict(cfg)


def execution_algo_config_to_dict(cfg: ExecutionAlgoConfig) -> dict[str, Any]:
    return asdict(cfg)


def backtest_config_to_dict(cfg: BacktestConfig) -> dict[str, Any]:
    return asdict(cfg)


def contract_strategies_config_to_dict(
    cfg: ContractStrategiesConfig,
) -> dict[str, Any]:
    return asdict(cfg)


def slow_execution_config_to_dict(cfg: SlowExecutionConfig) -> dict[str, Any]:
    return asdict(cfg)


def market_maker_config_to_dict(cfg: MarketMakerConfig) -> dict[str, Any]:
    return asdict(market_maker_config_with_id(cfg))


def market_maker_instance_id(cfg: MarketMakerConfig) -> str:
    configured = str(cfg.id or "").strip()
    if configured:
        return configured
    seed = f"{cfg.exchange}-{cfg.symbol}" if cfg.exchange and cfg.symbol else "default"
    normalized = "".join(
        character.lower() if character.isalnum() else "-"
        for character in seed
    )
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized or "default"


def market_maker_config_with_id(cfg: MarketMakerConfig) -> MarketMakerConfig:
    return replace(cfg, id=market_maker_instance_id(cfg))


def market_maker_configs_with_ids(
    configs: Iterable[MarketMakerConfig],
) -> list[MarketMakerConfig]:
    result: list[MarketMakerConfig] = []
    seen: dict[str, int] = {}
    for cfg in configs:
        base_id = market_maker_instance_id(cfg)
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        instance_id = base_id if count == 0 else f"{base_id}-{count + 1}"
        result.append(replace(cfg, id=instance_id))
    return result


def market_maker_configs_for_runtime(cfg: BotConfig) -> list[MarketMakerConfig]:
    return market_maker_configs_with_ids(cfg.market_makers or [cfg.market_maker])


def market_maker_configs_to_list(
    configs: Iterable[MarketMakerConfig],
) -> list[dict[str, Any]]:
    return [market_maker_config_to_dict(cfg) for cfg in configs]


def risk_config_to_dict(cfg: RiskConfig) -> dict[str, Any]:
    return asdict(cfg)


def _spot_symbols_by_exchange(cfg: BotConfig) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for market in cfg.spot_markets:
        symbols.setdefault(market.exchange, set()).add(market.symbol)
    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _market_maker_symbols_by_exchange(cfg: BotConfig) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {
        exchange: set(items)
        for exchange, items in _spot_symbols_by_exchange(cfg).items()
    }
    for pair in cfg.cash_and_carry_pairs:
        for exchange in cfg.spot_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.spot_symbol)
        for exchange in cfg.derivative_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.derivative_symbol)
    for maker_cfg in market_maker_configs_for_runtime(cfg):
        if maker_cfg.exchange and maker_cfg.symbol:
            symbols.setdefault(maker_cfg.exchange, set()).add(maker_cfg.symbol)
    return {exchange: sorted(items) for exchange, items in symbols.items()}


def market_maker_symbols_for_accounts(
    cfg: BotConfig,
    *,
    base_cfg: BotConfig | None = None,
) -> dict[str, list[str]]:
    symbols = {
        exchange: list(items)
        for exchange, items in _market_maker_symbols_by_exchange(cfg).items()
    }
    if base_cfg is None:
        return symbols
    for exchange, items in _market_maker_symbols_by_exchange(base_cfg).items():
        if not symbols.get(exchange):
            symbols[exchange] = list(items)
    return {exchange: sorted(set(items)) for exchange, items in symbols.items()}


def _grid_symbols_by_exchange(cfg: BotConfig) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {
        exchange: set(items)
        for exchange, items in _spot_symbols_by_exchange(cfg).items()
    }
    for strategy_cfg in (cfg.spot_grid, cfg.dca):
        if strategy_cfg.exchange and strategy_cfg.symbol:
            symbols.setdefault(strategy_cfg.exchange, set()).add(strategy_cfg.symbol)
    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _execution_symbols_by_exchange(cfg: BotConfig) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {
        exchange: set(items)
        for exchange, items in _spot_symbols_by_exchange(cfg).items()
    }
    for strategy_cfg in (cfg.execution_algo, cfg.backtest):
        if strategy_cfg.exchange and strategy_cfg.symbol:
            symbols.setdefault(strategy_cfg.exchange, set()).add(strategy_cfg.symbol)
    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _derivative_symbols_by_exchange(cfg: BotConfig) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for pair in cfg.cash_and_carry_pairs:
        for exchange in cfg.derivative_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.derivative_symbol)
    for combo in cfg.option_combos:
        symbols.setdefault(combo.option_exchange, set()).update(
            [combo.call_symbol, combo.put_symbol]
        )
    if (
        cfg.contract_strategies.derivative_exchange
        and cfg.contract_strategies.derivative_symbol
    ):
        symbols.setdefault(cfg.contract_strategies.derivative_exchange, set()).add(
            cfg.contract_strategies.derivative_symbol
        )
    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _merge_symbols_by_exchange(
    *items: dict[str, list[str]],
) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for item in items:
        for exchange, rows in item.items():
            symbols.setdefault(exchange, set()).update(rows)
    return {exchange: sorted(rows) for exchange, rows in symbols.items()}


def _symbol_asset(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].split(":", 1)[0].upper()


def strategy_universe_to_dict(cfg: BotConfig) -> dict[str, Any]:
    spot_symbols = _spot_symbols_by_exchange(cfg)
    grid_symbols = _grid_symbols_by_exchange(cfg)
    execution_symbols = _execution_symbols_by_exchange(cfg)
    market_maker_symbols = _market_maker_symbols_by_exchange(cfg)
    derivative_symbols = _derivative_symbols_by_exchange(cfg)
    all_symbols = _merge_symbols_by_exchange(
        spot_symbols,
        grid_symbols,
        execution_symbols,
        market_maker_symbols,
        derivative_symbols,
    )
    all_exchanges = [*cfg.spot_exchanges, *cfg.derivative_exchanges]
    assets = {
        market.asset.upper()
        for market in cfg.spot_markets
        if market.asset
    }
    for symbols in all_symbols.values():
        assets.update(_symbol_asset(symbol) for symbol in symbols if symbol)
    return {
        "assets": sorted(item for item in assets if item),
        "spot": {
            "accounts": slow_execution_accounts(cfg.spot_exchanges, spot_symbols),
        },
        "grid": {
            "accounts": slow_execution_accounts(cfg.spot_exchanges, grid_symbols),
        },
        "execution": {
            "accounts": slow_execution_accounts(
                cfg.spot_exchanges,
                execution_symbols,
            ),
        },
        "market_maker": {
            "accounts": slow_execution_accounts(
                all_exchanges,
                market_maker_symbols,
            ),
        },
        "derivative": {
            "accounts": slow_execution_accounts(
                cfg.derivative_exchanges,
                derivative_symbols,
            ),
        },
        "all": {
            "accounts": slow_execution_accounts(all_exchanges, all_symbols),
        },
    }


def slow_execution_accounts(
    exchanges: Iterable[ExchangeConfig],
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    symbols_by_exchange = symbols_by_exchange or {}
    rows = []
    for exchange in exchanges:
        symbols = symbols_by_exchange.get(exchange.key, [])
        rows.append(
            {
                "key": exchange.key,
                "label": exchange.key,
                "id": exchange.id,
                "market_type": exchange.market_type,
                "symbol": symbols[0] if symbols else "",
                "symbols": symbols,
            }
        )
    return rows


def _slow_execution_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    symbols_by_exchange = symbols_by_exchange or {}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        overrides["enabled"] = payload["enabled"]

    if "depth_simulation_enabled" in payload:
        if not isinstance(payload["depth_simulation_enabled"], bool):
            raise ValueError("depth_simulation_enabled must be a boolean")
        overrides["depth_simulation_enabled"] = payload[
            "depth_simulation_enabled"
        ]

    if "exchange" in payload:
        exchange = str(payload["exchange"]).strip()
        if not exchange:
            raise ValueError("exchange is required")
        if allowed_exchanges is not None and exchange not in allowed_exchanges:
            raise ValueError(f"unknown exchange account: {exchange}")
        overrides["exchange"] = exchange

    if "symbol" in payload:
        symbol = str(payload["symbol"]).strip()
        if not symbol:
            raise ValueError("symbol is required")
        selected_exchange = overrides.get("exchange")
        if selected_exchange and symbols_by_exchange.get(selected_exchange):
            if symbol not in symbols_by_exchange[selected_exchange]:
                raise ValueError(f"symbol is not configured for account: {symbol}")
        overrides["symbol"] = symbol
    elif "exchange" in overrides and symbols_by_exchange.get(overrides["exchange"]):
        overrides["symbol"] = symbols_by_exchange[overrides["exchange"]][0]

    if "side" in payload:
        side = str(payload["side"]).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        overrides["side"] = side

    if "price_mode" in payload:
        price_mode = str(payload["price_mode"]).lower()
        if price_mode not in {"taker", "maker"}:
            raise ValueError("price_mode must be taker or maker")
        overrides["price_mode"] = price_mode

    if "slice_mode" in payload:
        slice_mode = str(payload["slice_mode"]).lower()
        if slice_mode not in {"configured", "top_level"}:
            raise ValueError("slice_mode must be configured or top_level")
        overrides["slice_mode"] = slice_mode

    if "unlimited_total" in payload:
        if not isinstance(payload["unlimited_total"], bool):
            raise ValueError("unlimited_total must be a boolean")
        overrides["unlimited_total"] = payload["unlimited_total"]

    if "block_conflicting_market_maker" in payload:
        if not isinstance(payload["block_conflicting_market_maker"], bool):
            raise ValueError("block_conflicting_market_maker must be a boolean")
        overrides["block_conflicting_market_maker"] = payload[
            "block_conflicting_market_maker"
        ]

    numeric_fields = {
        "total_base",
        "total_quote",
        "slice_base_min",
        "slice_base_max",
        "interval_seconds",
        "order_ttl_seconds",
        "start_price",
        "stop_price",
        "price_offset_bps",
    }
    for field in numeric_fields:
        if field not in payload:
            continue
        value = float(payload[field])
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
        overrides[field] = value

    if "randomize_slice" in payload:
        if not isinstance(payload["randomize_slice"], bool):
            raise ValueError("randomize_slice must be a boolean")
        overrides["randomize_slice"] = payload["randomize_slice"]

    if "interval_seconds" in overrides and overrides["interval_seconds"] <= 0:
        raise ValueError("interval_seconds must be positive")

    overrides["slice_base"] = 0.0
    overrides["slice_quote"] = 0.0
    return overrides


def _account_symbol_overrides_from_payload(
    payload: dict[str, Any],
    *,
    allowed_exchanges: set[str] | None,
    symbols_by_exchange: dict[str, list[str]] | None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    symbols_by_exchange = symbols_by_exchange or {}
    if "exchange" in payload:
        exchange = str(payload["exchange"]).strip()
        if not exchange:
            raise ValueError("exchange is required")
        if allowed_exchanges is not None and exchange not in allowed_exchanges:
            raise ValueError(f"unknown exchange account: {exchange}")
        overrides["exchange"] = exchange

    if "symbol" in payload:
        symbol = str(payload["symbol"]).strip()
        if not symbol:
            raise ValueError("symbol is required")
        selected_exchange = overrides.get("exchange")
        if selected_exchange and symbols_by_exchange.get(selected_exchange):
            if symbol not in symbols_by_exchange[selected_exchange]:
                raise ValueError(f"symbol is not configured for account: {symbol}")
        overrides["symbol"] = symbol
    elif "exchange" in overrides and symbols_by_exchange.get(overrides["exchange"]):
        overrides["symbol"] = symbols_by_exchange[overrides["exchange"]][0]

    return overrides


def _market_maker_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    overrides: dict[str, Any] = {}
    symbols_by_exchange = symbols_by_exchange or {}

    if "id" in payload:
        instance_id = str(payload["id"]).strip()
        if instance_id:
            overrides["id"] = instance_id

    for field in {
        "enabled",
        "live_enabled",
        "post_only",
        "cancel_existing_orders",
        "inventory_control_enabled",
    }:
        if field in payload:
            if not isinstance(payload[field], bool):
                raise ValueError(f"{field} must be a boolean")
            overrides[field] = payload[field]

    if "exchange" in payload:
        exchange = str(payload["exchange"]).strip()
        if not exchange:
            raise ValueError("exchange is required")
        if allowed_exchanges is not None and exchange not in allowed_exchanges:
            raise ValueError(f"unknown exchange account: {exchange}")
        overrides["exchange"] = exchange

    if "symbol" in payload:
        symbol = str(payload["symbol"]).strip()
        if not symbol:
            raise ValueError("symbol is required")
        selected_exchange = overrides.get("exchange")
        if selected_exchange and symbols_by_exchange.get(selected_exchange):
            if symbol not in symbols_by_exchange[selected_exchange]:
                raise ValueError(f"symbol is not configured for account: {symbol}")
        overrides["symbol"] = symbol
    elif "exchange" in overrides and symbols_by_exchange.get(overrides["exchange"]):
        overrides["symbol"] = symbols_by_exchange[overrides["exchange"]][0]

    if "depth_shape" in payload:
        depth_shape = str(payload["depth_shape"]).strip().lower()
        if depth_shape not in {"flat", "linear"}:
            raise ValueError("depth_shape must be flat or linear")
        overrides["depth_shape"] = depth_shape

    if "levels" in payload:
        overrides["levels"] = _non_negative_int(payload, "levels")
        if overrides["levels"] <= 0:
            raise ValueError("levels must be positive")

    positive_float_fields = {
        "price_band_pct",
        "quote_per_level",
        "poll_seconds",
    }
    for field in positive_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)
            if overrides[field] <= 0:
                raise ValueError(f"{field} must be positive")

    non_negative_float_fields = {
        "min_order_quote",
        "min_distance_bps",
        "reprice_threshold_bps",
        "max_order_book_gap_bps",
        "inventory_target_base",
        "inventory_band_base",
        "inventory_max_deviation_base",
    }
    for field in non_negative_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)

    return overrides


def market_maker_config_from_payload(
    payload: dict[str, Any],
    *,
    base_config: MarketMakerConfig | None = None,
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> MarketMakerConfig:
    overrides = _market_maker_overrides_from_payload(
        payload,
        allowed_exchanges=allowed_exchanges,
        symbols_by_exchange=symbols_by_exchange,
    )
    config = replace(base_config or MarketMakerConfig(), **overrides)
    return market_maker_config_with_id(config)


def market_maker_configs_from_payload(
    payload: Any,
    *,
    base_configs: Iterable[MarketMakerConfig] | None = None,
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> list[MarketMakerConfig]:
    if not isinstance(payload, list):
        raise ValueError("market_maker instances must be a list")
    base_by_id = {
        market_maker_instance_id(config): market_maker_config_with_id(config)
        for config in (base_configs or [])
    }
    configs: list[MarketMakerConfig] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each market maker instance must be an object")
        instance_id = str(item.get("id") or "").strip()
        base_config = base_by_id.get(instance_id) if instance_id else None
        configs.append(
            market_maker_config_from_payload(
                item,
                base_config=base_config,
                allowed_exchanges=allowed_exchanges,
                symbols_by_exchange=symbols_by_exchange,
            )
        )
    return market_maker_configs_with_ids(configs)


def _spot_grid_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    overrides = _account_symbol_overrides_from_payload(
        payload,
        allowed_exchanges=allowed_exchanges,
        symbols_by_exchange=symbols_by_exchange,
    )

    for field in {"enabled", "live_enabled", "auto_rebuild", "post_only"}:
        if field in payload:
            if not isinstance(payload[field], bool):
                raise ValueError(f"{field} must be a boolean")
            overrides[field] = payload[field]

    if "spacing" in payload:
        spacing = str(payload["spacing"]).strip().lower()
        if spacing not in {"arithmetic", "geometric"}:
            raise ValueError("spacing must be arithmetic or geometric")
        overrides["spacing"] = spacing

    if "grid_count" in payload:
        overrides["grid_count"] = _non_negative_int(payload, "grid_count")
        if overrides["grid_count"] <= 0:
            raise ValueError("grid_count must be positive")

    if "max_open_orders" in payload:
        overrides["max_open_orders"] = _non_negative_int(payload, "max_open_orders")
        if overrides["max_open_orders"] <= 0:
            raise ValueError("max_open_orders must be positive")

    if "cancel_retry_attempts" in payload:
        overrides["cancel_retry_attempts"] = _non_negative_int(
            payload,
            "cancel_retry_attempts",
        )

    positive_float_fields = {
        "lower_price",
        "upper_price",
        "quote_per_grid",
    }
    for field in positive_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)
            if overrides[field] <= 0:
                raise ValueError(f"{field} must be positive")

    non_negative_float_fields = {
        "take_profit_price",
        "stop_loss_price",
        "max_position_base",
        "min_grid_step_bps",
    }
    for field in non_negative_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)

    lower = overrides.get("lower_price")
    upper = overrides.get("upper_price")
    if lower is not None and upper is not None and upper <= lower:
        raise ValueError("upper_price must be greater than lower_price")

    return overrides


def _dca_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    overrides = _account_symbol_overrides_from_payload(
        payload,
        allowed_exchanges=allowed_exchanges,
        symbols_by_exchange=symbols_by_exchange,
    )

    for field in {"enabled", "live_enabled"}:
        if field in payload:
            if not isinstance(payload[field], bool):
                raise ValueError(f"{field} must be a boolean")
            overrides[field] = payload[field]

    if "side" in payload:
        side = str(payload["side"]).strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        overrides["side"] = side

    if "price_mode" in payload:
        price_mode = str(payload["price_mode"]).strip().lower()
        if price_mode not in {"taker", "maker"}:
            raise ValueError("price_mode must be taker or maker")
        overrides["price_mode"] = price_mode

    if "max_orders" in payload:
        overrides["max_orders"] = _non_negative_int(payload, "max_orders")
        if overrides["max_orders"] <= 0:
            raise ValueError("max_orders must be positive")

    positive_float_fields = {
        "interval_seconds",
        "quote_per_order",
        "size_multiplier",
    }
    for field in positive_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)
            if overrides[field] <= 0:
                raise ValueError(f"{field} must be positive")
    if "size_multiplier" in overrides and overrides["size_multiplier"] < 1:
        raise ValueError("size_multiplier must be greater than or equal to 1")

    non_negative_float_fields = {
        "trigger_price",
        "average_entry_price",
        "take_profit_price",
        "max_position_base",
        "max_loss_quote",
        "price_offset_bps",
    }
    for field in non_negative_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)

    return overrides


def _execution_algo_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    overrides = _account_symbol_overrides_from_payload(
        payload,
        allowed_exchanges=allowed_exchanges,
        symbols_by_exchange=symbols_by_exchange,
    )

    for field in {"enabled", "live_enabled"}:
        if field in payload:
            if not isinstance(payload[field], bool):
                raise ValueError(f"{field} must be a boolean")
            overrides[field] = payload[field]

    if "side" in payload:
        side = str(payload["side"]).strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        overrides["side"] = side

    if "algo" in payload:
        algo = str(payload["algo"]).strip().lower()
        if algo not in {"twap", "vwap", "pov"}:
            raise ValueError("algo must be twap, vwap, or pov")
        overrides["algo"] = algo

    if "price_mode" in payload:
        price_mode = str(payload["price_mode"]).strip().lower()
        if price_mode not in {"taker", "maker"}:
            raise ValueError("price_mode must be taker or maker")
        overrides["price_mode"] = price_mode

    if "slice_count" in payload:
        overrides["slice_count"] = _non_negative_int(payload, "slice_count")
        if overrides["slice_count"] <= 0:
            raise ValueError("slice_count must be positive")

    positive_float_fields = {
        "duration_seconds",
        "interval_seconds",
        "volume_lookback_seconds",
    }
    for field in positive_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)
            if overrides[field] <= 0:
                raise ValueError(f"{field} must be positive")

    non_negative_float_fields = {
        "total_base",
        "total_quote",
        "participation_rate",
        "min_slice_quote",
        "max_slice_quote",
        "price_offset_bps",
        "start_price",
        "stop_price",
        "max_slippage_bps",
    }
    for field in non_negative_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)

    if "participation_rate" in overrides and overrides["participation_rate"] > 1:
        raise ValueError("participation_rate must be between 0 and 1")
    min_slice = overrides.get("min_slice_quote")
    max_slice = overrides.get("max_slice_quote")
    if min_slice is not None and max_slice is not None and max_slice > 0:
        if min_slice > max_slice:
            raise ValueError("min_slice_quote must be less than or equal to max_slice_quote")

    return overrides


def _backtest_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
    symbols_by_exchange: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    overrides = _account_symbol_overrides_from_payload(
        payload,
        allowed_exchanges=allowed_exchanges,
        symbols_by_exchange=symbols_by_exchange,
    )

    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        overrides["enabled"] = payload["enabled"]

    if "strategy" in payload:
        strategy = str(payload["strategy"]).strip().lower()
        if strategy not in {"spot_grid", "dca", "execution_algo"}:
            raise ValueError("strategy must be spot_grid, dca, or execution_algo")
        overrides["strategy"] = strategy

    if "data_source" in payload:
        data_source = str(payload["data_source"]).strip().lower()
        if data_source not in {"synthetic"}:
            raise ValueError("data_source must be synthetic")
        overrides["data_source"] = data_source

    if "step_count" in payload:
        overrides["step_count"] = _non_negative_int(payload, "step_count")
        if overrides["step_count"] < 2:
            raise ValueError("step_count must be at least 2")

    if "max_recent_points" in payload:
        overrides["max_recent_points"] = _non_negative_int(
            payload,
            "max_recent_points",
        )
        if overrides["max_recent_points"] <= 0:
            raise ValueError("max_recent_points must be positive")

    if "depth_levels" in payload:
        overrides["depth_levels"] = _non_negative_int(payload, "depth_levels")
        if overrides["depth_levels"] <= 0:
            raise ValueError("depth_levels must be positive")

    if "latency_steps" in payload:
        overrides["latency_steps"] = _non_negative_int(payload, "latency_steps")

    non_negative_float_fields = {
        "initial_cash",
        "initial_base",
        "fee_bps",
        "slippage_bps",
        "price_start",
        "price_end",
        "volatility_bps",
        "depth_quote_per_level",
        "depth_step_bps",
    }
    for field in non_negative_float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)
    if "trend_bps" in payload:
        try:
            overrides["trend_bps"] = float(payload["trend_bps"])
        except (TypeError, ValueError) as exc:
            raise ValueError("trend_bps must be a number") from exc

    return overrides


def spot_market_to_dict(market: SpotMarketConfig) -> dict[str, Any]:
    return {
        "asset": market.asset,
        "exchange": market.exchange,
        "symbol": market.symbol,
        "quote_currency": market.quote_currency,
    }


def spot_markets_to_list(markets: Iterable[SpotMarketConfig]) -> list[dict[str, Any]]:
    return [spot_market_to_dict(market) for market in markets]


def cash_and_carry_pair_to_dict(pair: CashAndCarryPair) -> dict[str, Any]:
    return {
        "spot_symbol": pair.spot_symbol,
        "derivative_symbol": pair.derivative_symbol,
    }


def cash_and_carry_pairs_to_list(
    pairs: Iterable[CashAndCarryPair],
) -> list[dict[str, Any]]:
    return [cash_and_carry_pair_to_dict(pair) for pair in pairs]


def exchange_configs_to_list(
    exchanges: Iterable[ExchangeConfig],
) -> list[dict[str, Any]]:
    return [
        {
            "key": exchange.key,
            "label": exchange.label or exchange.key,
            "id": exchange.id,
            "market_type": exchange.market_type,
        }
        for exchange in exchanges
    ]


def _spot_markets_from_payload(
    payload: dict[str, Any],
    *,
    allowed_exchanges: set[str],
) -> list[SpotMarketConfig]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    raw_markets = payload.get("spot_markets")
    if raw_markets is None:
        raw_markets = payload.get("markets")
    if not isinstance(raw_markets, list):
        raise ValueError("spot_markets must be a list")

    markets: list[SpotMarketConfig] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_markets, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"spot_markets[{index}] must be an object")
        asset = str(item.get("asset", "")).strip().upper()
        exchange = str(item.get("exchange", "")).strip()
        symbol = str(item.get("symbol", "")).strip().upper()
        quote_currency = str(item.get("quote_currency", "")).strip().upper()
        if not asset:
            raise ValueError(f"spot_markets[{index}].asset is required")
        if not exchange:
            raise ValueError(f"spot_markets[{index}].exchange is required")
        if exchange not in allowed_exchanges:
            raise ValueError(f"unknown exchange account: {exchange}")
        if not symbol or "/" not in symbol:
            raise ValueError(f"spot_markets[{index}].symbol must look like BASE/QUOTE")
        inferred_quote = symbol.split("/", 1)[1].upper()
        if not quote_currency:
            quote_currency = inferred_quote
        if quote_currency != inferred_quote:
            raise ValueError(
                f"spot_markets[{index}].quote_currency must match symbol quote"
            )
        key = (exchange, symbol)
        if key in seen:
            raise ValueError(f"duplicate spot market: {exchange} {symbol}")
        seen.add(key)
        markets.append(
            SpotMarketConfig(
                asset=asset,
                exchange=exchange,
                symbol=symbol,
                quote_currency=quote_currency,
            )
        )
    return markets


def _cash_and_carry_pairs_from_payload(
    payload: dict[str, Any],
) -> list[CashAndCarryPair]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    raw_pairs = payload.get("cash_and_carry_pairs")
    if raw_pairs is None:
        raw_pairs = payload.get("pairs")
    if not isinstance(raw_pairs, list):
        raise ValueError("cash_and_carry_pairs must be a list")

    pairs: list[CashAndCarryPair] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_pairs, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"cash_and_carry_pairs[{index}] must be an object")
        spot_symbol = str(item.get("spot_symbol", "")).strip().upper()
        derivative_symbol = str(item.get("derivative_symbol", "")).strip().upper()
        if not spot_symbol or "/" not in spot_symbol:
            raise ValueError(
                f"cash_and_carry_pairs[{index}].spot_symbol must look like BASE/QUOTE"
            )
        if not derivative_symbol or "/" not in derivative_symbol:
            raise ValueError(
                f"cash_and_carry_pairs[{index}].derivative_symbol must look like BASE/QUOTE"
            )
        spot_base, _ = _symbol_base_quote(spot_symbol)
        derivative_base, _ = _symbol_base_quote(derivative_symbol)
        if spot_base != derivative_base:
            raise ValueError(
                f"cash_and_carry_pairs[{index}] spot and contract base must match"
            )
        key = (spot_symbol, derivative_symbol)
        if key in seen:
            raise ValueError(
                f"duplicate cash & carry pair: {spot_symbol} {derivative_symbol}"
            )
        seen.add(key)
        pairs.append(
            CashAndCarryPair(
                spot_symbol=spot_symbol,
                derivative_symbol=derivative_symbol,
            )
        )
    return pairs


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    quote = quote.partition(":")[0]
    return base.upper(), quote.upper()


def _non_negative_float(payload: dict[str, Any], field: str) -> float:
    try:
        value = float(payload[field])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def _non_negative_int(payload: dict[str, Any], field: str) -> int:
    value = _non_negative_float(payload, field)
    if not value.is_integer():
        raise ValueError(f"{field} must be an integer")
    return int(value)


def _bool_map_from_payload(
    payload: dict[str, Any],
    field: str,
    *,
    allowed_keys: set[str],
    label: str,
) -> dict[str, bool] | None:
    if field not in payload:
        return None
    raw = payload[field]
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be an object")
    clean: dict[str, bool] = {}
    for key, value in raw.items():
        clean_key = str(key).strip()
        if clean_key not in allowed_keys:
            raise ValueError(f"unknown {label}: {clean_key}")
        if not isinstance(value, bool):
            raise ValueError(f"{field}.{clean_key} must be a boolean")
        clean[clean_key] = value
    return clean


def _risk_overrides_from_payload(
    payload: dict[str, Any],
    *,
    allowed_accounts: set[str],
    allowed_strategies: set[str],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    overrides: dict[str, Any] = {}
    if "allow_live_trading" in payload:
        if not isinstance(payload["allow_live_trading"], bool):
            raise ValueError("allow_live_trading must be a boolean")
        overrides["allow_live_trading"] = payload["allow_live_trading"]

    account_enabled = _bool_map_from_payload(
        payload,
        "account_enabled",
        allowed_keys=allowed_accounts,
        label="exchange account",
    )
    if account_enabled is not None:
        overrides["account_enabled"] = account_enabled

    strategy_enabled = _bool_map_from_payload(
        payload,
        "strategy_enabled",
        allowed_keys=allowed_strategies,
        label="strategy",
    )
    if strategy_enabled is not None:
        overrides["strategy_enabled"] = strategy_enabled

    float_fields = {
        "max_order_quote",
        "max_cycle_quote",
        "max_exposure_quote",
        "max_daily_loss_quote",
        "min_seconds_between_cancels",
        "min_order_book_depth_quote",
        "max_slippage_bps",
        "max_order_book_age_seconds",
        "max_order_book_gap_bps",
        "max_price_jump_bps",
        "max_derivative_leverage",
        "min_liquidation_buffer_pct",
        "max_margin_usage_pct",
    }
    for field in float_fields:
        if field in payload:
            overrides[field] = _non_negative_float(payload, field)

    int_fields = {
        "max_orders_per_cycle",
        "max_open_orders",
        "max_cancels_per_cycle",
    }
    for field in int_fields:
        if field in payload:
            overrides[field] = _non_negative_int(payload, field)

    return overrides
