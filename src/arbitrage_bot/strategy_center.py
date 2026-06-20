from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STRATEGY_TYPES = {
    "market_maker",
    "auto_buy_sell",
    "spot_grid",
    "dca",
    "execution_algo",
    "backtest",
    "spot_spread",
    "cash_and_carry",
    "funding_arbitrage",
    "options_arbitrage",
    "signal_bot",
}
SIGNAL_SOURCES = {"tradingview", "custom"}
SIGNAL_ACTIONS = {"alert", "buy", "sell", "entry", "exit", "close"}
SIDE_VALUES = {"buy", "sell", ""}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SECRET_FIELD_RE = re.compile(
    r"(^|_)(api[_-]?key|secret|password|passphrase|token|private[_-]?key)($|_)",
    re.IGNORECASE,
)


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _clean_text(value: Any, *, max_length: int = 160) -> str:
    return str(value or "").strip()[:max_length]


def _clean_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _clean_asset(value: Any) -> str:
    if value is None:
        return ""
    return str(value or "").strip().upper()


def _asset_from_symbol(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].split(":", 1)[0].strip().upper()


def _clean_id(value: Any, *, prefix: str) -> str:
    raw = _clean_text(value, max_length=80)
    if not raw:
        return _new_id(prefix)
    if not ID_RE.match(raw):
        raise ValueError(f"{prefix} id contains unsupported characters")
    return raw


def _clean_env_name(value: Any) -> str:
    raw = _clean_text(value, max_length=80).upper()
    if raw and not ENV_NAME_RE.match(raw):
        raise ValueError(f"invalid environment variable name: {raw}")
    return raw


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        rows = re.split(r"[\s,]+", value)
    elif isinstance(value, list):
        rows = value
    else:
        rows = []
    result: list[str] = []
    seen: set[str] = set()
    for item in rows:
        text = _clean_text(item, max_length=40)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _clean_asset_list(value: Any) -> list[str]:
    assets: list[str] = []
    seen: set[str] = set()
    for item in _clean_string_list(value):
        asset = item.upper()
        if asset and asset not in seen:
            assets.append(asset)
            seen.add(asset)
    return assets


def _clean_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    _reject_secret_values(value)
    return json.loads(json.dumps(value, ensure_ascii=True))


