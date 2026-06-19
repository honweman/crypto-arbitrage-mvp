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
    allowed_exchanges: list[str] = field(default_factory=list)
    blocked_exchanges: list[str] = field(default_factory=list)
    allowed_symbols: list[str] = field(default_factory=list)
    blocked_symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TradeLogConfig:
    enabled: bool = True
    path: str = "data/trade_events.jsonl"
    max_recent_events: int = 50


@dataclass(frozen=True)
class StrategyTimelineConfig:
    enabled: bool = True
    path: str = "data/strategy_timeline.jsonl"
    max_recent_events: int = 100


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
    registration_code_env: str | None = "CRYPTO_ARB_WEB_REGISTRATION_CODE"
    totp_issuer: str = "Crypto Trading Dashboard"


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
    spot_grid: SpotGridConfig = field(default_factory=SpotGridConfig)
    dca: DcaConfig = field(default_factory=DcaConfig)
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
    slow_execution_raw = raw.get("slow_execution", {})
    spot_grid_raw = raw.get("spot_grid", {})
    dca_raw = raw.get("dca", {})
    portfolio_raw = raw.get("portfolio", {})
    risk_raw = raw.get("risk", {})
    trade_log_raw = raw.get("trade_log", {})
    strategy_timeline_raw = raw.get("strategy_timeline", {})
    pnl_store_raw = raw.get("pnl_store", {})
    alerts_raw = raw.get("alerts", {})
    web_security_raw = raw.get("web_security", {})
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
        market_maker=MarketMakerConfig(
            enabled=bool(market_maker_raw.get("enabled", False)),
            live_enabled=bool(market_maker_raw.get("live_enabled", False)),
            exchange=market_maker_raw.get("exchange", ""),
            symbol=market_maker_raw.get("symbol", ""),
            levels=int(market_maker_raw.get("levels", 10)),
            price_band_pct=float(market_maker_raw.get("price_band_pct", 10.0)),
            quote_per_level=float(market_maker_raw.get("quote_per_level", 1.0)),
            depth_shape=str(market_maker_raw.get("depth_shape", "linear")).lower(),
            min_order_quote=float(market_maker_raw.get("min_order_quote", 0.0)),
            min_distance_bps=float(market_maker_raw.get("min_distance_bps", 0.0)),
            reprice_threshold_bps=float(
                market_maker_raw.get("reprice_threshold_bps", 0.0)
            ),
            poll_seconds=float(market_maker_raw.get("poll_seconds", 1.0)),
            post_only=bool(market_maker_raw.get("post_only", True)),
            cancel_existing_orders=bool(
                market_maker_raw.get("cancel_existing_orders", False)
            ),
            client_order_prefix=market_maker_raw.get(
                "client_order_prefix",
                "crypto-arb-mm",
            ),
            inventory_control_enabled=bool(
                market_maker_raw.get("inventory_control_enabled", False)
            ),
            inventory_target_base=float(
                market_maker_raw.get("inventory_target_base", 0.0)
            ),
            inventory_band_base=float(
                market_maker_raw.get("inventory_band_base", 0.0)
            ),
            inventory_max_deviation_base=float(
                market_maker_raw.get("inventory_max_deviation_base", 0.0)
            ),
        ),
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
        spot_exchanges=[
            _exchange_from_dict(item) for item in raw.get("spot_exchanges", [])
        ],
        derivative_exchanges=[
            _exchange_from_dict(item) for item in raw.get("derivative_exchanges", [])
        ],
        risk=RiskConfig(
            enabled=bool(risk_raw.get("enabled", True)),
            trading_enabled=bool(risk_raw.get("trading_enabled", True)),
            allow_live_trading=bool(risk_raw.get("allow_live_trading", False)),
            allow_market_maker=bool(risk_raw.get("allow_market_maker", True)),
            allow_slow_execution=bool(risk_raw.get("allow_slow_execution", True)),
            strategy_enabled=_bool_dict(risk_raw.get("strategy_enabled", {})),
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
            allowed_exchanges=_string_list(risk_raw.get("allowed_exchanges", [])),
            blocked_exchanges=_string_list(risk_raw.get("blocked_exchanges", [])),
            allowed_symbols=_string_list(risk_raw.get("allowed_symbols", [])),
            blocked_symbols=_string_list(risk_raw.get("blocked_symbols", [])),
        ),
        trade_log=TradeLogConfig(
            enabled=bool(trade_log_raw.get("enabled", True)),
            path=str(trade_log_raw.get("path", "data/trade_events.jsonl")),
            max_recent_events=int(trade_log_raw.get("max_recent_events", 50)),
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
            registration_code_env=web_security_raw.get(
                "registration_code_env",
                "CRYPTO_ARB_WEB_REGISTRATION_CODE",
            ),
            totp_issuer=web_security_raw.get(
                "totp_issuer",
                "Crypto Trading Dashboard",
            ),
        ),
    )
