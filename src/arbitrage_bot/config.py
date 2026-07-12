from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExchangeConfig:
    id: str
    label: str | None = None
    market_type: str = "spot"
    fee_bps: float = 0.0
    api_key_env: str | None = None
    secret_env: str | None = None
    password_env: str | None = None
    http_proxy_env: str | None = None
    https_proxy_env: str | None = None
    socks_proxy_env: str | None = None
    ws_proxy_env: str | None = None
    wss_proxy_env: str | None = None
    ws_socks_proxy_env: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.label or f"{self.id}:{self.market_type}"


@dataclass(frozen=True)
class CashAndCarryPair:
    spot_symbol: str
    derivative_symbol: str


@dataclass(frozen=True)
class TriangleRouteConfig:
    exchange: str
    start_currency: str
    symbols: list[str]
    label: str = ""


@dataclass(frozen=True)
class SpotMarketConfig:
    asset: str
    exchange: str
    symbol: str
    quote_currency: str


@dataclass(frozen=True)
class QuoteRateSource:
    exchange: str
    symbol: str
    base_currency: str
    quote_currency: str
    base_to_common_rate: float = 1.0


@dataclass(frozen=True)
class OnchainMonitorConfig:
    enabled: bool = False
    network: str = "solana"
    rpc_url: str = "https://solana-rpc.publicnode.com"
    rpc_urls: list[str] = field(default_factory=list)
    rpc_url_env: str | None = "SOLANA_RPC_URLS"
    token_mint: str = ""
    label: str = "Token"
    top_n: int = 20
    poll_seconds: float = 60.0
    history_path: str = "data/onchain_holder_changes.json"
    address_labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketMakerConfig:
    id: str = ""
    enabled: bool = False
    live_enabled: bool = False
    exchange: str = ""
    symbol: str = ""
    levels: int = 10
    price_band_pct: float = 10.0
    quote_per_level: float = 1.0
    depth_shape: str = "linear"
    min_order_quote: float = 0.0
    min_distance_bps: float = 0.0
    reprice_threshold_bps: float = 0.0
    max_order_quote: float = 0.0
    max_cycle_quote: float = 0.0
    max_open_orders: int = 0
    max_cancels_per_cycle: int = 0
    max_slippage_bps: float = 0.0
    max_order_book_gap_bps: float = 0.0
    max_order_book_age_seconds: float = 0.0
    poll_seconds: float = 1.0
    post_only: bool = True
    cancel_existing_orders: bool = False
    client_order_prefix: str = "crypto-arb-mm"
    inventory_control_enabled: bool = False
    inventory_target_base: float = 0.0
    inventory_band_base: float = 0.0
    inventory_max_deviation_base: float = 0.0


@dataclass(frozen=True)
class SlowExecutionConfig:
    enabled: bool = False
    exchange: str = ""
    symbol: str = ""
    side: str = "sell"
    total_base: float = 0.0
    total_quote: float = 0.0
    unlimited_total: bool = False
    slice_mode: str = "configured"
    slice_base: float = 0.0
    slice_base_min: float = 0.0
    slice_base_max: float = 0.0
    slice_quote: float = 0.0
    randomize_slice: bool = False
    interval_seconds: float = 60.0
    order_ttl_seconds: float = 0.0
    start_price: float = 0.0
    stop_price: float = 0.0
    price_mode: str = "taker"
    price_offset_bps: float = 0.0
    min_order_quote: float = 0.0
    post_only: bool = False
    cancel_existing_orders: bool = False
    client_order_prefix: str = "crypto-arb-slow"
    block_conflicting_market_maker: bool = True


@dataclass(frozen=True)
class CrossExchangeRebalanceConfig:
    enabled: bool = False
    live_enabled: bool = False
    buy_exchange: str = ""
    buy_symbol: str = ""
    sell_exchange: str = ""
    sell_symbol: str = ""
    total_quote_common: float = 0.0
    quote_per_cycle_common: float = 0.0
    interval_seconds: float = 30.0
    order_ttl_seconds: float = 2.0
    max_cost_bps: float = 50.0
    max_slippage_bps: float = 50.0
    buy_quote_reserve: float = 0.0
    sell_base_reserve: float = 0.0
    block_conflicting_open_orders: bool = True
    halt_on_error: bool = True
    client_order_prefix: str = "crypto-arb-rebalance"
    runtime_path: str = "data/cross_exchange_rebalance_runtime.json"


@dataclass(frozen=True)
class SpotGridConfig:
    enabled: bool = False
    live_enabled: bool = False
    exchange: str = ""
    symbol: str = ""
    lower_price: float = 0.0
    upper_price: float = 0.0
    grid_count: int = 10
    spacing: str = "arithmetic"
    quote_per_grid: float = 1.0
    take_profit_price: float = 0.0
    stop_loss_price: float = 0.0
    auto_rebuild: bool = False
    max_position_base: float = 0.0
    max_open_orders: int = 20
    min_grid_step_bps: float = 10.0
    cancel_retry_attempts: int = 3
    post_only: bool = True
    client_order_prefix: str = "crypto-arb-grid"
    runtime_path: str = "data/spot_grid_runtime.json"


@dataclass(frozen=True)
class DcaConfig:
    enabled: bool = False
    live_enabled: bool = False
    exchange: str = ""
    symbol: str = ""
    side: str = "buy"
    trigger_price: float = 0.0
    interval_seconds: float = 3600.0
    quote_per_order: float = 1.0
    size_multiplier: float = 1.0
    max_orders: int = 10
    average_entry_price: float = 0.0
    take_profit_price: float = 0.0
    max_position_base: float = 0.0
    max_loss_quote: float = 0.0
    price_mode: str = "taker"
    price_offset_bps: float = 0.0
    client_order_prefix: str = "crypto-arb-dca"


@dataclass(frozen=True)
class ExecutionAlgoConfig:
    enabled: bool = False
    live_enabled: bool = False
    exchange: str = ""
    symbol: str = ""
    side: str = "buy"
    algo: str = "twap"
    total_base: float = 0.0
    total_quote: float = 0.0
    duration_seconds: float = 3600.0
    slice_count: int = 12
    interval_seconds: float = 300.0
    participation_rate: float = 0.05
    volume_lookback_seconds: float = 300.0
    min_slice_quote: float = 0.0
    max_slice_quote: float = 0.0
    price_mode: str = "taker"
    price_offset_bps: float = 0.0
    start_price: float = 0.0
    stop_price: float = 0.0
    max_slippage_bps: float = 50.0
    client_order_prefix: str = "crypto-arb-exec"