def _reject_secret_values(value: Any, *, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}" if path else key_text
            if SECRET_FIELD_RE.search(key_text) and not key_text.endswith("_env"):
                raise ValueError(
                    f"{key_path} must not contain secret values; store environment variable names only"
                )
            _reject_secret_values(item, path=key_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_values(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class StrategyInstance:
    id: str
    name: str
    strategy_type: str
    owner_email: str = ""
    account_id: str = ""
    exchange: str = ""
    symbol: str = ""
    asset: str = ""
    enabled: bool = False
    live_enabled: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)
    risk_overrides: dict[str, Any] = field(default_factory=dict)
    status: str = "draft"
    last_reason: str = ""
    pnl_quote: float = 0.0
    open_order_count: int = 0
    last_signal_id: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StrategyInstance":
        if not isinstance(raw, dict):
            raise ValueError("strategy instance must be an object")
        _reject_secret_values(raw)
        strategy_type = _clean_text(raw.get("strategy_type") or raw.get("type")).lower()
        if strategy_type not in STRATEGY_TYPES:
            raise ValueError(f"unsupported strategy type: {strategy_type}")
        symbol = _clean_text(raw.get("symbol"), max_length=40)
        asset = _clean_asset(raw.get("asset")) or _asset_from_symbol(symbol)
        if symbol and "/" not in symbol:
            raise ValueError("strategy symbol must use BASE/QUOTE format")
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), prefix="strategy"),
            name=_clean_text(raw.get("name") or strategy_type, max_length=80),
            strategy_type=strategy_type,
            owner_email=_clean_email(raw.get("owner_email")),
            account_id=_clean_text(raw.get("account_id"), max_length=80),
            exchange=_clean_text(raw.get("exchange"), max_length=80),
            symbol=symbol,
            asset=asset,
            enabled=bool(raw.get("enabled", False)),
            live_enabled=bool(raw.get("live_enabled", False)),
            parameters=_clean_dict(raw.get("parameters", {})),
            risk_overrides=_clean_dict(raw.get("risk_overrides", {})),
            status=_clean_text(raw.get("status") or "draft", max_length=40),
            last_reason=_clean_text(raw.get("last_reason"), max_length=240),
            pnl_quote=float(raw.get("pnl_quote") or 0.0),
            open_order_count=int(raw.get("open_order_count") or 0),
            last_signal_id=_clean_text(raw.get("last_signal_id"), max_length=80),
            created_at=float(raw.get("created_at") or now),
            updated_at=float(raw.get("updated_at") or now),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "strategy_type": self.strategy_type,
            "owner_email": self.owner_email,
            "account_id": self.account_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "asset": self.asset,
            "enabled": self.enabled,
            "live_enabled": self.live_enabled,
            "parameters": self.parameters,
            "risk_overrides": self.risk_overrides,
            "status": self.status,
            "last_reason": self.last_reason,
            "pnl_quote": self.pnl_quote,
            "open_order_count": self.open_order_count,
            "last_signal_id": self.last_signal_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "strategy_type": self.strategy_type,
            "owner_email": self.owner_email,
            "account_id": self.account_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "asset": self.asset,
            "enabled": self.enabled,
            "live_enabled": self.live_enabled,
            "status": self.status,
            "last_reason": self.last_reason,
            "pnl_quote": self.pnl_quote,
            "open_order_count": self.open_order_count,
            "last_signal_id": self.last_signal_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class UserApiAccount:
    id: str
    owner_email: str
    label: str
    exchange: str
    market_type: str = "spot"
    asset_scope: list[str] = field(default_factory=list)
    api_key_env: str = ""
    secret_env: str = ""
    password_env: str = ""
    passphrase_env: str = ""
    proxy_env: str = ""
    enabled: bool = False
    ip_label: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserApiAccount":
        if not isinstance(raw, dict):
            raise ValueError("api account must be an object")
        _reject_secret_values(raw)
        exchange = _clean_text(raw.get("exchange"), max_length=80)
        if not exchange:
            raise ValueError("api account exchange is required")
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), prefix="account"),
            owner_email=_clean_email(raw.get("owner_email")),
            label=_clean_text(raw.get("label") or exchange, max_length=80),
            exchange=exchange,
            market_type=_clean_text(raw.get("market_type") or "spot", max_length=40),
            asset_scope=_clean_asset_list(raw.get("asset_scope", [])),
            api_key_env=_clean_env_name(raw.get("api_key_env")),
            secret_env=_clean_env_name(raw.get("secret_env")),
            password_env=_clean_env_name(raw.get("password_env")),
            passphrase_env=_clean_env_name(raw.get("passphrase_env")),
            proxy_env=_clean_env_name(raw.get("proxy_env")),
            enabled=bool(raw.get("enabled", False)),
            ip_label=_clean_text(raw.get("ip_label"), max_length=80),
            created_at=float(raw.get("created_at") or now),
            updated_at=float(raw.get("updated_at") or now),
        )

    def env_status(self) -> dict[str, Any]:
        fields = [
            ("api_key_env", self.api_key_env),
            ("secret_env", self.secret_env),
            ("password_env", self.password_env),
            ("passphrase_env", self.passphrase_env),
            ("proxy_env", self.proxy_env),
        ]
        configured = [
            {"field": field, "env": env_name, "set": bool(os.environ.get(env_name))}
            for field, env_name in fields
            if env_name
        ]
        required_env = {
            "api_key_env": self.api_key_env,
            "secret_env": self.secret_env,
        }
        missing_required_names = [
            field for field, env_name in required_env.items() if not env_name
        ]
        required_set = [
            bool(os.environ.get(env_name))
            for env_name in required_env.values()
            if env_name
        ]
        return {
            "configured": not missing_required_names
            and bool(required_set)
            and all(required_set),
            "fields": configured,
            "missing_env": [
                *missing_required_names,
                *[item["env"] for item in configured if not item["set"]],
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_email": self.owner_email,
            "label": self.label,
            "exchange": self.exchange,
            "market_type": self.market_type,
            "asset_scope": self.asset_scope,
            "api_key_env": self.api_key_env,
            "secret_env": self.secret_env,
            "password_env": self.password_env,
            "passphrase_env": self.passphrase_env,
            "proxy_env": self.proxy_env,
            "enabled": self.enabled,
            "ip_label": self.ip_label,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["auth"] = self.env_status()
        return payload


@dataclass(frozen=True)
class FundingArbitrageSettings:
    enabled: bool = False
    pair_id: str = ""
    spot_exchange: str = ""
    derivative_exchange: str = ""
    spot_symbol: str = ""
    derivative_symbol: str = ""
    prediction_source: str = "manual"
    predicted_funding_rate_bps: float = 0.0
    min_entry_basis_bps: float = 0.0
    max_entry_basis_bps: float = 0.0
    min_funding_bps: float = 0.0
    take_profit_bps: float = 0.0
    stop_loss_bps: float = 0.0
    max_margin_usage_pct: float = 50.0
    min_liquidation_buffer_pct: float = 20.0
    hedge_failure_action: str = "pause_and_alert"
    partial_fill_action: str = "hedge_residual"
    rebalance_threshold_bps: float = 25.0
    close_on_negative_funding: bool = True
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FundingArbitrageSettings":
        if not isinstance(raw, dict):
            raw = {}
        spot_symbol = _clean_text(raw.get("spot_symbol"), max_length=40)
        derivative_symbol = _clean_text(raw.get("derivative_symbol"), max_length=60)
        for label, symbol in (("spot_symbol", spot_symbol), ("derivative_symbol", derivative_symbol)):
            if symbol and "/" not in symbol:
                raise ValueError(f"{label} must use BASE/QUOTE format")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            pair_id=_clean_text(raw.get("pair_id"), max_length=80),
            spot_exchange=_clean_text(raw.get("spot_exchange"), max_length=80),
            derivative_exchange=_clean_text(raw.get("derivative_exchange"), max_length=80),
            spot_symbol=spot_symbol,
            derivative_symbol=derivative_symbol,
            prediction_source=_clean_text(raw.get("prediction_source") or "manual", max_length=40),
            predicted_funding_rate_bps=float(raw.get("predicted_funding_rate_bps") or 0.0),
            min_entry_basis_bps=float(raw.get("min_entry_basis_bps") or 0.0),
            max_entry_basis_bps=float(raw.get("max_entry_basis_bps") or 0.0),
            min_funding_bps=float(raw.get("min_funding_bps") or 0.0),
            take_profit_bps=float(raw.get("take_profit_bps") or 0.0),
            stop_loss_bps=float(raw.get("stop_loss_bps") or 0.0),
            max_margin_usage_pct=float(raw.get("max_margin_usage_pct") or 0.0),
            min_liquidation_buffer_pct=float(raw.get("min_liquidation_buffer_pct") or 0.0),
            hedge_failure_action=_clean_text(raw.get("hedge_failure_action") or "pause_and_alert", max_length=40),
            partial_fill_action=_clean_text(raw.get("partial_fill_action") or "hedge_residual", max_length=40),
            rebalance_threshold_bps=float(raw.get("rebalance_threshold_bps") or 0.0),
            close_on_negative_funding=bool(raw.get("close_on_negative_funding", True)),
            updated_at=float(raw.get("updated_at") or _now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "pair_id": self.pair_id,
            "spot_exchange": self.spot_exchange,
            "derivative_exchange": self.derivative_exchange,
            "spot_symbol": self.spot_symbol,
            "derivative_symbol": self.derivative_symbol,
            "prediction_source": self.prediction_source,
            "predicted_funding_rate_bps": self.predicted_funding_rate_bps,
            "min_entry_basis_bps": self.min_entry_basis_bps,
            "max_entry_basis_bps": self.max_entry_basis_bps,
            "min_funding_bps": self.min_funding_bps,
            "take_profit_bps": self.take_profit_bps,
            "stop_loss_bps": self.stop_loss_bps,
            "max_margin_usage_pct": self.max_margin_usage_pct,
            "min_liquidation_buffer_pct": self.min_liquidation_buffer_pct,
            "hedge_failure_action": self.hedge_failure_action,
            "partial_fill_action": self.partial_fill_action,
            "rebalance_threshold_bps": self.rebalance_threshold_bps,
            "close_on_negative_funding": self.close_on_negative_funding,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SignalBotSettings:
    enabled: bool = False
    webhook_secret_env: str = "SIGNAL_BOT_WEBHOOK_SECRET"
    allow_custom_webhook: bool = True
    max_signal_age_seconds: float = 60.0
    default_strategy_id: str = ""
    allowed_sources: list[str] = field(default_factory=lambda: ["tradingview", "custom"])
    dedupe_seconds: float = 300.0
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SignalBotSettings":
        if not isinstance(raw, dict):
            raw = {}
        allowed_sources = [
            source
            for source in _clean_string_list(raw.get("allowed_sources", ["tradingview", "custom"]))
            if source in SIGNAL_SOURCES
        ]
        return cls(
            enabled=bool(raw.get("enabled", False)),
            webhook_secret_env=_clean_env_name(raw.get("webhook_secret_env") or "SIGNAL_BOT_WEBHOOK_SECRET"),
            allow_custom_webhook=bool(raw.get("allow_custom_webhook", True)),
            max_signal_age_seconds=float(raw.get("max_signal_age_seconds") or 60.0),
            default_strategy_id=_clean_text(raw.get("default_strategy_id"), max_length=80),
            allowed_sources=allowed_sources or ["tradingview", "custom"],
            dedupe_seconds=float(raw.get("dedupe_seconds") or 300.0),
            updated_at=float(raw.get("updated_at") or _now()),
        )

    def to_dict(self) -> dict[str, Any]:
        secret_set = bool(os.environ.get(self.webhook_secret_env)) if self.webhook_secret_env else False
        return {
            "enabled": self.enabled,
            "webhook_secret_env": self.webhook_secret_env,
            "webhook_secret_set": secret_set,
            "allow_custom_webhook": self.allow_custom_webhook,
            "max_signal_age_seconds": self.max_signal_age_seconds,
            "default_strategy_id": self.default_strategy_id,
            "allowed_sources": self.allowed_sources,
            "dedupe_seconds": self.dedupe_seconds,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SignalEvent:
    id: str
    source: str
    strategy_id: str = ""
    symbol: str = ""
    side: str = ""
    action: str = "alert"
    price: float = 0.0
    amount: float = 0.0
    quote_notional: float = 0.0
    timeframe: str = ""
    message: str = ""
    status: str = "received"
    reason: str = ""
    received_at: float = field(default_factory=_now)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SignalEvent":
        if not isinstance(raw, dict):
            raise ValueError("signal event must be an object")
        _reject_secret_values(raw)
        source = _clean_text(raw.get("source") or "custom", max_length=40).lower()
        if source not in SIGNAL_SOURCES:
            source = "custom"
        side = _clean_text(raw.get("side"), max_length=12).lower()
        if side not in SIDE_VALUES:
            side = ""
        action = _clean_text(raw.get("action") or side or "alert", max_length=20).lower()
        if action not in SIGNAL_ACTIONS:
            action = "alert"
        return cls(
            id=_clean_text(raw.get("id"), max_length=80) or _new_id("signal"),
            source=source,
            strategy_id=_clean_text(raw.get("strategy_id"), max_length=80),
            symbol=_clean_text(raw.get("symbol"), max_length=60),
            side=side,
            action=action,
            price=float(raw.get("price") or 0.0),
            amount=float(raw.get("amount") or 0.0),
            quote_notional=float(raw.get("quote_notional") or 0.0),
            timeframe=_clean_text(raw.get("timeframe"), max_length=40),
            message=_clean_text(raw.get("message"), max_length=240),
            status=_clean_text(raw.get("status") or "received", max_length=40),
            reason=_clean_text(raw.get("reason"), max_length=240),
            received_at=float(raw.get("received_at") or _now()),
            raw=_clean_dict(raw.get("raw", {})),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        source: str,
        default_strategy_id: str = "",
        status: str = "received",
        reason: str = "",
    ) -> "SignalEvent":
        if not isinstance(payload, dict):
            raise ValueError("signal payload must be an object")
        _reject_secret_values(payload)
        normalized_source = _clean_text(source or payload.get("source") or "custom").lower()
        if normalized_source not in SIGNAL_SOURCES:
            raise ValueError(f"unsupported signal source: {normalized_source}")
        side = _clean_text(payload.get("side"), max_length=12).lower()
        if side not in SIDE_VALUES:
            raise ValueError("signal side must be buy or sell")
        action = _clean_text(payload.get("action") or side or "alert", max_length=20).lower()
        if action not in SIGNAL_ACTIONS:
            raise ValueError("signal action is not supported")
        symbol = _clean_text(payload.get("symbol") or payload.get("ticker"), max_length=60)
        if symbol and "/" not in symbol:
            symbol = symbol.replace("-", "/")
        signal_id = _clean_text(payload.get("id") or payload.get("signal_id"), max_length=80)
        if not signal_id:
            signal_id = _new_id("signal")
        return cls(
            id=signal_id,
            source=normalized_source,
            strategy_id=_clean_text(payload.get("strategy_id") or default_strategy_id, max_length=80),
            symbol=symbol,
            side=side,
            action=action,
            price=float(payload.get("price") or 0.0),
            amount=float(payload.get("amount") or payload.get("base_amount") or 0.0),
            quote_notional=float(payload.get("quote_notional") or payload.get("quote") or 0.0),
            timeframe=_clean_text(payload.get("timeframe") or payload.get("interval"), max_length=40),
            message=_clean_text(payload.get("message") or payload.get("comment"), max_length=240),
            status=_clean_text(status, max_length=40),
            reason=_clean_text(reason, max_length=240),
            received_at=float(payload.get("received_at") or _now()),
            raw=_clean_dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "timeframe": self.timeframe,
            "message": self.message,
            "status": self.status,
            "reason": self.reason,
            "received_at": self.received_at,
            "raw": self.raw,
        }


class StrategyCenterStore:
    def __init__(self, path: str | Path, *, max_recent_signals: int = 100) -> None:
        self.path = Path(path)
        self.max_recent_signals = max(1, int(max_recent_signals))
        self._sqlite_ready = False

    def read(self) -> dict[str, Any]:
        if self._is_sqlite:
            return self._read_sqlite()
        return self._read_json()

    def _read_json(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.empty_payload()
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read strategy center store: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("strategy center store must be a JSON object")
        return self._normalize_payload(raw)

    def write(self, payload: dict[str, Any]) -> None:
        if self._is_sqlite:
            self._write_sqlite(payload)
            return
        self._write_json(payload)

    def _write_json(self, payload: dict[str, Any]) -> None:
        normalized = self._normalize_payload(payload)
        normalized["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(normalized, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def _is_sqlite(self) -> bool:
        return self.path.suffix.lower() in SQLITE_SUFFIXES

    def _connect_sqlite(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure_sqlite(self) -> None:
        if self._sqlite_ready:
            return
        with self._connect_sqlite() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_instances (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL DEFAULT '',
                    asset TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_instances_owner
                    ON strategy_instances(owner_email);
                CREATE INDEX IF NOT EXISTS idx_strategy_instances_asset
                    ON strategy_instances(asset);
                CREATE TABLE IF NOT EXISTS user_api_accounts (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_api_accounts_owner
                    ON user_api_accounts(owner_email);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    received_at REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_received_at
                    ON signals(received_at);
                CREATE INDEX IF NOT EXISTS idx_signals_strategy_id
                    ON signals(strategy_id);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO metadata(key, value)
                VALUES('schema_version', '1')
                """
            )
            self._migrate_legacy_json_unlocked(connection)
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._sqlite_ready = True

    def _legacy_json_path(self) -> Path:
        return self.path.with_suffix(".json")

    def _migrate_legacy_json_unlocked(self, connection: sqlite3.Connection) -> None:
        checked = connection.execute(
            "SELECT value FROM metadata WHERE key = 'json_migration_checked'"
        ).fetchone()
        if checked is not None:
            return
        legacy_path = self._legacy_json_path()
        if legacy_path.exists():
            try:
                raw = json.loads(legacy_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._write_sqlite_payload_unlocked(
                        connection,
                        self._normalize_payload(raw),
                        updated_at=float(raw.get("updated_at") or _now()),
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO metadata(key, value)
                        VALUES('json_migrated_from', ?)
                        """,
                        (str(legacy_path),),
                    )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO metadata(key, value)
                    VALUES('json_migration_error', ?)
                    """,
                    (f"{exc.__class__.__name__}: {exc}",),
                )
        connection.execute(
            """
            INSERT OR REPLACE INTO metadata(key, value)
            VALUES('json_migration_checked', '1')
            """
        )

    def _json_dumps(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _updated_at_sqlite(self, connection: sqlite3.Connection) -> float | None:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'updated_at'"
        ).fetchone()
        if row is None:
            return None
        try:
            return float(row["value"])
        except (TypeError, ValueError):
            return None

    def _set_updated_at_sqlite(
        self,
        connection: sqlite3.Connection,
        updated_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO metadata(key, value)
            VALUES('updated_at', ?)
            """,
            (str(float(updated_at)),),
        )

    def _read_sqlite(self) -> dict[str, Any]:
        self._ensure_sqlite()
        with self._connect_sqlite() as connection:
            strategy_rows = connection.execute(
                """
                SELECT payload FROM strategy_instances
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
            account_rows = connection.execute(
                """
                SELECT payload FROM user_api_accounts
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
            funding_row = connection.execute(
                "SELECT payload FROM settings WHERE key = 'funding_arbitrage'"
            ).fetchone()
            signal_bot_row = connection.execute(
                "SELECT payload FROM settings WHERE key = 'signal_bot'"
            ).fetchone()
            signal_rows = connection.execute(
                """
                SELECT payload FROM (
                    SELECT payload, received_at, id FROM signals
                    ORDER BY received_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY received_at ASC, id ASC
                """,
                (self.max_recent_signals,),
            ).fetchall()
            raw = {
                "version": 1,
                "updated_at": self._updated_at_sqlite(connection),
                "strategy_instances": [
                    json.loads(row["payload"]) for row in strategy_rows
                ],
                "user_api_accounts": [
                    json.loads(row["payload"]) for row in account_rows
                ],
                "funding_arbitrage": (
                    json.loads(funding_row["payload"])
                    if funding_row is not None
                    else {}
                ),
                "signal_bot": (
                    json.loads(signal_bot_row["payload"])
                    if signal_bot_row is not None
                    else {}
                ),
                "signals": [json.loads(row["payload"]) for row in signal_rows],
            }
        return self._normalize_payload(raw)

    def _write_sqlite(self, payload: dict[str, Any]) -> None:
        normalized = self._normalize_payload(payload)
        updated_at = _now()
        self._ensure_sqlite()
        with self._connect_sqlite() as connection:
            self._write_sqlite_payload_unlocked(
                connection,
                normalized,
                updated_at=updated_at,
            )
            connection.commit()

    def _write_sqlite_payload_unlocked(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        *,
        updated_at: float,
    ) -> None:
        connection.execute("DELETE FROM strategy_instances")
        connection.execute("DELETE FROM user_api_accounts")
        connection.execute("DELETE FROM settings")
        connection.execute("DELETE FROM signals")
        for item in payload["strategy_instances"]:
            strategy = StrategyInstance.from_dict(item)
            self._upsert_strategy_sqlite_unlocked(connection, strategy)
        for item in payload["user_api_accounts"]:
            account = UserApiAccount.from_dict(item)
            self._upsert_api_account_sqlite_unlocked(connection, account)
        connection.execute(
            """
            INSERT OR REPLACE INTO settings(key, payload, updated_at)
            VALUES('funding_arbitrage', ?, ?)
            """,
            (self._json_dumps(payload["funding_arbitrage"]), updated_at),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO settings(key, payload, updated_at)
            VALUES('signal_bot', ?, ?)
            """,
            (self._json_dumps(payload["signal_bot"]), updated_at),
        )
        for item in payload["signals"][-self.max_recent_signals :]:
            event = SignalEvent.from_dict(item)
            self._upsert_signal_sqlite_unlocked(connection, event)
        self._trim_signals_sqlite_unlocked(connection)
        self._set_updated_at_sqlite(connection, updated_at)

    def _upsert_strategy_sqlite_unlocked(
        self,
        connection: sqlite3.Connection,
        strategy: StrategyInstance,
    ) -> None:
        row = strategy.to_dict()
        connection.execute(
            """
            INSERT OR REPLACE INTO strategy_instances(
                id, owner_email, asset, symbol, updated_at, payload
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                strategy.id,
                strategy.owner_email,
                strategy.asset,
                strategy.symbol,
                strategy.updated_at,
                self._json_dumps(row),
            ),
        )

    def _upsert_api_account_sqlite_unlocked(
        self,
        connection: sqlite3.Connection,
        account: UserApiAccount,
    ) -> None:
        row = account.to_dict()
        connection.execute(
            """
            INSERT OR REPLACE INTO user_api_accounts(
                id, owner_email, updated_at, payload
            )
            VALUES(?, ?, ?, ?)
            """,
            (
                account.id,
                account.owner_email,
                account.updated_at,
                self._json_dumps(row),
            ),
        )

    def _upsert_signal_sqlite_unlocked(
        self,
        connection: sqlite3.Connection,
        event: SignalEvent,
    ) -> None:
        row = event.to_dict()
        connection.execute(
            """
            INSERT OR REPLACE INTO signals(
                id, strategy_id, source, symbol, received_at, payload
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.strategy_id,
                event.source,
                event.symbol,
                event.received_at,
                self._json_dumps(row),
            ),
        )

    def _trim_signals_sqlite_unlocked(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM signals
            WHERE id NOT IN (
                SELECT id FROM signals
                ORDER BY received_at DESC, id DESC
                LIMIT ?
            )
            """,
            (self.max_recent_signals,),
        )

    def empty_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": None,
            "strategy_instances": [],
            "user_api_accounts": [],
            "funding_arbitrage": FundingArbitrageSettings().to_dict(),
            "signal_bot": SignalBotSettings().to_dict(),
            "signals": [],
        }

    def _normalize_payload(self, raw: dict[str, Any]) -> dict[str, Any]:
        strategies = [
            StrategyInstance.from_dict(item).to_dict()
            for item in raw.get("strategy_instances", [])
            if isinstance(item, dict)
        ]
        accounts = [
            UserApiAccount.from_dict(item).to_dict()
            for item in raw.get("user_api_accounts", [])
            if isinstance(item, dict)
        ]
        signals = [
            SignalEvent.from_dict(item).to_dict()
            for item in raw.get("signals", [])[-self.max_recent_signals :]
            if isinstance(item, dict)
        ]
        return {
            "version": 1,
            "updated_at": raw.get("updated_at"),
            "strategy_instances": strategies,
            "user_api_accounts": accounts,
            "funding_arbitrage": FundingArbitrageSettings.from_dict(
                raw.get("funding_arbitrage", {})
            ).to_dict(),
            "signal_bot": SignalBotSettings.from_dict(raw.get("signal_bot", {})).to_dict(),
            "signals": signals,
        }

    def upsert_strategy(self, strategy: StrategyInstance) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            strategy = StrategyInstance.from_dict(
                {**strategy.to_dict(), "updated_at": updated_at}
            )
            with self._connect_sqlite() as connection:
                self._upsert_strategy_sqlite_unlocked(connection, strategy)
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        rows = [
            item
            for item in payload["strategy_instances"]
            if item.get("id") != strategy.id
        ]
        rows.append(strategy.to_dict())
        payload["strategy_instances"] = rows
        self.write(payload)
        return self.read()

    def delete_strategy(self, strategy_id: str) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            with self._connect_sqlite() as connection:
                connection.execute(
                    "DELETE FROM strategy_instances WHERE id = ?",
                    (strategy_id,),
                )
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        payload["strategy_instances"] = [
            item
            for item in payload["strategy_instances"]
            if item.get("id") != strategy_id
        ]
        self.write(payload)
        return self.read()

    def upsert_api_account(self, account: UserApiAccount) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            account = UserApiAccount.from_dict(
                {**account.to_dict(), "updated_at": updated_at}
            )
            with self._connect_sqlite() as connection:
                self._upsert_api_account_sqlite_unlocked(connection, account)
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        rows = [
            item
            for item in payload["user_api_accounts"]
            if item.get("id") != account.id
        ]
        rows.append(account.to_dict())
        payload["user_api_accounts"] = rows
        self.write(payload)
        return self.read()

    def delete_api_account(self, account_id: str) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            with self._connect_sqlite() as connection:
                connection.execute(
                    "DELETE FROM user_api_accounts WHERE id = ?",
                    (account_id,),
                )
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        payload["user_api_accounts"] = [
            item
            for item in payload["user_api_accounts"]
            if item.get("id") != account_id
        ]
        self.write(payload)
        return self.read()

    def update_funding(self, settings: FundingArbitrageSettings) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            row = FundingArbitrageSettings.from_dict(
                {**settings.to_dict(), "updated_at": updated_at}
            )
            with self._connect_sqlite() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO settings(key, payload, updated_at)
                    VALUES('funding_arbitrage', ?, ?)
                    """,
                    (self._json_dumps(row.to_dict()), updated_at),
                )
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        payload["funding_arbitrage"] = settings.to_dict()
        self.write(payload)
        return self.read()

    def update_signal_bot(self, settings: SignalBotSettings) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            row = SignalBotSettings.from_dict(
                {**settings.to_dict(), "updated_at": updated_at}
            )
            with self._connect_sqlite() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO settings(key, payload, updated_at)
                    VALUES('signal_bot', ?, ?)
                    """,
                    (self._json_dumps(row.to_dict()), updated_at),
                )
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        payload["signal_bot"] = settings.to_dict()
        self.write(payload)
        return self.read()

    def append_signal(self, event: SignalEvent) -> dict[str, Any]:
        if self._is_sqlite:
            self._ensure_sqlite()
            updated_at = _now()
            with self._connect_sqlite() as connection:
                self._upsert_signal_sqlite_unlocked(connection, event)
                if event.strategy_id:
                    strategy_row = connection.execute(
                        """
                        SELECT payload FROM strategy_instances WHERE id = ?
                        """,
                        (event.strategy_id,),
                    ).fetchone()
                    if strategy_row is not None:
                        strategy_payload = json.loads(strategy_row["payload"])
                        strategy_payload["last_signal_id"] = event.id
                        strategy_payload["updated_at"] = updated_at
                        strategy = StrategyInstance.from_dict(strategy_payload)
                        self._upsert_strategy_sqlite_unlocked(connection, strategy)
                self._trim_signals_sqlite_unlocked(connection)
                self._set_updated_at_sqlite(connection, updated_at)
                connection.commit()
            return self.read()
        payload = self.read()
        rows = [item for item in payload.get("signals", []) if item.get("id") != event.id]
        rows.append(event.to_dict())
        payload["signals"] = rows[-self.max_recent_signals :]
        if event.strategy_id:
            strategies: list[dict[str, Any]] = []
            for item in payload.get("strategy_instances", []):
                if item.get("id") == event.strategy_id:
                    item = {**item, "last_signal_id": event.id, "updated_at": _now()}
                strategies.append(item)
            payload["strategy_instances"] = strategies
        self.write(payload)
        return self.read()


def build_strategy_center_public_payload(
    store_payload: dict[str, Any],
    *,
    current_user_email: str = "",
    current_user_role: str = "",
    allowed_assets: list[str] | None = None,
) -> dict[str, Any]:
    allowed = {asset.upper() for asset in allowed_assets or [] if asset}
    is_admin = current_user_role == "admin" or not current_user_email

    def can_view_owner(owner_email: str) -> bool:
        return is_admin or _clean_email(owner_email) == current_user_email

    def can_view_asset(asset: str, symbol: str = "") -> bool:
        if is_admin or not allowed:
            return True
        normalized = _clean_asset(asset) or _asset_from_symbol(symbol)
        return not normalized or normalized in allowed

    strategies = [
        StrategyInstance.from_dict(item).to_dict()
        for item in store_payload.get("strategy_instances", [])
        if isinstance(item, dict)
        and can_view_owner(str(item.get("owner_email") or ""))
        and can_view_asset(str(item.get("asset") or ""), str(item.get("symbol") or ""))
    ]
    accounts = [
        UserApiAccount.from_dict(item).public_dict()
        for item in store_payload.get("user_api_accounts", [])
        if isinstance(item, dict)
        and can_view_owner(str(item.get("owner_email") or ""))
        and (
            is_admin
            or not allowed
            or not item.get("asset_scope")
            or bool(allowed.intersection({asset.upper() for asset in item.get("asset_scope", [])}))
        )
    ]
    signals = [
        SignalEvent.from_dict(item).to_dict()
        for item in store_payload.get("signals", [])
        if isinstance(item, dict)
        and can_view_asset("", str(item.get("symbol") or ""))
    ]
    enabled_count = sum(1 for item in strategies if item.get("enabled"))
    live_count = sum(1 for item in strategies if item.get("live_enabled"))
    return {
        "status": "ok",
        "updated_at": store_payload.get("updated_at"),
        "strategy_instances": sorted(strategies, key=lambda item: item.get("updated_at") or 0, reverse=True),
        "user_api_accounts": sorted(accounts, key=lambda item: item.get("updated_at") or 0, reverse=True),
        "funding_arbitrage": FundingArbitrageSettings.from_dict(
            store_payload.get("funding_arbitrage", {})
        ).to_dict(),
        "signal_bot": SignalBotSettings.from_dict(store_payload.get("signal_bot", {})).to_dict(),
        "signals": list(reversed(signals[-50:])),
        "summary": {
            "strategy_count": len(strategies),
            "enabled_count": enabled_count,
            "live_count": live_count,
            "api_account_count": len(accounts),
            "recent_signal_count": len(signals),
            "pnl_quote": sum(float(item.get("pnl_quote") or 0.0) for item in strategies),
            "open_order_count": sum(int(item.get("open_order_count") or 0) for item in strategies),
        },
    }