@dataclass(frozen=True)
class BacktestConfig:
    enabled: bool = False
    strategy: str = "spot_grid"
    exchange: str = ""
    symbol: str = ""
    initial_cash: float = 1000.0
    initial_base: float = 0.0
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    price_start: float = 0.0
    price_end: float = 0.0
    step_count: int = 200
    volatility_bps: float = 50.0
    trend_bps: float = 0.0
    max_recent_points: int = 80
    data_source: str = "synthetic"
    history_path: str = ""
    depth_simulation_enabled: bool = False
    depth_quote_per_level: float = 0.0
    depth_step_bps: float = 5.0
    depth_levels: int = 5
    latency_steps: int = 0


@dataclass(frozen=True)
class OptionComboConfig:
    underlying: str
    spot_exchange: str
    spot_symbol: str
    option_exchange: str
    call_symbol: str
    put_symbol: str
    strike: float
    expiry: str = ""
    contract_size: float = 1.0
    quote_currency: str = "USDT"


@dataclass(frozen=True)
class OptionsArbitrageConfig:
    enabled: bool = False
    notional_quote: float = 1000.0
    min_edge_quote: float = 0.0
    min_edge_bps: float = 10.0
    max_contracts: float = 0.0
    max_days_to_expiry: float = 0.0
    risk_free_rate_bps: float = 0.0
    borrow_rate_bps: float = 0.0
    min_option_depth_quote: float = 0.0
    max_option_spread_bps: float = 0.0
    min_days_to_expiry_open: float = 0.0
    expiry_reminder_days: float = 0.0


@dataclass(frozen=True)
class ContractStrategiesConfig:
    enabled: bool = True
    funding_bot_enabled: bool = True
    basis_bot_enabled: bool = True
    futures_grid_enabled: bool = False
    hedge_rebalancer_enabled: bool = False
    live_enabled: bool = False
    spot_exchange: str = ""
    spot_symbol: str = ""
    derivative_exchange: str = ""
    derivative_symbol: str = ""
    notional_quote: float = 100.0
    funding_min_bps: float = 0.0
    basis_entry_bps: float = 0.0
    basis_exit_bps: float = 0.0
    futures_grid_levels: int = 6
    futures_grid_band_pct: float = 2.0
    futures_grid_quote_per_level: float = 5.0
    futures_grid_max_leverage: float = 1.0
    hedge_threshold_base: float = 0.0
    hedge_max_quote: float = 0.0
    post_only: bool = True
    client_order_prefix: str = "crypto-arb-contract"


@dataclass(frozen=True)
class TriangularArbitrageConfig:
    enabled: bool = False
    notional_quote: float = 1000.0
    min_profit_quote: float = 0.0
    min_profit_bps: float = 5.0
    routes: list[TriangleRouteConfig] = field(default_factory=list)


@dataclass(frozen=True)
class StrategyCenterConfig:
    enabled: bool = True
    path: str = "data/strategy_center.sqlite3"
    max_recent_signals: int = 100


@dataclass(frozen=True)
class AssetPosition:
    asset: str
    position_base: float = 0.0
    average_entry_price: float = 0.0


@dataclass(frozen=True)
class PortfolioConfig:
    enabled: bool = False
    asset: str = ""
    position_base: float = 0.0
    average_entry_price: float = 0.0
    positions: list[AssetPosition] = field(default_factory=list)
    cash_balances: dict[str, float] = field(default_factory=dict)
    realized_pnl: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool = True
    trading_enabled: bool = True
    allow_live_trading: bool = False
    allow_market_maker: bool = True
    allow_slow_execution: bool = True
    strategy_enabled: dict[str, bool] = field(default_factory=dict)
    strategy_overrides: dict[str, dict[str, float | int]] = field(
        default_factory=dict
    )
    account_enabled: dict[str, bool] = field(default_factory=dict)
    require_post_only: bool = True
    max_order_quote: float = 5.0
    max_cycle_quote: float = 25.0
    max_position_base: float = 0.0
    max_position_base_by_asset: dict[str, float] = field(default_factory=dict)
    max_exposure_quote: float = 0.0
    max_exposure_quote_by_asset: dict[str, float] = field(default_factory=dict)
    max_daily_loss_quote: float = 0.0
    max_orders_per_cycle: int = 30
    max_open_orders: int = 50
    max_cancels_per_cycle: int = 50
    min_seconds_between_cancels: float = 0.0
    max_existing_spread_bps: float = 2500.0
    max_price_distance_bps: float = 1500.0
    max_slippage_bps: float = 50.0
    min_order_book_depth_quote: float = 0.0
    max_order_book_gap_bps: float = 2000.0
    max_price_jump_bps: float = 1000.0
    max_plan_age_seconds: float = 5.0
    max_order_book_age_seconds: float = 10.0
    require_order_book_timestamp: bool = False
    max_derivative_leverage: float = 0.0
    min_liquidation_buffer_pct: float = 0.0
    max_margin_usage_pct: float = 0.0
    allowed_exchanges: list[str] = field(default_factory=list)
    blocked_exchanges: list[str] = field(default_factory=list)
    allowed_symbols: list[str] = field(default_factory=list)
    blocked_symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TradeLogConfig:
    enabled: bool = True
    path: str = "data/trade_events.jsonl"
    max_recent_events: int = 50
    rotate_max_bytes: int = 64 * 1024 * 1024
    rotate_keep_files: int = 8
    rotate_compress: bool = True


@dataclass(frozen=True)
class StrategyTimelineConfig:
    enabled: bool = True
    path: str = "data/strategy_timeline.jsonl"
    max_recent_events: int = 100
    rotate_max_bytes: int = 64 * 1024 * 1024
    rotate_keep_files: int = 8
    rotate_compress: bool = True


@dataclass(frozen=True)
class PnlStoreConfig:
    enabled: bool = False
    path: str = "data/fill_pnl.sqlite3"


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = False
    min_level: str = "warning"
    webhook_url_env: str | None = None
    telegram_bot_token_env: str | None = None
    telegram_chat_id_env: str | None = None
    email_from_env: str | None = None
    email_to_env: str | None = None
    smtp_host_env: str | None = None
    smtp_port_env: str | None = None
    smtp_username_env: str | None = None
    smtp_password_env: str | None = None
    smtp_tls: bool = True
    auto_stop_enabled: bool = False
    auto_stop_consecutive_errors: int = 3
    daily_report_enabled: bool = False
    daily_report_time: str = "23:59"


@dataclass(frozen=True)
class WebSecurityConfig:
    password_env: str | None = "CRYPTO_ARB_WEB_PASSWORD"
    cookie_secret_env: str | None = "CRYPTO_ARB_WEB_COOKIE_SECRET"
    allowed_ips_env: str | None = "CRYPTO_ARB_WEB_ALLOWED_IPS"
    trust_proxy_headers: bool = True
    cookie_secure: bool = True
    user_store_path: str = "data/web_users.json"
    registration_enabled: bool = False
    bootstrap_admin_email_env: str | None = "CRYPTO_ARB_WEB_ADMIN_EMAIL"
    registration_code_env: str | None = "CRYPTO_ARB_WEB_REGISTRATION_CODE"
    totp_issuer: str = "Crypto Trading Dashboard"
    verification_code_ttl_seconds: int = 600
    verification_resend_seconds: int = 60
    verification_max_attempts: int = 5
    user_workspace_path: str = "data/user_workspace.sqlite3"
    credential_master_key_env: str | None = "CRYPTO_ARB_CREDENTIAL_MASTER_KEY"


@dataclass(frozen=True)
class BotConfig:
    poll_seconds: float
    order_book_depth: int
    notional_quote: float
    min_profit_quote: float
    min_profit_bps: float
    min_basis_bps: float
    common_quote_currency: str
    quote_rates: dict[str, float]
    quote_rate_sources: list[QuoteRateSource]
    onchain_monitor: OnchainMonitorConfig
    market_maker: MarketMakerConfig
    slow_execution: SlowExecutionConfig
    portfolio: PortfolioConfig
    spot_symbols: list[str]
    spot_markets: list[SpotMarketConfig]
    cash_and_carry_pairs: list[CashAndCarryPair]
    spot_exchanges: list[ExchangeConfig]
    derivative_exchanges: list[ExchangeConfig]
    option_combos: list[OptionComboConfig] = field(default_factory=list)
    market_makers: list[MarketMakerConfig] = field(default_factory=list)
    cross_exchange_rebalance: CrossExchangeRebalanceConfig = field(
        default_factory=CrossExchangeRebalanceConfig
    )
    spot_grid: SpotGridConfig = field(default_factory=SpotGridConfig)
    dca: DcaConfig = field(default_factory=DcaConfig)
    execution_algo: ExecutionAlgoConfig = field(default_factory=ExecutionAlgoConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    options_arbitrage: OptionsArbitrageConfig = field(
        default_factory=OptionsArbitrageConfig
    )
    contract_strategies: ContractStrategiesConfig = field(
        default_factory=ContractStrategiesConfig
    )
    triangular_arbitrage: TriangularArbitrageConfig = field(
        default_factory=TriangularArbitrageConfig
    )
    strategy_center: StrategyCenterConfig = field(default_factory=StrategyCenterConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trade_log: TradeLogConfig = field(default_factory=TradeLogConfig)
    strategy_timeline: StrategyTimelineConfig = field(
        default_factory=StrategyTimelineConfig
    )
    pnl_store: PnlStoreConfig = field(default_factory=PnlStoreConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    web_security: WebSecurityConfig = field(default_factory=WebSecurityConfig)


def _exchange_from_dict(raw: dict[str, Any]) -> ExchangeConfig:
    return ExchangeConfig(
        id=raw["id"],
        label=raw.get("label"),
        market_type=raw.get("market_type", "spot"),
        fee_bps=float(raw.get("fee_bps", 0.0)),
        api_key_env=raw.get("api_key_env"),
        secret_env=raw.get("secret_env"),
        password_env=raw.get("password_env"),
        http_proxy_env=raw.get("http_proxy_env"),
        https_proxy_env=raw.get("https_proxy_env"),
        socks_proxy_env=raw.get("socks_proxy_env"),
        ws_proxy_env=raw.get("ws_proxy_env"),
        wss_proxy_env=raw.get("wss_proxy_env"),
        ws_socks_proxy_env=raw.get("ws_socks_proxy_env"),
        options=dict(raw.get("options", {})),
    )


def _asset_position_from_dict(raw: dict[str, Any]) -> AssetPosition:
    return AssetPosition(
        asset=str(raw["asset"]).upper(),
        position_base=float(raw.get("position_base", 0.0)),
        average_entry_price=float(raw.get("average_entry_price", 0.0)),
    )


def _option_combo_from_dict(raw: dict[str, Any]) -> OptionComboConfig:
    return OptionComboConfig(
        underlying=str(raw["underlying"]).upper(),
        spot_exchange=str(raw["spot_exchange"]),
        spot_symbol=str(raw["spot_symbol"]).upper(),
        option_exchange=str(raw["option_exchange"]),
        call_symbol=str(raw["call_symbol"]),
        put_symbol=str(raw["put_symbol"]),
        strike=float(raw["strike"]),
        expiry=str(raw.get("expiry", "")),
        contract_size=float(raw.get("contract_size", 1.0)),
        quote_currency=str(raw.get("quote_currency", "USDT")).upper(),
    )


def _market_maker_from_dict(raw: dict[str, Any]) -> MarketMakerConfig:
    return MarketMakerConfig(
        id=str(raw.get("id", "")).strip(),
        enabled=bool(raw.get("enabled", False)),
        live_enabled=bool(raw.get("live_enabled", False)),
        exchange=raw.get("exchange", ""),
        symbol=raw.get("symbol", ""),
        levels=int(raw.get("levels", 10)),
        price_band_pct=float(raw.get("price_band_pct", 10.0)),
        quote_per_level=float(raw.get("quote_per_level", 1.0)),
        depth_shape=str(raw.get("depth_shape", "linear")).lower(),
        min_order_quote=float(raw.get("min_order_quote", 0.0)),
        min_distance_bps=float(raw.get("min_distance_bps", 0.0)),
        reprice_threshold_bps=float(raw.get("reprice_threshold_bps", 0.0)),
        max_order_quote=float(raw.get("max_order_quote", 0.0)),
        max_cycle_quote=float(raw.get("max_cycle_quote", 0.0)),
        max_open_orders=int(raw.get("max_open_orders", 0)),
        max_cancels_per_cycle=int(raw.get("max_cancels_per_cycle", 0)),
        max_slippage_bps=float(raw.get("max_slippage_bps", 0.0)),
        max_order_book_gap_bps=float(raw.get("max_order_book_gap_bps", 0.0)),
        max_order_book_age_seconds=float(
            raw.get("max_order_book_age_seconds", 0.0)
        ),
        poll_seconds=float(raw.get("poll_seconds", 1.0)),
        post_only=bool(raw.get("post_only", True)),
        cancel_existing_orders=bool(raw.get("cancel_existing_orders", False)),
        client_order_prefix=raw.get("client_order_prefix", "crypto-arb-mm"),
        inventory_control_enabled=bool(
            raw.get("inventory_control_enabled", False)
        ),
        inventory_target_base=float(raw.get("inventory_target_base", 0.0)),
        inventory_band_base=float(raw.get("inventory_band_base", 0.0)),
        inventory_max_deviation_base=float(
            raw.get("inventory_max_deviation_base", 0.0)
        ),
    )


def _triangle_route_from_dict(raw: dict[str, Any]) -> TriangleRouteConfig:
    return TriangleRouteConfig(
        exchange=str(raw["exchange"]),
        start_currency=str(raw.get("start_currency", "USDT")).upper(),
        symbols=[str(symbol).upper() for symbol in raw.get("symbols", [])],
        label=str(raw.get("label", "")),
    )


def _portfolio_positions_from_dict(raw: dict[str, Any]) -> list[AssetPosition]:
    positions_raw = raw.get("positions")
    if positions_raw is not None:
        return [_asset_position_from_dict(item) for item in positions_raw]

    legacy_asset = str(raw.get("asset", "")).upper()
    if not legacy_asset and "position_base" not in raw and "average_entry_price" not in raw:
        return []

    return [
        AssetPosition(
            asset=legacy_asset,
            position_base=float(raw.get("position_base", 0.0)),
            average_entry_price=float(raw.get("average_entry_price", 0.0)),
        )
    ]


def _string_list(raw: Any) -> list[str]:
    return [str(item) for item in raw or []]


def _bool_dict(raw: Any) -> dict[str, bool]:
    return {str(key): bool(value) for key, value in (raw or {}).items()}


def _float_dict(raw: Any) -> dict[str, float]:
    return {str(key).upper(): float(value) for key, value in (raw or {}).items()}


_RISK_STRATEGY_OVERRIDE_FLOAT_FIELDS = {
    "max_order_quote",
    "max_cycle_quote",
    "max_exposure_quote",
    "max_daily_loss_quote",
    "min_seconds_between_cancels",
    "max_existing_spread_bps",
    "max_price_distance_bps",
    "max_slippage_bps",
    "min_order_book_depth_quote",
    "max_order_book_gap_bps",
    "max_price_jump_bps",
    "max_plan_age_seconds",
    "max_order_book_age_seconds",
}

_RISK_STRATEGY_OVERRIDE_INT_FIELDS = {
    "max_orders_per_cycle",
    "max_open_orders",
    "max_cancels_per_cycle",
}


def _risk_strategy_overrides_from_dict(raw: Any) -> dict[str, dict[str, float | int]]:
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, dict[str, float | int]] = {}
    allowed_fields = (
        _RISK_STRATEGY_OVERRIDE_FLOAT_FIELDS | _RISK_STRATEGY_OVERRIDE_INT_FIELDS
    )
    for strategy, values in raw.items():
        strategy_name = str(strategy).strip()
        if not strategy_name or not isinstance(values, dict):
            continue
        strategy_values: dict[str, float | int] = {}
        for field_name, value in values.items():
            field = str(field_name).strip()
            if field not in allowed_fields:
                continue
            if field in _RISK_STRATEGY_OVERRIDE_INT_FIELDS:
                parsed = int(float(value))
                if parsed >= 0:
                    strategy_values[field] = parsed
            else:
                parsed = float(value)
                if parsed >= 0:
                    strategy_values[field] = parsed
        if strategy_values:
            clean[strategy_name] = strategy_values
    return clean


def _normalize_rpc_urls(
    *,
    rpc_url: str | None,
    rpc_urls: Any,
    rpc_url_env: str | None,
    default_url: str,
) -> list[str]:
    candidates: list[str] = []
    if rpc_url_env:
        env_value = os.environ.get(rpc_url_env)
        if env_value:
            candidates.extend(part.strip() for part in env_value.split(","))
    if rpc_url:
        candidates.append(rpc_url)
    if isinstance(rpc_urls, str):
        candidates.extend(part.strip() for part in rpc_urls.split(","))
    elif isinstance(rpc_urls, list):
        candidates.extend(str(item).strip() for item in rpc_urls)
    candidates.append(default_url)

    urls: list[str] = []
    seen: set[str] = set()
    for raw_url in candidates:
        url = str(raw_url or "").strip()
        if not url or "://" not in url or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls or [default_url]


def load_config(path: str | Path) -> BotConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    onchain_raw = raw.get("onchain_monitor", {})
    default_solana_rpc = "https://solana-rpc.publicnode.com"
    onchain_rpc_url_env = onchain_raw.get("rpc_url_env", "SOLANA_RPC_URLS")
    onchain_rpc_urls = _normalize_rpc_urls(
        rpc_url=onchain_raw.get("rpc_url", default_solana_rpc),
        rpc_urls=onchain_raw.get("rpc_urls", []),
        rpc_url_env=onchain_rpc_url_env,
        default_url=default_solana_rpc,
    )
    market_maker_raw = raw.get("market_maker", {})
    market_makers_raw = raw.get("market_makers", [])
    slow_execution_raw = raw.get("slow_execution", {})
    cross_exchange_rebalance_raw = raw.get("cross_exchange_rebalance", {})
    spot_grid_raw = raw.get("spot_grid", {})
    dca_raw = raw.get("dca", {})
    execution_algo_raw = raw.get("execution_algo", {})
    backtest_raw = raw.get("backtest", {})
    options_arbitrage_raw = raw.get("options_arbitrage", {})
    contract_strategies_raw = raw.get("contract_strategies", {})
    triangular_arbitrage_raw = raw.get("triangular_arbitrage", {})
    strategy_center_raw = raw.get("strategy_center", {})
    portfolio_raw = raw.get("portfolio", {})
    risk_raw = raw.get("risk", {})
    trade_log_raw = raw.get("trade_log", {})
    strategy_timeline_raw = raw.get("strategy_timeline", {})
    pnl_store_raw = raw.get("pnl_store", {})
    alerts_raw = raw.get("alerts", {})
    web_security_raw = raw.get("web_security", {})
    market_maker_config = _market_maker_from_dict(market_maker_raw)
    market_maker_configs = [
        _market_maker_from_dict(item)
        for item in market_makers_raw
        if isinstance(item, dict)
    ] or [market_maker_config]

    return BotConfig(
        poll_seconds=float(raw.get("poll_seconds", 10)),
        order_book_depth=int(raw.get("order_book_depth", 20)),
        notional_quote=float(raw.get("notional_quote", 1000)),
        min_profit_quote=float(raw.get("min_profit_quote", 0)),
        min_profit_bps=float(raw.get("min_profit_bps", 0)),
        min_basis_bps=float(raw.get("min_basis_bps", 0)),
        common_quote_currency=raw.get("common_quote_currency", "USD"),
        quote_rates={
            key.upper(): float(value)
            for key, value in raw.get(
                "quote_rates",
                {
                    "USD": 1.0,
                    "USDT": 1.0,
                    "USDC": 1.0,
                },
            ).items()
        },
        quote_rate_sources=[
            QuoteRateSource(
                exchange=item["exchange"],
                symbol=item["symbol"],
                base_currency=item["base_currency"].upper(),
                quote_currency=item["quote_currency"].upper(),
                base_to_common_rate=float(item.get("base_to_common_rate", 1.0)),
            )
            for item in raw.get("quote_rate_sources", [])
        ],
        onchain_monitor=OnchainMonitorConfig(
            enabled=bool(onchain_raw.get("enabled", False)),
            network=onchain_raw.get("network", "solana"),
            rpc_url=onchain_rpc_urls[0],
            rpc_urls=onchain_rpc_urls,
            rpc_url_env=onchain_rpc_url_env,
            token_mint=onchain_raw.get("token_mint", ""),
            label=onchain_raw.get("label", "Token"),
            top_n=int(onchain_raw.get("top_n", 20)),
            poll_seconds=float(onchain_raw.get("poll_seconds", 60.0)),
            history_path=onchain_raw.get(
                "history_path",
                "data/onchain_holder_changes.json",
            ),
            address_labels={
                str(address): str(label)
                for address, label in onchain_raw.get("address_labels", {}).items()
            },
        ),
        market_maker=market_maker_config,
        market_makers=market_maker_configs,
        slow_execution=SlowExecutionConfig(
            enabled=bool(slow_execution_raw.get("enabled", False)),
            exchange=slow_execution_raw.get("exchange", ""),
            symbol=slow_execution_raw.get("symbol", ""),
            side=slow_execution_raw.get("side", "sell").lower(),
            total_base=float(slow_execution_raw.get("total_base", 0.0)),
            total_quote=float(slow_execution_raw.get("total_quote", 0.0)),
            unlimited_total=bool(slow_execution_raw.get("unlimited_total", False)),
            slice_mode=slow_execution_raw.get("slice_mode", "configured").lower(),
            slice_base=float(slow_execution_raw.get("slice_base", 0.0)),
            slice_base_min=float(slow_execution_raw.get("slice_base_min", 0.0)),
            slice_base_max=float(slow_execution_raw.get("slice_base_max", 0.0)),
            slice_quote=float(slow_execution_raw.get("slice_quote", 0.0)),
            randomize_slice=bool(
                slow_execution_raw.get("randomize_slice", False)
            ),
            interval_seconds=float(
                slow_execution_raw.get("interval_seconds", 60.0)
            ),
            order_ttl_seconds=float(
                slow_execution_raw.get("order_ttl_seconds", 0.0)
            ),
            start_price=float(slow_execution_raw.get("start_price", 0.0)),
            stop_price=float(slow_execution_raw.get("stop_price", 0.0)),
            price_mode=slow_execution_raw.get("price_mode", "taker").lower(),
            price_offset_bps=float(slow_execution_raw.get("price_offset_bps", 0.0)),
            min_order_quote=float(slow_execution_raw.get("min_order_quote", 0.0)),
            post_only=bool(slow_execution_raw.get("post_only", False)),
            cancel_existing_orders=bool(
                slow_execution_raw.get("cancel_existing_orders", False)
            ),
            client_order_prefix=slow_execution_raw.get(
                "client_order_prefix",
                "crypto-arb-slow",
            ),
            block_conflicting_market_maker=bool(
                slow_execution_raw.get("block_conflicting_market_maker", True)
            ),
        ),
        cross_exchange_rebalance=CrossExchangeRebalanceConfig(
            enabled=bool(cross_exchange_rebalance_raw.get("enabled", False)),
            live_enabled=bool(cross_exchange_rebalance_raw.get("live_enabled", False)),
            buy_exchange=str(cross_exchange_rebalance_raw.get("buy_exchange", "")),
            buy_symbol=str(cross_exchange_rebalance_raw.get("buy_symbol", "")),
            sell_exchange=str(cross_exchange_rebalance_raw.get("sell_exchange", "")),
            sell_symbol=str(cross_exchange_rebalance_raw.get("sell_symbol", "")),
            total_quote_common=float(
                cross_exchange_rebalance_raw.get("total_quote_common", 0.0)
            ),
            quote_per_cycle_common=float(
                cross_exchange_rebalance_raw.get(
                    "quote_per_cycle_common",
                    0.0,
                )
            ),
            interval_seconds=float(
                cross_exchange_rebalance_raw.get("interval_seconds", 30.0)
            ),
            order_ttl_seconds=float(
                cross_exchange_rebalance_raw.get("order_ttl_seconds", 2.0)
            ),
            max_cost_bps=float(cross_exchange_rebalance_raw.get("max_cost_bps", 50.0)),
            max_slippage_bps=float(
                cross_exchange_rebalance_raw.get("max_slippage_bps", 50.0)
            ),
            buy_quote_reserve=float(
                cross_exchange_rebalance_raw.get("buy_quote_reserve", 0.0)
            ),
            sell_base_reserve=float(
                cross_exchange_rebalance_raw.get("sell_base_reserve", 0.0)
            ),
            block_conflicting_open_orders=bool(
                cross_exchange_rebalance_raw.get(
                    "block_conflicting_open_orders",
                    True,
                )
            ),
            halt_on_error=bool(cross_exchange_rebalance_raw.get("halt_on_error", True)),
            client_order_prefix=str(
                cross_exchange_rebalance_raw.get(
                    "client_order_prefix",
                    "crypto-arb-rebalance",
                )
            ),
            runtime_path=str(
                cross_exchange_rebalance_raw.get(
                    "runtime_path",
                    "data/cross_exchange_rebalance_runtime.json",
                )
            ),
        ),
        spot_grid=SpotGridConfig(
            enabled=bool(spot_grid_raw.get("enabled", False)),
            live_enabled=bool(spot_grid_raw.get("live_enabled", False)),
            exchange=spot_grid_raw.get("exchange", ""),
            symbol=spot_grid_raw.get("symbol", ""),
            lower_price=float(spot_grid_raw.get("lower_price", 0.0)),
            upper_price=float(spot_grid_raw.get("upper_price", 0.0)),
            grid_count=int(spot_grid_raw.get("grid_count", 10)),
            spacing=str(spot_grid_raw.get("spacing", "arithmetic")).lower(),
            quote_per_grid=float(spot_grid_raw.get("quote_per_grid", 1.0)),
            take_profit_price=float(spot_grid_raw.get("take_profit_price", 0.0)),
            stop_loss_price=float(spot_grid_raw.get("stop_loss_price", 0.0)),
            auto_rebuild=bool(spot_grid_raw.get("auto_rebuild", False)),
            max_position_base=float(spot_grid_raw.get("max_position_base", 0.0)),
            max_open_orders=int(spot_grid_raw.get("max_open_orders", 20)),
            min_grid_step_bps=float(spot_grid_raw.get("min_grid_step_bps", 10.0)),
            cancel_retry_attempts=int(
                spot_grid_raw.get("cancel_retry_attempts", 3)
            ),
            post_only=bool(spot_grid_raw.get("post_only", True)),
            client_order_prefix=spot_grid_raw.get(
                "client_order_prefix",
                "crypto-arb-grid",
            ),
            runtime_path=str(
                spot_grid_raw.get("runtime_path", "data/spot_grid_runtime.json")
            ),
        ),
        dca=DcaConfig(
            enabled=bool(dca_raw.get("enabled", False)),
            live_enabled=bool(dca_raw.get("live_enabled", False)),
            exchange=dca_raw.get("exchange", ""),
            symbol=dca_raw.get("symbol", ""),
            side=str(dca_raw.get("side", "buy")).lower(),
            trigger_price=float(dca_raw.get("trigger_price", 0.0)),
            interval_seconds=float(dca_raw.get("interval_seconds", 3600.0)),
            quote_per_order=float(dca_raw.get("quote_per_order", 1.0)),
            size_multiplier=float(dca_raw.get("size_multiplier", 1.0)),
            max_orders=int(dca_raw.get("max_orders", 10)),
            average_entry_price=float(dca_raw.get("average_entry_price", 0.0)),
            take_profit_price=float(dca_raw.get("take_profit_price", 0.0)),
            max_position_base=float(dca_raw.get("max_position_base", 0.0)),
            max_loss_quote=float(dca_raw.get("max_loss_quote", 0.0)),
            price_mode=str(dca_raw.get("price_mode", "taker")).lower(),
            price_offset_bps=float(dca_raw.get("price_offset_bps", 0.0)),
            client_order_prefix=dca_raw.get(
                "client_order_prefix",
                "crypto-arb-dca",
            ),
        ),
        execution_algo=ExecutionAlgoConfig(
            enabled=bool(execution_algo_raw.get("enabled", False)),
            live_enabled=bool(execution_algo_raw.get("live_enabled", False)),
            exchange=execution_algo_raw.get("exchange", ""),
            symbol=execution_algo_raw.get("symbol", ""),
            side=str(execution_algo_raw.get("side", "buy")).lower(),
            algo=str(execution_algo_raw.get("algo", "twap")).lower(),
            total_base=float(execution_algo_raw.get("total_base", 0.0)),
            total_quote=float(execution_algo_raw.get("total_quote", 0.0)),
            duration_seconds=float(
                execution_algo_raw.get("duration_seconds", 3600.0)
            ),
            slice_count=int(execution_algo_raw.get("slice_count", 12)),
            interval_seconds=float(
                execution_algo_raw.get("interval_seconds", 300.0)
            ),
            participation_rate=float(
                execution_algo_raw.get("participation_rate", 0.05)
            ),
            volume_lookback_seconds=float(
                execution_algo_raw.get("volume_lookback_seconds", 300.0)
            ),
            min_slice_quote=float(
                execution_algo_raw.get("min_slice_quote", 0.0)
            ),
            max_slice_quote=float(
                execution_algo_raw.get("max_slice_quote", 0.0)
            ),
            price_mode=str(execution_algo_raw.get("price_mode", "taker")).lower(),
            price_offset_bps=float(
                execution_algo_raw.get("price_offset_bps", 0.0)
            ),
            start_price=float(execution_algo_raw.get("start_price", 0.0)),
            stop_price=float(execution_algo_raw.get("stop_price", 0.0)),
            max_slippage_bps=float(
                execution_algo_raw.get("max_slippage_bps", 50.0)
            ),
            client_order_prefix=execution_algo_raw.get(
                "client_order_prefix",
                "crypto-arb-exec",
            ),
        ),
        backtest=BacktestConfig(
            enabled=bool(backtest_raw.get("enabled", False)),
            strategy=str(backtest_raw.get("strategy", "spot_grid")).lower(),
            exchange=backtest_raw.get("exchange", ""),
            symbol=backtest_raw.get("symbol", ""),
            initial_cash=float(backtest_raw.get("initial_cash", 1000.0)),
            initial_base=float(backtest_raw.get("initial_base", 0.0)),
            fee_bps=float(backtest_raw.get("fee_bps", 10.0)),
            slippage_bps=float(backtest_raw.get("slippage_bps", 5.0)),
            price_start=float(backtest_raw.get("price_start", 0.0)),
            price_end=float(backtest_raw.get("price_end", 0.0)),
            step_count=int(backtest_raw.get("step_count", 200)),
            volatility_bps=float(backtest_raw.get("volatility_bps", 50.0)),
            trend_bps=float(backtest_raw.get("trend_bps", 0.0)),
            max_recent_points=int(backtest_raw.get("max_recent_points", 80)),
            data_source=str(backtest_raw.get("data_source", "synthetic")).lower(),
            history_path=backtest_raw.get("history_path", ""),
            depth_simulation_enabled=bool(
                backtest_raw.get("depth_simulation_enabled", False)
            ),
            depth_quote_per_level=float(
                backtest_raw.get("depth_quote_per_level", 0.0)
            ),
            depth_step_bps=float(backtest_raw.get("depth_step_bps", 5.0)),
            depth_levels=int(backtest_raw.get("depth_levels", 5)),
            latency_steps=int(backtest_raw.get("latency_steps", 0)),
        ),
        strategy_center=StrategyCenterConfig(
            enabled=bool(strategy_center_raw.get("enabled", True)),
            path=str(strategy_center_raw.get("path", "data/strategy_center.sqlite3")),
            max_recent_signals=int(
                strategy_center_raw.get("max_recent_signals", 100)
            ),
        ),
        portfolio=PortfolioConfig(
            enabled=bool(portfolio_raw.get("enabled", False)),
            asset=portfolio_raw.get("asset", "").upper(),
            position_base=float(portfolio_raw.get("position_base", 0.0)),
            average_entry_price=float(
                portfolio_raw.get("average_entry_price", 0.0)
            ),
            positions=_portfolio_positions_from_dict(portfolio_raw),
            cash_balances={
                str(currency).upper(): float(value)
                for currency, value in portfolio_raw.get(
                    "cash_balances",
                    {},
                ).items()
            },
            realized_pnl={
                str(source): float(value)
                for source, value in portfolio_raw.get("realized_pnl", {}).items()
            },
        ),
        spot_symbols=list(raw.get("spot_symbols", [])),
        spot_markets=[
            SpotMarketConfig(
                asset=item["asset"].upper(),
                exchange=item["exchange"],
                symbol=item["symbol"],
                quote_currency=item["quote_currency"].upper(),
            )
            for item in raw.get("spot_markets", [])
        ],
        cash_and_carry_pairs=[
            CashAndCarryPair(
                spot_symbol=item["spot_symbol"],
                derivative_symbol=item["derivative_symbol"],
            )
            for item in raw.get("cash_and_carry_pairs", [])
        ],
        option_combos=[
            _option_combo_from_dict(item)
            for item in raw.get("option_combos", [])
        ],
        spot_exchanges=[
            _exchange_from_dict(item) for item in raw.get("spot_exchanges", [])
        ],
        derivative_exchanges=[
            _exchange_from_dict(item) for item in raw.get("derivative_exchanges", [])
        ],
        options_arbitrage=OptionsArbitrageConfig(
            enabled=bool(options_arbitrage_raw.get("enabled", False)),
            notional_quote=float(
                options_arbitrage_raw.get(
                    "notional_quote",
                    raw.get("notional_quote", 1000.0),
                )
            ),
            min_edge_quote=float(
                options_arbitrage_raw.get(
                    "min_edge_quote",
                    raw.get("min_profit_quote", 0.0),
                )
            ),
            min_edge_bps=float(options_arbitrage_raw.get("min_edge_bps", 10.0)),
            max_contracts=float(options_arbitrage_raw.get("max_contracts", 0.0)),
            max_days_to_expiry=float(
                options_arbitrage_raw.get("max_days_to_expiry", 0.0)
            ),
            risk_free_rate_bps=float(
                options_arbitrage_raw.get("risk_free_rate_bps", 0.0)
            ),
            borrow_rate_bps=float(
                options_arbitrage_raw.get("borrow_rate_bps", 0.0)
            ),
            min_option_depth_quote=float(
                options_arbitrage_raw.get("min_option_depth_quote", 0.0)
            ),
            max_option_spread_bps=float(
                options_arbitrage_raw.get("max_option_spread_bps", 0.0)
            ),
            min_days_to_expiry_open=float(
                options_arbitrage_raw.get("min_days_to_expiry_open", 0.0)
            ),
            expiry_reminder_days=float(
                options_arbitrage_raw.get("expiry_reminder_days", 0.0)
            ),
        ),
        contract_strategies=ContractStrategiesConfig(
            enabled=bool(contract_strategies_raw.get("enabled", True)),
            funding_bot_enabled=bool(
                contract_strategies_raw.get("funding_bot_enabled", True)
            ),
            basis_bot_enabled=bool(
                contract_strategies_raw.get("basis_bot_enabled", True)
            ),
            futures_grid_enabled=bool(
                contract_strategies_raw.get("futures_grid_enabled", False)
            ),
            hedge_rebalancer_enabled=bool(
                contract_strategies_raw.get("hedge_rebalancer_enabled", False)
            ),
            live_enabled=bool(contract_strategies_raw.get("live_enabled", False)),
            spot_exchange=str(contract_strategies_raw.get("spot_exchange", "")),
            spot_symbol=str(contract_strategies_raw.get("spot_symbol", "")),
            derivative_exchange=str(
                contract_strategies_raw.get("derivative_exchange", "")
            ),
            derivative_symbol=str(
                contract_strategies_raw.get("derivative_symbol", "")
            ),
            notional_quote=float(
                contract_strategies_raw.get(
                    "notional_quote",
                    raw.get("notional_quote", 100.0),
                )
            ),
            funding_min_bps=float(
                contract_strategies_raw.get("funding_min_bps", 0.0)
            ),
            basis_entry_bps=float(
                contract_strategies_raw.get(
                    "basis_entry_bps",
                    raw.get("min_basis_bps", 0.0),
                )
            ),
            basis_exit_bps=float(
                contract_strategies_raw.get("basis_exit_bps", 0.0)
            ),
            futures_grid_levels=int(
                contract_strategies_raw.get("futures_grid_levels", 6)
            ),
            futures_grid_band_pct=float(
                contract_strategies_raw.get("futures_grid_band_pct", 2.0)
            ),
            futures_grid_quote_per_level=float(
                contract_strategies_raw.get("futures_grid_quote_per_level", 5.0)
            ),
            futures_grid_max_leverage=float(
                contract_strategies_raw.get("futures_grid_max_leverage", 1.0)
            ),
            hedge_threshold_base=float(
                contract_strategies_raw.get("hedge_threshold_base", 0.0)
            ),
            hedge_max_quote=float(
                contract_strategies_raw.get("hedge_max_quote", 0.0)
            ),
            post_only=bool(contract_strategies_raw.get("post_only", True)),
            client_order_prefix=str(
                contract_strategies_raw.get(
                    "client_order_prefix",
                    "crypto-arb-contract",
                )
            ),
        ),
        triangular_arbitrage=TriangularArbitrageConfig(
            enabled=bool(triangular_arbitrage_raw.get("enabled", False)),
            notional_quote=float(
                triangular_arbitrage_raw.get(
                    "notional_quote",
                    raw.get("notional_quote", 1000.0),
                )
            ),
            min_profit_quote=float(
                triangular_arbitrage_raw.get(
                    "min_profit_quote",
                    raw.get("min_profit_quote", 0.0),
                )
            ),
            min_profit_bps=float(
                triangular_arbitrage_raw.get(
                    "min_profit_bps",
                    raw.get("min_profit_bps", 5.0),
                )
            ),
            routes=[
                _triangle_route_from_dict(item)
                for item in triangular_arbitrage_raw.get("routes", [])
            ],
        ),
        risk=RiskConfig(
            enabled=bool(risk_raw.get("enabled", True)),
            trading_enabled=bool(risk_raw.get("trading_enabled", True)),
            allow_live_trading=bool(risk_raw.get("allow_live_trading", False)),
            allow_market_maker=bool(risk_raw.get("allow_market_maker", True)),
            allow_slow_execution=bool(risk_raw.get("allow_slow_execution", True)),
            strategy_enabled=_bool_dict(risk_raw.get("strategy_enabled", {})),
            strategy_overrides=_risk_strategy_overrides_from_dict(
                risk_raw.get("strategy_overrides", {})
            ),
            account_enabled=_bool_dict(risk_raw.get("account_enabled", {})),
            require_post_only=bool(risk_raw.get("require_post_only", True)),
            max_order_quote=float(risk_raw.get("max_order_quote", 5.0)),
            max_cycle_quote=float(risk_raw.get("max_cycle_quote", 25.0)),
            max_position_base=float(risk_raw.get("max_position_base", 0.0)),
            max_position_base_by_asset=_float_dict(
                risk_raw.get("max_position_base_by_asset", {})
            ),
            max_exposure_quote=float(risk_raw.get("max_exposure_quote", 0.0)),
            max_exposure_quote_by_asset=_float_dict(
                risk_raw.get("max_exposure_quote_by_asset", {})
            ),
            max_daily_loss_quote=float(risk_raw.get("max_daily_loss_quote", 0.0)),
            max_orders_per_cycle=int(risk_raw.get("max_orders_per_cycle", 30)),
            max_open_orders=int(risk_raw.get("max_open_orders", 50)),
            max_cancels_per_cycle=int(risk_raw.get("max_cancels_per_cycle", 50)),
            min_seconds_between_cancels=float(
                risk_raw.get("min_seconds_between_cancels", 0.0)
            ),
            max_existing_spread_bps=float(
                risk_raw.get("max_existing_spread_bps", 2500.0)
            ),
            max_price_distance_bps=float(
                risk_raw.get("max_price_distance_bps", 1500.0)
            ),
            max_slippage_bps=float(risk_raw.get("max_slippage_bps", 50.0)),
            min_order_book_depth_quote=float(
                risk_raw.get("min_order_book_depth_quote", 0.0)
            ),
            max_order_book_gap_bps=float(
                risk_raw.get("max_order_book_gap_bps", 2000.0)
            ),
            max_price_jump_bps=float(risk_raw.get("max_price_jump_bps", 1000.0)),
            max_plan_age_seconds=float(risk_raw.get("max_plan_age_seconds", 5.0)),
            max_order_book_age_seconds=float(
                risk_raw.get("max_order_book_age_seconds", 10.0)
            ),
            require_order_book_timestamp=bool(
                risk_raw.get("require_order_book_timestamp", False)
            ),
            max_derivative_leverage=float(
                risk_raw.get("max_derivative_leverage", 0.0)
            ),
            min_liquidation_buffer_pct=float(
                risk_raw.get("min_liquidation_buffer_pct", 0.0)
            ),
            max_margin_usage_pct=float(risk_raw.get("max_margin_usage_pct", 0.0)),
            allowed_exchanges=_string_list(risk_raw.get("allowed_exchanges", [])),
            blocked_exchanges=_string_list(risk_raw.get("blocked_exchanges", [])),
            allowed_symbols=_string_list(risk_raw.get("allowed_symbols", [])),
            blocked_symbols=_string_list(risk_raw.get("blocked_symbols", [])),
        ),
        trade_log=TradeLogConfig(
            enabled=bool(trade_log_raw.get("enabled", True)),
            path=str(trade_log_raw.get("path", "data/trade_events.jsonl")),
            max_recent_events=int(trade_log_raw.get("max_recent_events", 50)),
            rotate_max_bytes=int(
                trade_log_raw.get("rotate_max_bytes", 64 * 1024 * 1024)
            ),
            rotate_keep_files=int(trade_log_raw.get("rotate_keep_files", 8)),
            rotate_compress=bool(trade_log_raw.get("rotate_compress", True)),
        ),
        strategy_timeline=StrategyTimelineConfig(
            enabled=bool(strategy_timeline_raw.get("enabled", True)),
            path=str(
                strategy_timeline_raw.get(
                    "path",
                    "data/strategy_timeline.jsonl",
                )
            ),
            max_recent_events=int(
                strategy_timeline_raw.get("max_recent_events", 100)
            ),
            rotate_max_bytes=int(
                strategy_timeline_raw.get("rotate_max_bytes", 64 * 1024 * 1024)
            ),
            rotate_keep_files=int(
                strategy_timeline_raw.get("rotate_keep_files", 8)
            ),
            rotate_compress=bool(
                strategy_timeline_raw.get("rotate_compress", True)
            ),
        ),
        pnl_store=PnlStoreConfig(
            enabled=bool(pnl_store_raw.get("enabled", False)),
            path=str(pnl_store_raw.get("path", "data/fill_pnl.sqlite3")),
        ),
        alerts=AlertConfig(
            enabled=bool(alerts_raw.get("enabled", False)),
            min_level=str(alerts_raw.get("min_level", "warning")),
            webhook_url_env=alerts_raw.get("webhook_url_env"),
            telegram_bot_token_env=alerts_raw.get("telegram_bot_token_env"),
            telegram_chat_id_env=alerts_raw.get("telegram_chat_id_env"),
            email_from_env=alerts_raw.get("email_from_env"),
            email_to_env=alerts_raw.get("email_to_env"),
            smtp_host_env=alerts_raw.get("smtp_host_env"),
            smtp_port_env=alerts_raw.get("smtp_port_env"),
            smtp_username_env=alerts_raw.get("smtp_username_env"),
            smtp_password_env=alerts_raw.get("smtp_password_env"),
            smtp_tls=bool(alerts_raw.get("smtp_tls", True)),
            auto_stop_enabled=bool(alerts_raw.get("auto_stop_enabled", False)),
            auto_stop_consecutive_errors=int(
                alerts_raw.get("auto_stop_consecutive_errors", 3)
            ),
            daily_report_enabled=bool(alerts_raw.get("daily_report_enabled", False)),
            daily_report_time=str(alerts_raw.get("daily_report_time", "23:59")),
        ),
        web_security=WebSecurityConfig(
            password_env=web_security_raw.get(
                "password_env",
                "CRYPTO_ARB_WEB_PASSWORD",
            ),
            cookie_secret_env=web_security_raw.get(
                "cookie_secret_env",
                "CRYPTO_ARB_WEB_COOKIE_SECRET",
            ),
            allowed_ips_env=web_security_raw.get(
                "allowed_ips_env",
                "CRYPTO_ARB_WEB_ALLOWED_IPS",
            ),
            trust_proxy_headers=bool(
                web_security_raw.get("trust_proxy_headers", True)
            ),
            cookie_secure=bool(web_security_raw.get("cookie_secure", True)),
            user_store_path=web_security_raw.get(
                "user_store_path",
                "data/web_users.json",
            ),
            registration_enabled=bool(
                web_security_raw.get("registration_enabled", False)
            ),
            bootstrap_admin_email_env=web_security_raw.get(
                "bootstrap_admin_email_env",
                "CRYPTO_ARB_WEB_ADMIN_EMAIL",
            ),
            registration_code_env=web_security_raw.get(
                "registration_code_env",
                "CRYPTO_ARB_WEB_REGISTRATION_CODE",
            ),
            totp_issuer=web_security_raw.get(
                "totp_issuer",
                "Crypto Trading Dashboard",
            ),
            verification_code_ttl_seconds=max(
                60,
                int(web_security_raw.get("verification_code_ttl_seconds", 600)),
            ),
            verification_resend_seconds=max(
                10,
                int(web_security_raw.get("verification_resend_seconds", 60)),
            ),
            verification_max_attempts=max(
                1,
                int(web_security_raw.get("verification_max_attempts", 5)),
            ),
            user_workspace_path=str(
                web_security_raw.get(
                    "user_workspace_path",
                    "data/user_workspace.sqlite3",
                )
            ),
            credential_master_key_env=web_security_raw.get(
                "credential_master_key_env",
                "CRYPTO_ARB_CREDENTIAL_MASTER_KEY",
            ),
        ),
    )
