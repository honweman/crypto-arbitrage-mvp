from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address

from .user_strategies import (
    USER_STRATEGY_DEFINITIONS,
    UserStrategy,
    strategy_parameter_blockers,
    user_strategy_catalog,
)


PROJECT_STATUSES = {"pending", "active", "disabled"}
MARKET_TYPES = {"spot", "swap", "future"}
CONNECTION_STATUSES = {"unverified", "healthy", "error"}
VENUE_CONNECTION_STATUSES = {"healthy", "error"}
VENUE_HEALTHY_REFRESH_SECONDS = 300.0
VENUE_ERROR_RETRY_SECONDS = 60.0
VENUE_CONNECTION_STALE_SECONDS = 600.0
CONNECTION_MAX_AGE_SECONDS = 86_400.0
CREDENTIAL_ROTATION_SECONDS = 90 * 86_400.0
WALLET_CHALLENGE_TTL_SECONDS = 300.0
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ASSET_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,19}$")
SYMBOL_RE = re.compile(
    r"^[A-Z0-9][A-Z0-9._-]{0,29}/[A-Z0-9][A-Z0-9._-]{0,29}"
    r"(?::[A-Z0-9][A-Z0-9._-]{0,29})?$"
)
CREDENTIAL_FIELDS = {"api_key", "secret", "passphrase", "password"}

EXCHANGE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "coinbase",
        "label": "Coinbase",
        "market_types": ["spot"],
        "required_credentials": ["api_key", "secret"],
        "default_variant": "default",
        "variants": [{"id": "default", "label": "Default"}],
    },
    {
        "id": "bithumb",
        "label": "Bithumb",
        "market_types": ["spot"],
        "required_credentials": ["api_key", "secret"],
        "default_variant": "v2",
        "variants": [{"id": "v2", "label": "API v2.0"}],
    },
    {
        "id": "upbit",
        "label": "Upbit",
        "market_types": ["spot"],
        "required_credentials": ["api_key", "secret"],
        "default_variant": "global",
        "variants": [
            {"id": "global", "label": "Global"},
            {"id": "indonesia", "label": "Indonesia (id.Upbit)"},
        ],
    },
    {
        "id": "bybit",
        "label": "Bybit",
        "market_types": ["spot", "swap"],
        "required_credentials": ["api_key", "secret"],
        "default_variant": "default",
        "variants": [{"id": "default", "label": "Default"}],
    },
    {
        "id": "binance",
        "label": "Binance",
        "market_types": ["spot", "swap"],
        "required_credentials": ["api_key", "secret"],
        "default_variant": "default",
        "variants": [{"id": "default", "label": "Default"}],
    },
    {
        "id": "hyperliquid",
        "label": "Hyperliquid",
        "market_types": ["spot", "swap"],
        "required_credentials": ["api_key", "secret"],
        "credential_labels": {
            "api_key": "Main Wallet Address",
            "secret": "Agent Wallet Private Key",
        },
        "default_variant": "mainnet",
        "variants": [
            {"id": "mainnet", "label": "Mainnet"},
            {"id": "testnet", "label": "Testnet"},
        ],
    },
    {
        "id": "dydx",
        "label": "dYdX v4",
        "market_types": ["swap"],
        "required_credentials": ["api_key", "secret"],
        "credential_labels": {
            "api_key": "dYdX Chain Address",
            "secret": "Dedicated Trading Mnemonic",
        },
        "default_variant": "mainnet",
        "variants": [{"id": "mainnet", "label": "Mainnet"}],
    },
    {
        "id": "aster",
        "label": "Aster",
        "market_types": ["spot", "swap"],
        "required_credentials": ["api_key", "secret"],
        "credential_labels": {
            "api_key": "Owner Wallet Address",
            "secret": "Dedicated Signer Private Key",
        },
        "default_variant": "v3",
        "variants": [{"id": "v3", "label": "API v3"}],
    },
)
EXCHANGES_BY_ID = {row["id"]: row for row in EXCHANGE_CATALOG}

DEX_VENUE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "hyperliquid",
        "label": "Hyperliquid",
        "market_types": ["spot", "swap"],
        "wallet_network": "EVM",
        "wallet_required": True,
        "read_only_supported": True,
        "automation_auth": "agent_wallet",
        "live_enabled": False,
    },
    {
        "id": "polymarket",
        "label": "Polymarket",
        "market_types": ["prediction"],
        "wallet_network": "Polygon",
        "wallet_required": True,
        "required_chain_id": 137,
        "read_only_supported": True,
        "automation_auth": "eip712_and_clob_credentials",
        "live_enabled": False,
    },
    {
        "id": "dydx",
        "label": "dYdX",
        "market_types": ["swap"],
        "wallet_network": "dYdX Chain",
        "wallet_required": False,
        "read_only_supported": True,
        "automation_auth": "permissioned_key",
        "live_enabled": False,
    },
    {
        "id": "aster",
        "label": "Aster",
        "market_types": ["spot", "swap"],
        "wallet_network": "EVM",
        "wallet_required": True,
        "read_only_supported": True,
        "automation_auth": "api_or_signer_key",
        "live_enabled": False,
    },
)
DEX_VENUES_BY_ID = {row["id"]: row for row in DEX_VENUE_CATALOG}


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _clean_id(value: Any, *, prefix: str) -> str:
    result = str(value or "").strip() or _new_id(prefix)
    if not ID_RE.fullmatch(result):
        raise ValueError(f"{prefix} id contains unsupported characters")
    return result


def _clean_email(value: Any) -> str:
    result = str(value or "").strip().lower()
    if not result or "@" not in result:
        raise ValueError("owner email is required")
    return result


def _clean_text(value: Any, *, max_length: int = 80) -> str:
    return str(value or "").strip()[:max_length]


def _strict_bool(value: Any, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _clean_asset(value: Any, *, label: str) -> str:
    result = str(value or "").strip().upper()
    if not ASSET_RE.fullmatch(result):
        raise ValueError(f"{label} must be 2-20 letters, numbers, '.', '_' or '-'")
    return result


def _clean_symbol(value: Any) -> str:
    result = str(value or "").strip().upper()
    if result and not SYMBOL_RE.fullmatch(result):
        raise ValueError(
            "account symbol must use BASE/QUOTE or BASE/QUOTE:SETTLE format"
        )
    return result


def exchange_catalog() -> list[dict[str, Any]]:
    return [dict(row) for row in EXCHANGE_CATALOG]


def required_credentials_for_exchange(exchange: str) -> set[str]:
    row = EXCHANGES_BY_ID.get(str(exchange or "").strip().lower())
    if row is None:
        raise ValueError(f"unsupported exchange: {exchange}")
    return set(row.get("required_credentials") or [])


def validate_exchange_credentials(
    exchange: str,
    credentials: dict[str, str],
) -> None:
    exchange_id = str(exchange or "").strip().lower()
    if exchange_id in {"hyperliquid", "aster"}:
        owner_address = _normalize_evm_address(credentials.get("api_key"))
        try:
            signer_address = Account.from_key(
                str(credentials.get("secret") or "")
            ).address
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{exchange_id} signer key must be a valid 32-byte EVM private key"
            ) from exc
        if signer_address == owner_address:
            raise ValueError(
                f"{exchange_id} requires a dedicated agent/signer key, not the owner wallet key"
            )
    elif exchange_id == "dydx":
        address = str(credentials.get("api_key") or "").strip().lower()
        if not address.startswith("dydx1") or len(address) < 20:
            raise ValueError("dydx API key field must be a dYdX Chain address")
        words = str(credentials.get("secret") or "").strip().split()
        if len(words) not in {12, 15, 18, 21, 24}:
            raise ValueError(
                "dydx trading mnemonic must contain 12, 15, 18, 21, or 24 words"
            )


def dex_venue_catalog() -> list[dict[str, Any]]:
    return [dict(row) for row in DEX_VENUE_CATALOG]


def _safe_venue_detail(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "market_count",
        "account_value",
        "position_count",
        "server_time",
        "balance_available",
    }
    return {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, (str, int, float, bool, type(None)))
    }


def _normalize_evm_address(value: Any) -> str:
    address = str(value or "").strip()
    if not is_address(address):
        raise ValueError("wallet address must be a valid EVM address")
    return to_checksum_address(address)


@dataclass(frozen=True)
class UserWalletConnection:
    id: str
    owner_email: str
    address: str
    chain_id: int
    wallet_type: str = "injected"
    label: str = "EVM Wallet"
    permissions: tuple[str, ...] = ("identify", "read_account")
    verified_at: float = field(default_factory=_now)
    last_seen_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserWalletConnection":
        if not isinstance(raw, dict):
            raise ValueError("wallet connection must be an object")
        try:
            chain_id = int(raw.get("chain_id") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("wallet chain id must be an integer") from exc
        if chain_id <= 0:
            raise ValueError("wallet chain id must be positive")
        permissions = tuple(
            item
            for item in raw.get("permissions", ["identify", "read_account"])
            if item in {"identify", "read_account"}
        ) or ("identify", "read_account")
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), prefix="wallet"),
            owner_email=_clean_email(raw.get("owner_email")),
            address=_normalize_evm_address(raw.get("address")),
            chain_id=chain_id,
            wallet_type=_clean_text(raw.get("wallet_type") or "injected", max_length=40),
            label=_clean_text(raw.get("label") or "EVM Wallet", max_length=80),
            permissions=permissions,
            verified_at=float(raw.get("verified_at") or now),
            last_seen_at=float(raw.get("last_seen_at") or now),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_email": self.owner_email,
            "address": self.address,
            "chain_id": self.chain_id,
            "wallet_type": self.wallet_type,
            "label": self.label,
            "permissions": list(self.permissions),
            "verified_at": self.verified_at,
            "last_seen_at": self.last_seen_at,
            "trading_authorized": False,
        }


@dataclass(frozen=True)
class UserVenueConnection:
    id: str
    owner_email: str
    venue: str
    wallet_id: str
    wallet_address: str
    status: str
    latency_ms: float
    checked_at: float
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserVenueConnection":
        if not isinstance(raw, dict):
            raise ValueError("venue connection must be an object")
        venue = str(raw.get("venue") or "").strip().lower()
        if venue not in DEX_VENUES_BY_ID:
            raise ValueError(f"unsupported decentralized venue: {venue}")
        status = str(raw.get("status") or "error").strip().lower()
        if status not in VENUE_CONNECTION_STATUSES:
            raise ValueError(f"unsupported venue connection status: {status}")
        wallet_address = str(raw.get("wallet_address") or "").strip()
        if wallet_address:
            wallet_address = _normalize_evm_address(wallet_address)
        return cls(
            id=_clean_id(raw.get("id"), prefix="venue-connection"),
            owner_email=_clean_email(raw.get("owner_email")),
            venue=venue,
            wallet_id=str(raw.get("wallet_id") or "").strip(),
            wallet_address=wallet_address,
            status=status,
            latency_ms=max(0.0, float(raw.get("latency_ms") or 0.0)),
            checked_at=float(raw.get("checked_at") or _now()),
            detail=(dict(raw.get("detail")) if isinstance(raw.get("detail"), dict) else {}),
            error=_clean_text(raw.get("error"), max_length=240),
        )

    def to_dict(self) -> dict[str, Any]:
        now = _now()
        age_seconds = max(0.0, now - self.checked_at)
        refresh_seconds = (
            VENUE_HEALTHY_REFRESH_SECONDS
            if self.status == "healthy"
            else VENUE_ERROR_RETRY_SECONDS
        )
        stale = age_seconds > VENUE_CONNECTION_STALE_SECONDS
        return {
            "id": self.id,
            "owner_email": self.owner_email,
            "venue": self.venue,
            "wallet_id": self.wallet_id,
            "wallet_address": self.wallet_address,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
            "detail": dict(self.detail),
            "error": self.error,
            "health_age_seconds": age_seconds,
            "next_check_at": self.checked_at + refresh_seconds,
            "stale": stale,
            "read_only_verified": self.status == "healthy" and not stale,
            "trading_authorized": False,
        }


@dataclass(frozen=True)
class UserProject:
    id: str
    owner_email: str
    name: str
    asset: str
    quote_currency: str
    status: str = "pending"
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserProject":
        if not isinstance(raw, dict):
            raise ValueError("project must be an object")
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in PROJECT_STATUSES:
            raise ValueError(f"unsupported project status: {status}")
        asset = _clean_asset(raw.get("asset"), label="project asset")
        quote = _clean_asset(
            raw.get("quote_currency") or raw.get("quote"),
            label="quote currency",
        )
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), prefix="project"),
            owner_email=_clean_email(raw.get("owner_email")),
            name=_clean_text(raw.get("name") or f"{asset}/{quote}"),
            asset=asset,
            quote_currency=quote,
            status=status,
            created_at=float(raw.get("created_at") or now),
            updated_at=float(raw.get("updated_at") or now),
        )

    @property
    def symbol(self) -> str:
        return f"{self.asset}/{self.quote_currency}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_email": self.owner_email,
            "name": self.name,
            "asset": self.asset,
            "quote_currency": self.quote_currency,
            "symbol": self.symbol,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class UserExchangeAccount:
    id: str
    owner_email: str
    project_id: str
    label: str
    exchange: str
    market_type: str = "spot"
    api_variant: str = "default"
    symbol: str = ""
    enabled: bool = False
    withdrawal_disabled_confirmed: bool = False
    trade_permission_confirmed: bool = False
    connection_status: str = "unverified"
    connection_checked_at: float | None = None
    connection_error: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserExchangeAccount":
        if not isinstance(raw, dict):
            raise ValueError("exchange account must be an object")
        exchange = str(raw.get("exchange") or "").strip().lower()
        exchange_row = EXCHANGES_BY_ID.get(exchange)
        if exchange_row is None:
            raise ValueError(f"unsupported exchange: {exchange}")
        market_type = str(raw.get("market_type") or "spot").strip().lower()
        if (
            market_type not in MARKET_TYPES
            or market_type not in exchange_row["market_types"]
        ):
            raise ValueError(f"{exchange} does not support {market_type} accounts")
        variants = {
            str(item.get("id") or "")
            for item in exchange_row.get("variants", [])
            if isinstance(item, dict)
        }
        api_variant = (
            str(
                raw.get("api_variant")
                or exchange_row.get("default_variant")
                or "default"
            )
            .strip()
            .lower()
        )
        if api_variant not in variants:
            raise ValueError(f"{exchange} does not support API variant {api_variant}")
        connection_status = (
            str(raw.get("connection_status") or "unverified").strip().lower()
        )
        if connection_status not in CONNECTION_STATUSES:
            connection_status = "unverified"
        project_id = _clean_id(raw.get("project_id"), prefix="project")
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), prefix="account"),
            owner_email=_clean_email(raw.get("owner_email")),
            project_id=project_id,
            label=_clean_text(raw.get("label") or exchange_row["label"]),
            exchange=exchange,
            market_type=market_type,
            api_variant=api_variant,
            symbol=_clean_symbol(raw.get("symbol")),
            enabled=_strict_bool(
                raw.get("enabled"),
                label="account enabled",
                default=False,
            ),
            withdrawal_disabled_confirmed=_strict_bool(
                raw.get("withdrawal_disabled_confirmed"),
                label="withdrawal-disabled confirmation",
                default=False,
            ),
            trade_permission_confirmed=_strict_bool(
                raw.get("trade_permission_confirmed"),
                label="trade permission confirmation",
                default=bool(raw.get("withdrawal_disabled_confirmed", False)),
            ),
            connection_status=connection_status,
            connection_checked_at=(
                float(raw["connection_checked_at"])
                if raw.get("connection_checked_at") is not None
                else None
            ),
            connection_error=_clean_text(
                raw.get("connection_error"),
                max_length=240,
            ),
            created_at=float(raw.get("created_at") or now),
            updated_at=float(raw.get("updated_at") or now),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_email": self.owner_email,
            "project_id": self.project_id,
            "label": self.label,
            "exchange": self.exchange,
            "market_type": self.market_type,
            "api_variant": self.api_variant,
            "symbol": self.symbol,
            "enabled": self.enabled,
            "withdrawal_disabled_confirmed": self.withdrawal_disabled_confirmed,
            "trade_permission_confirmed": self.trade_permission_confirmed,
            "connection_status": self.connection_status,
            "connection_checked_at": self.connection_checked_at,
            "connection_error": self.connection_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def account_connection_is_fresh(
    account: UserExchangeAccount,
    *,
    now: float | None = None,
) -> bool:
    if account.connection_status != "healthy":
        return False
    if account.connection_checked_at is None:
        return False
    age = (now if now is not None else _now()) - account.connection_checked_at
    return 0.0 <= age <= CONNECTION_MAX_AGE_SECONDS


@dataclass(frozen=True)
class UserRiskProfile:
    owner_email: str
    trading_enabled: bool = True
    max_total_exposure_quote: float = 0.0
    max_daily_loss_quote: float = 0.0
    max_open_orders: int = 0
    max_active_strategies: int = 0
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserRiskProfile":
        if not isinstance(raw, dict):
            raise ValueError("user risk profile must be an object")

        def non_negative_float(key: str, default: float) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a number") from exc
            if value < 0:
                raise ValueError(f"{key} must be non-negative")
            return value

        def non_negative_int(key: str, default: int) -> int:
            value = non_negative_float(key, float(default))
            if not value.is_integer():
                raise ValueError(f"{key} must be an integer")
            return int(value)

        return cls(
            owner_email=_clean_email(raw.get("owner_email")),
            trading_enabled=_strict_bool(
                raw.get("trading_enabled"),
                label="user trading enabled",
                default=True,
            ),
            max_total_exposure_quote=non_negative_float(
                "max_total_exposure_quote",
                0.0,
            ),
            max_daily_loss_quote=non_negative_float(
                "max_daily_loss_quote",
                0.0,
            ),
            max_open_orders=non_negative_int("max_open_orders", 0),
            max_active_strategies=non_negative_int(
                "max_active_strategies",
                0,
            ),
            updated_at=float(raw.get("updated_at") or _now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_email": self.owner_email,
            "trading_enabled": self.trading_enabled,
            "max_total_exposure_quote": self.max_total_exposure_quote,
            "max_daily_loss_quote": self.max_daily_loss_quote,
            "max_open_orders": self.max_open_orders,
            "max_active_strategies": self.max_active_strategies,
            "live_submit_allowed": False,
            "updated_at": self.updated_at,
        }


class CredentialCipher:
    def __init__(self, encoded_key: str | None) -> None:
        self._key: bytes | None = None
        if encoded_key:
            try:
                padding = "=" * ((4 - len(encoded_key) % 4) % 4)
                key = base64.urlsafe_b64decode((encoded_key + padding).encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError("credential master key is not valid base64") from exc
            if len(key) != 32:
                raise ValueError("credential master key must decode to 32 bytes")
            self._key = key

    @property
    def available(self) -> bool:
        return self._key is not None

    @staticmethod
    def _associated_data(account_id: str, owner_email: str) -> bytes:
        return f"crypto-arb-user-credential:v1:{owner_email}:{account_id}".encode(
            "utf-8"
        )

    def encrypt(
        self,
        *,
        account_id: str,
        owner_email: str,
        credentials: dict[str, str],
    ) -> tuple[bytes, bytes]:
        if self._key is None:
            raise RuntimeError("credential encryption is not configured")
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(
            credentials,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            plaintext,
            self._associated_data(account_id, owner_email),
        )
        return nonce, ciphertext

    def decrypt(
        self,
        *,
        account_id: str,
        owner_email: str,
        nonce: bytes,
        ciphertext: bytes,
    ) -> dict[str, str]:
        if self._key is None:
            raise RuntimeError("credential encryption is not configured")
        try:
            plaintext = AESGCM(self._key).decrypt(
                nonce,
                ciphertext,
                self._associated_data(account_id, owner_email),
            )
        except InvalidTag as exc:
            raise ValueError("encrypted credentials could not be decrypted") from exc
        raw = json.loads(plaintext.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("decrypted credentials are invalid")
        return {
            key: str(value)
            for key, value in raw.items()
            if key in CREDENTIAL_FIELDS and value
        }


class UserWorkspaceStore:
    def __init__(self, path: str | Path, *, master_key_env: str | None) -> None:
        self.path = Path(path)
        self.master_key_env = master_key_env
        self.cipher = CredentialCipher(
            os.environ.get(master_key_env) if master_key_env else None
        )
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_projects (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_projects_owner
                    ON user_projects(owner_email);
                CREATE TABLE IF NOT EXISTS user_exchange_accounts (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_exchange_accounts_owner
                    ON user_exchange_accounts(owner_email);
                CREATE INDEX IF NOT EXISTS idx_user_exchange_accounts_project
                    ON user_exchange_accounts(project_id);
                CREATE TABLE IF NOT EXISTS user_strategies (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_strategies_owner
                    ON user_strategies(owner_email);
                CREATE INDEX IF NOT EXISTS idx_user_strategies_project
                    ON user_strategies(project_id);
                CREATE TABLE IF NOT EXISTS user_api_credentials (
                    account_id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    fields TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_risk_profiles (
                    owner_email TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_wallet_connections (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    address TEXT NOT NULL,
                    chain_id INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(owner_email, address)
                );
                CREATE INDEX IF NOT EXISTS idx_user_wallet_connections_owner
                    ON user_wallet_connections(owner_email);
                CREATE TABLE IF NOT EXISTS user_wallet_challenges (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    address TEXT NOT NULL,
                    chain_id INTEGER NOT NULL,
                    wallet_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_user_wallet_challenges_owner
                    ON user_wallet_challenges(owner_email, expires_at);
                CREATE TABLE IF NOT EXISTS user_venue_connections (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    wallet_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(owner_email, venue, wallet_id)
                );
                CREATE INDEX IF NOT EXISTS idx_user_venue_connections_owner
                    ON user_venue_connections(owner_email, updated_at);
                """
            )
            self._migrate_legacy_accounts(connection)
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._ready = True

    def _migrate_legacy_accounts(self, connection: sqlite3.Connection) -> None:
        project_rows = connection.execute(
            "SELECT id, payload FROM user_projects"
        ).fetchall()
        projects = {
            str(row["id"]): UserProject.from_dict(json.loads(row["payload"]))
            for row in project_rows
        }
        now = _now()
        account_rows = connection.execute(
            "SELECT id, payload FROM user_exchange_accounts"
        ).fetchall()
        for row in account_rows:
            account = UserExchangeAccount.from_dict(json.loads(row["payload"]))
            project = projects.get(account.project_id)
            symbol = account.symbol or (project.symbol if project else "")
            connection_fresh = account_connection_is_fresh(account, now=now)
            enabled = account.enabled and connection_fresh
            if symbol == account.symbol and enabled == account.enabled:
                continue
            updated = replace(
                account,
                symbol=symbol,
                enabled=enabled,
                updated_at=now,
            )
            connection.execute(
                """
                UPDATE user_exchange_accounts
                SET exchange = ?, updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (
                    updated.exchange,
                    updated.updated_at,
                    self._dump(updated.to_dict()),
                    updated.id,
                ),
            )

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _scope_sql(owner_email: str, is_admin: bool) -> tuple[str, tuple[Any, ...]]:
        if is_admin:
            return "", ()
        return " WHERE owner_email = ?", (_clean_email(owner_email),)

    def list_projects(
        self,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> list[UserProject]:
        self._ensure()
        where, params = self._scope_sql(owner_email, is_admin)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM user_projects"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
        return [UserProject.from_dict(json.loads(row["payload"])) for row in rows]

    def platform_projects(self) -> list[dict[str, Any]]:
        """Return project approval metadata only; never account or credential data."""
        self._ensure()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM user_projects ORDER BY updated_at DESC, id ASC"
            ).fetchall()
        return [
            {
                **UserProject.from_dict(json.loads(row["payload"])).to_dict(),
                "platform_only": True,
            }
            for row in rows
        ]

    def risk_profile(self, owner_email: str) -> UserRiskProfile:
        self._ensure()
        owner = _clean_email(owner_email)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_risk_profiles WHERE owner_email = ?",
                (owner,),
            ).fetchone()
        if row is None:
            return UserRiskProfile(owner_email=owner)
        return UserRiskProfile.from_dict(json.loads(row["payload"]))

    def upsert_risk_profile(self, profile: UserRiskProfile) -> UserRiskProfile:
        self._ensure()
        updated = replace(profile, updated_at=_now())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_risk_profiles(owner_email, updated_at, payload)
                VALUES(?, ?, ?)
                ON CONFLICT(owner_email) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    updated.owner_email,
                    updated.updated_at,
                    self._dump(updated.to_dict()),
                ),
            )
            connection.commit()
        return updated

    def list_accounts(
        self,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> list[UserExchangeAccount]:
        self._ensure()
        where, params = self._scope_sql(owner_email, is_admin)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM user_exchange_accounts"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
            accounts = [
                UserExchangeAccount.from_dict(json.loads(row["payload"]))
                for row in rows
            ]
            accounts = self._disable_stale_accounts_in_connection(
                connection,
                accounts,
            )
            connection.commit()
        return accounts

    def _list_accounts_wallets_and_venues(
        self,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> tuple[
        list[UserExchangeAccount],
        list[UserWalletConnection],
        list[UserVenueConnection],
    ]:
        self._ensure()
        where, params = self._scope_sql(owner_email, is_admin)
        with self._connect() as connection:
            account_rows = connection.execute(
                "SELECT payload FROM user_exchange_accounts"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
            wallet_rows = connection.execute(
                "SELECT payload FROM user_wallet_connections"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
            venue_rows = connection.execute(
                "SELECT payload FROM user_venue_connections"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
            accounts = [
                UserExchangeAccount.from_dict(json.loads(row["payload"]))
                for row in account_rows
            ]
            accounts = self._disable_stale_accounts_in_connection(
                connection,
                accounts,
            )
            connection.commit()
        wallets = [
            UserWalletConnection.from_dict(json.loads(row["payload"]))
            for row in wallet_rows
        ]
        venues = [
            UserVenueConnection.from_dict(json.loads(row["payload"]))
            for row in venue_rows
        ]
        return accounts, wallets, venues

    def list_wallets(
        self,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> list[UserWalletConnection]:
        self._ensure()
        where, params = self._scope_sql(owner_email, is_admin)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM user_wallet_connections"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
        return [
            UserWalletConnection.from_dict(json.loads(row["payload"])) for row in rows
        ]

    def get_wallet(self, wallet_id: str) -> UserWalletConnection | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_wallet_connections WHERE id = ?",
                (str(wallet_id or "").strip(),),
            ).fetchone()
        return (
            UserWalletConnection.from_dict(json.loads(row["payload"])) if row else None
        )

    def create_wallet_challenge(
        self,
        *,
        owner_email: str,
        address: str,
        chain_id: int,
        wallet_type: str,
        domain: str,
    ) -> dict[str, Any]:
        self._ensure()
        owner = _clean_email(owner_email)
        normalized_address = _normalize_evm_address(address)
        try:
            normalized_chain_id = int(chain_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("wallet chain id must be an integer") from exc
        if normalized_chain_id <= 0:
            raise ValueError("wallet chain id must be positive")
        challenge_id = _new_id("wallet-challenge")
        issued_at = int(_now())
        expires_at = float(issued_at) + WALLET_CHALLENGE_TTL_SECONDS
        nonce = secrets.token_urlsafe(24)
        safe_domain = _clean_text(domain or "crypto-arbitrage", max_length=160)
        normalized_wallet_type = _clean_text(
            wallet_type or "injected",
            max_length=40,
        )
        message = "\n".join(
            (
                "Crypto Arbitrage Wallet Authorization",
                "",
                f"Domain: {safe_domain}",
                f"Account: {owner}",
                f"Address: {normalized_address}",
                f"Chain ID: {normalized_chain_id}",
                f"Nonce: {nonce}",
                f"Issued At: {issued_at}",
                f"Expiration: {int(expires_at)}",
                "Purpose: Link this wallet for identification and read-only account access.",
                "This signature does not authorize transactions, token approvals, or withdrawals.",
            )
        )
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM user_wallet_challenges "
                "WHERE expires_at < ? OR used_at IS NOT NULL",
                (_now() - WALLET_CHALLENGE_TTL_SECONDS,),
            )
            connection.execute(
                """
                INSERT INTO user_wallet_challenges(
                    id, owner_email, address, chain_id, wallet_type,
                    message, expires_at, used_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    challenge_id,
                    owner,
                    normalized_address,
                    normalized_chain_id,
                    normalized_wallet_type,
                    message,
                    expires_at,
                ),
            )
            connection.commit()
        return {
            "challenge_id": challenge_id,
            "message": message,
            "address": normalized_address,
            "chain_id": normalized_chain_id,
            "expires_at": expires_at,
        }

    def verify_wallet_challenge(
        self,
        *,
        owner_email: str,
        challenge_id: str,
        signature: str,
        label: str = "",
    ) -> UserWalletConnection:
        self._ensure()
        owner = _clean_email(owner_email)
        challenge_key = str(challenge_id or "").strip()
        signature_text = str(signature or "").strip()
        if not challenge_key or not signature_text:
            raise ValueError("wallet challenge and signature are required")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM user_wallet_challenges WHERE id = ?",
                (challenge_key,),
            ).fetchone()
            if row is None or str(row["owner_email"]) != owner:
                raise ValueError("wallet challenge is invalid")
            if row["used_at"] is not None:
                raise ValueError("wallet challenge has already been used")
            if float(row["expires_at"]) < now:
                raise ValueError("wallet challenge has expired")
            try:
                recovered = Account.recover_message(
                    encode_defunct(text=str(row["message"])),
                    signature=signature_text,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("wallet signature is invalid") from exc
            expected = _normalize_evm_address(row["address"])
            if _normalize_evm_address(recovered) != expected:
                raise ValueError("wallet signature does not match the selected address")
            existing = connection.execute(
                "SELECT payload FROM user_wallet_connections "
                "WHERE owner_email = ? AND address = ?",
                (owner, expected),
            ).fetchone()
            existing_wallet = (
                UserWalletConnection.from_dict(json.loads(existing["payload"]))
                if existing
                else None
            )
            wallet = UserWalletConnection(
                id=(existing_wallet.id if existing_wallet else _new_id("wallet")),
                owner_email=owner,
                address=expected,
                chain_id=int(row["chain_id"]),
                wallet_type=str(row["wallet_type"]),
                label=_clean_text(
                    label or (existing_wallet.label if existing_wallet else "EVM Wallet"),
                    max_length=80,
                ),
                verified_at=(
                    existing_wallet.verified_at if existing_wallet else now
                ),
                last_seen_at=now,
            )
            connection.execute(
                """
                INSERT INTO user_wallet_connections(
                    id, owner_email, address, chain_id, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_email, address) DO UPDATE SET
                    chain_id = excluded.chain_id,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    wallet.id,
                    wallet.owner_email,
                    wallet.address,
                    wallet.chain_id,
                    wallet.last_seen_at,
                    self._dump(wallet.to_dict()),
                ),
            )
            connection.execute(
                "UPDATE user_wallet_challenges SET used_at = ? WHERE id = ?",
                (now, challenge_key),
            )
            connection.commit()
        return wallet

    def delete_wallet(self, wallet_id: str, *, owner_email: str) -> None:
        self._ensure()
        wallet = self.get_wallet(wallet_id)
        if wallet is None:
            raise ValueError(f"wallet not found: {wallet_id}")
        if wallet.owner_email != _clean_email(owner_email):
            raise PermissionError("wallet belongs to another user")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM user_venue_connections WHERE wallet_id = ?",
                (wallet.id,),
            )
            connection.execute(
                "DELETE FROM user_wallet_connections WHERE id = ?",
                (wallet.id,),
            )
            connection.commit()

    def list_venue_connections(
        self,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> list[UserVenueConnection]:
        self._ensure()
        where, params = self._scope_sql(owner_email, is_admin)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM user_venue_connections"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
        return [
            UserVenueConnection.from_dict(json.loads(row["payload"])) for row in rows
        ]

    def get_venue_connection(
        self,
        connection_id: str,
    ) -> UserVenueConnection | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_venue_connections WHERE id = ?",
                (str(connection_id or "").strip(),),
            ).fetchone()
        return (
            UserVenueConnection.from_dict(json.loads(row["payload"])) if row else None
        )

    def upsert_venue_connection(
        self,
        *,
        owner_email: str,
        venue: str,
        wallet: UserWalletConnection | None,
        check: dict[str, Any],
    ) -> UserVenueConnection:
        self._ensure()
        owner = _clean_email(owner_email)
        venue_id = str(venue or "").strip().lower()
        venue_row = DEX_VENUES_BY_ID.get(venue_id)
        if venue_row is None:
            raise ValueError(f"unsupported decentralized venue: {venue_id}")
        if venue_row.get("wallet_required") and wallet is None:
            raise ValueError(f"{venue_id} requires a verified wallet")
        if wallet is not None and wallet.owner_email != owner:
            raise PermissionError("wallet belongs to another user")
        status = str(check.get("status") or "error").strip().lower()
        if status not in VENUE_CONNECTION_STATUSES:
            status = "error"
        safe_detail = _safe_venue_detail(check.get("detail"))
        wallet_id = wallet.id if wallet else ""
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload FROM user_venue_connections "
                "WHERE owner_email = ? AND venue = ? AND wallet_id = ?",
                (owner, venue_id, wallet_id),
            ).fetchone()
            existing_link = (
                UserVenueConnection.from_dict(json.loads(existing["payload"]))
                if existing
                else None
            )
            link = UserVenueConnection(
                id=(
                    existing_link.id
                    if existing_link is not None
                    else _new_id("venue-connection")
                ),
                owner_email=owner,
                venue=venue_id,
                wallet_id=wallet_id,
                wallet_address=wallet.address if wallet else "",
                status=status,
                latency_ms=max(0.0, float(check.get("latency_ms") or 0.0)),
                checked_at=float(check.get("checked_at") or _now()),
                detail=safe_detail,
                error=_clean_text(check.get("error"), max_length=240),
            )
            connection.execute(
                """
                INSERT INTO user_venue_connections(
                    id, owner_email, venue, wallet_id, status, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_email, venue, wallet_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    link.id,
                    link.owner_email,
                    link.venue,
                    link.wallet_id,
                    link.status,
                    link.checked_at,
                    self._dump(link.to_dict()),
                ),
            )
            connection.commit()
        return link

    def record_venue_connection_check(
        self,
        connection_id: str,
        check: dict[str, Any],
    ) -> UserVenueConnection | None:
        """Update a still-existing link without recreating a revoked connection."""
        self._ensure()
        connection_key = str(connection_id or "").strip()
        status = str(check.get("status") or "error").strip().lower()
        if status not in VENUE_CONNECTION_STATUSES:
            status = "error"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM user_venue_connections WHERE id = ?",
                (connection_key,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            existing = UserVenueConnection.from_dict(json.loads(row["payload"]))
            updated = replace(
                existing,
                status=status,
                latency_ms=max(0.0, float(check.get("latency_ms") or 0.0)),
                checked_at=float(check.get("checked_at") or _now()),
                detail=_safe_venue_detail(check.get("detail")),
                error=_clean_text(check.get("error"), max_length=240),
            )
            cursor = connection.execute(
                """
                UPDATE user_venue_connections
                SET status = ?, updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (
                    updated.status,
                    updated.checked_at,
                    self._dump(updated.to_dict()),
                    updated.id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
        return updated

    def delete_venue_connection(
        self,
        connection_id: str,
        *,
        owner_email: str,
    ) -> None:
        self._ensure()
        link = self.get_venue_connection(connection_id)
        if link is None:
            raise ValueError(f"venue connection not found: {connection_id}")
        if link.owner_email != _clean_email(owner_email):
            raise PermissionError("venue connection belongs to another user")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM user_venue_connections WHERE id = ?",
                (link.id,),
            )
            connection.commit()

    def list_strategies(
        self,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> list[UserStrategy]:
        self._ensure()
        where, params = self._scope_sql(owner_email, is_admin)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM user_strategies"
                + where
                + " ORDER BY updated_at DESC, id ASC",
                params,
            ).fetchall()
        return [UserStrategy.from_dict(json.loads(row["payload"])) for row in rows]

    def get_project(self, project_id: str) -> UserProject | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return UserProject.from_dict(json.loads(row["payload"])) if row else None

    def get_account(self, account_id: str) -> UserExchangeAccount | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_exchange_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            account = UserExchangeAccount.from_dict(json.loads(row["payload"]))
            account = self._disable_stale_accounts_in_connection(
                connection,
                [account],
            )[0]
            connection.commit()
        return account

    def get_strategy(self, strategy_id: str) -> UserStrategy | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
        return UserStrategy.from_dict(json.loads(row["payload"])) if row else None

    def _disable_stale_accounts_in_connection(
        self,
        connection: sqlite3.Connection,
        accounts: list[UserExchangeAccount],
    ) -> list[UserExchangeAccount]:
        now = _now()
        result = []
        for account in accounts:
            if not account.enabled or account_connection_is_fresh(account, now=now):
                result.append(account)
                continue
            updated = replace(account, enabled=False, updated_at=now)
            connection.execute(
                """
                UPDATE user_exchange_accounts
                SET updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (updated.updated_at, self._dump(updated.to_dict()), updated.id),
            )
            self._disable_account_strategies_in_connection(connection, updated.id)
            result.append(updated)
        return result

    def upsert_project(self, project: UserProject) -> UserProject:
        self._ensure()
        existing = self.get_project(project.id)
        if existing is not None and existing.owner_email != project.owner_email:
            raise ValueError("project owner cannot be changed")
        scope_changed = bool(
            existing is not None
            and (
                existing.asset != project.asset
                or existing.quote_currency != project.quote_currency
            )
        )
        updated = replace(
            project,
            created_at=existing.created_at
            if existing is not None
            else project.created_at,
            updated_at=_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO user_projects(
                    id, owner_email, asset, status, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    updated.id,
                    updated.owner_email,
                    updated.asset,
                    updated.status,
                    updated.updated_at,
                    self._dump(updated.to_dict()),
                ),
            )
            if updated.status != "active" or scope_changed:
                self._disable_project_accounts_in_connection(
                    connection,
                    updated.id,
                )
                self._disable_project_strategies_in_connection(
                    connection,
                    updated.id,
                )
            connection.commit()
        return updated

    def set_project_status(self, project_id: str, status: str) -> UserProject:
        if status not in PROJECT_STATUSES:
            raise ValueError(f"unsupported project status: {status}")
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        return self.upsert_project(replace(project, status=status))

    def _disable_project_accounts_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> int:
        changed = 0
        rows = connection.execute(
            "SELECT payload FROM user_exchange_accounts WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        for row in rows:
            account = UserExchangeAccount.from_dict(json.loads(row["payload"]))
            if not account.enabled:
                continue
            updated = replace(account, enabled=False, updated_at=_now())
            connection.execute(
                """
                UPDATE user_exchange_accounts
                SET updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (updated.updated_at, self._dump(updated.to_dict()), updated.id),
            )
            changed += 1
        return changed

    def _disable_project_strategies_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> int:
        changed = 0
        rows = connection.execute(
            "SELECT payload FROM user_strategies WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        for row in rows:
            strategy = UserStrategy.from_dict(json.loads(row["payload"]))
            if not strategy.enabled:
                continue
            updated = replace(strategy, enabled=False, updated_at=_now())
            connection.execute(
                """
                UPDATE user_strategies
                SET updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (updated.updated_at, self._dump(updated.to_dict()), updated.id),
            )
            changed += 1
        return changed

    def _disable_account_strategies_in_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> int:
        changed = 0
        rows = connection.execute("SELECT payload FROM user_strategies").fetchall()
        for row in rows:
            strategy = UserStrategy.from_dict(json.loads(row["payload"]))
            if not strategy.enabled or account_id not in strategy.account_ids:
                continue
            updated = replace(strategy, enabled=False, updated_at=_now())
            connection.execute(
                """
                UPDATE user_strategies
                SET updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (updated.updated_at, self._dump(updated.to_dict()), updated.id),
            )
            changed += 1
        return changed

    @staticmethod
    def _strategies_using_account_in_connection(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> list[UserStrategy]:
        rows = connection.execute("SELECT payload FROM user_strategies").fetchall()
        return [
            strategy
            for row in rows
            for strategy in [UserStrategy.from_dict(json.loads(row["payload"]))]
            if account_id in strategy.account_ids
        ]

    def disable_project_accounts(self, project_id: str) -> int:
        """Disable every account under a project without touching credentials."""
        self._ensure()
        with self._connect() as connection:
            changed = self._disable_project_accounts_in_connection(
                connection,
                project_id,
            )
            connection.commit()
        return changed

    def delete_project(self, project_id: str) -> None:
        self._ensure()
        with self._connect() as connection:
            strategy_count = connection.execute(
                "SELECT COUNT(*) AS count FROM user_strategies WHERE project_id = ?",
                (project_id,),
            ).fetchone()["count"]
            if strategy_count:
                raise ValueError("delete project strategies first")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM user_exchange_accounts WHERE project_id = ?",
                (project_id,),
            ).fetchone()["count"]
            if count:
                raise ValueError("delete project exchange accounts first")
            connection.execute("DELETE FROM user_projects WHERE id = ?", (project_id,))
            connection.commit()

    @staticmethod
    def _clean_credentials(raw: Any) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {
            key: str(value).strip()
            for key, value in raw.items()
            if key in CREDENTIAL_FIELDS and str(value or "").strip()
        }

    def _credential_row(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM user_api_credentials WHERE account_id = ?",
            (account_id,),
        ).fetchone()

    def upsert_account(
        self,
        account: UserExchangeAccount,
        *,
        credentials: dict[str, Any] | None = None,
        replace_credentials: bool = False,
    ) -> UserExchangeAccount:
        self._ensure()
        project = self.get_project(account.project_id)
        if project is None:
            raise ValueError(f"project not found: {account.project_id}")
        if project.owner_email != account.owner_email:
            raise ValueError("project and exchange account owners must match")
        supplied = self._clean_credentials(credentials)
        existing = self.get_account(account.id)
        if existing is not None and existing.owner_email != account.owner_email:
            raise ValueError("exchange account owner cannot be changed")
        if not account.symbol:
            account = replace(account, symbol=project.symbol)
        if account.symbol.split("/", 1)[0] != project.asset:
            raise ValueError(
                f"account symbol base must match project asset {project.asset}"
            )
        exchange_changed = bool(
            existing is not None and existing.exchange != account.exchange
        )
        if exchange_changed:
            required = required_credentials_for_exchange(account.exchange)
            missing = sorted(required.difference(supplied))
            if missing:
                raise ValueError(
                    "re-enter API key / required credentials when changing exchange: "
                    + ", ".join(missing)
                )
            replace_credentials = True
        connection_changed = bool(
            existing is None
            or (
                supplied
                or existing.project_id != account.project_id
                or existing.exchange != account.exchange
                or existing.market_type != account.market_type
                or existing.api_variant != account.api_variant
                or existing.symbol != account.symbol
            )
        )
        updated = replace(
            account,
            created_at=existing.created_at
            if existing is not None
            else account.created_at,
            updated_at=_now(),
        )
        if connection_changed:
            updated = replace(
                updated,
                enabled=False,
                connection_status="unverified",
                connection_checked_at=None,
                connection_error="",
            )
        with self._connect() as connection:
            if existing is not None and existing.project_id != updated.project_id:
                referenced_by = self._strategies_using_account_in_connection(
                    connection,
                    updated.id,
                )
                if referenced_by:
                    raise ValueError(
                        "remove the account from its strategies before changing project"
                    )
            existing_credential = self._credential_row(connection, account.id)
            merged_credentials: dict[str, str] = {}
            if supplied:
                if not updated.withdrawal_disabled_confirmed:
                    raise ValueError(
                        "confirm that API withdrawal permission is disabled"
                    )
                if existing_credential is not None and not replace_credentials:
                    merged_credentials = self.cipher.decrypt(
                        account_id=account.id,
                        owner_email=account.owner_email,
                        nonce=existing_credential["nonce"],
                        ciphertext=existing_credential["ciphertext"],
                    )
                merged_credentials.update(supplied)
                required = required_credentials_for_exchange(account.exchange)
                missing = sorted(required.difference(merged_credentials))
                if missing:
                    raise ValueError(
                        "missing required API credential fields: " + ", ".join(missing)
                    )
                validate_exchange_credentials(account.exchange, merged_credentials)
                nonce, ciphertext = self.cipher.encrypt(
                    account_id=account.id,
                    owner_email=account.owner_email,
                    credentials=merged_credentials,
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO user_api_credentials(
                        account_id, owner_email, nonce, ciphertext, fields, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account.id,
                        account.owner_email,
                        nonce,
                        ciphertext,
                        self._dump({"fields": sorted(merged_credentials)}),
                        updated.updated_at,
                    ),
                )
            configured_fields = set(merged_credentials)
            if not configured_fields and existing_credential is not None:
                configured_fields = set(
                    json.loads(existing_credential["fields"]).get("fields") or []
                )
            if updated.enabled:
                if project.status != "active":
                    raise ValueError("project must be active before enabling account")
                if not updated.withdrawal_disabled_confirmed:
                    raise ValueError(
                        "confirm that API withdrawal permission is disabled"
                    )
                if not updated.trade_permission_confirmed:
                    raise ValueError("confirm that the API key has trading permission")
                if not self.cipher.available:
                    raise RuntimeError("credential encryption is not configured")
                required = required_credentials_for_exchange(updated.exchange)
                missing = sorted(required.difference(configured_fields))
                if missing:
                    raise ValueError(
                        "configure required API credentials before enabling account"
                    )
                if not account_connection_is_fresh(updated):
                    raise ValueError(
                        "run a successful account connection test before enabling"
                    )
            connection.execute(
                """
                INSERT OR REPLACE INTO user_exchange_accounts(
                    id, owner_email, project_id, exchange, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    updated.id,
                    updated.owner_email,
                    updated.project_id,
                    updated.exchange,
                    updated.updated_at,
                    self._dump(updated.to_dict()),
                ),
            )
            if not updated.enabled:
                self._disable_account_strategies_in_connection(
                    connection,
                    updated.id,
                )
            connection.commit()
        return updated

    def update_account_connection(
        self,
        account_id: str,
        *,
        status: str,
        error: str = "",
    ) -> UserExchangeAccount:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in CONNECTION_STATUSES:
            raise ValueError(f"unsupported connection status: {status}")
        account = self.get_account(account_id)
        if account is None:
            raise ValueError(f"exchange account not found: {account_id}")
        updated = replace(
            account,
            enabled=account.enabled if normalized_status == "healthy" else False,
            connection_status=normalized_status,
            connection_checked_at=_now(),
            connection_error=_clean_text(error, max_length=240),
        )
        return self.upsert_account(updated)

    def upsert_strategy(self, strategy: UserStrategy) -> UserStrategy:
        self._ensure()
        existing = self.get_strategy(strategy.id)
        if existing is not None and existing.owner_email != strategy.owner_email:
            raise ValueError("strategy owner cannot be changed")
        project = self.get_project(strategy.project_id)
        if project is None:
            raise ValueError(f"project not found: {strategy.project_id}")
        if project.owner_email != strategy.owner_email:
            raise ValueError("project and strategy owners must match")
        for account_id in strategy.account_ids:
            account = self.get_account(account_id)
            if account is None:
                raise ValueError(f"exchange account not found: {account_id}")
            if account.owner_email != strategy.owner_email:
                raise ValueError("strategy and exchange account owners must match")
            if account.project_id != strategy.project_id:
                raise ValueError(
                    "strategy accounts must belong to the selected project"
                )
        updated = replace(
            strategy,
            created_at=(
                existing.created_at if existing is not None else strategy.created_at
            ),
            updated_at=_now(),
        )
        if updated.enabled:
            readiness = self.strategy_readiness(updated)
            if not readiness["ready"]:
                raise ValueError(
                    "strategy cannot be enabled: " + "; ".join(readiness["blockers"])
                )
            profile = self.risk_profile(updated.owner_email)
            existing_strategies = [
                row
                for row in self.list_strategies(
                    owner_email=updated.owner_email,
                    is_admin=False,
                )
                if row.id != updated.id and row.enabled
            ]
            if (
                profile.max_active_strategies > 0
                and len(existing_strategies) + 1 > profile.max_active_strategies
            ):
                raise ValueError("user max active strategies limit would be exceeded")
            projected_exposure = float(
                updated.risk.get("max_total_quote") or 0.0
            ) + sum(
                float(row.risk.get("max_total_quote") or 0.0)
                for row in existing_strategies
            )
            if (
                profile.max_total_exposure_quote > 0
                and projected_exposure > profile.max_total_exposure_quote
            ):
                raise ValueError(
                    "user max total exposure quote limit would be exceeded"
                )
            projected_open_orders = int(updated.risk.get("max_open_orders") or 0) + sum(
                int(row.risk.get("max_open_orders") or 0) for row in existing_strategies
            )
            if (
                profile.max_open_orders > 0
                and projected_open_orders > profile.max_open_orders
            ):
                raise ValueError("user max open orders limit would be exceeded")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO user_strategies(
                    id, owner_email, project_id, strategy_type, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    updated.id,
                    updated.owner_email,
                    updated.project_id,
                    updated.strategy_type,
                    updated.updated_at,
                    self._dump(updated.to_dict()),
                ),
            )
            connection.commit()
        return updated

    def _account_readiness(
        self,
        account: UserExchangeAccount,
        *,
        project: UserProject | None,
        credential_status: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        project_active = project is not None and project.status == "active"
        vault_available = bool(credential_status.get("vault_available"))
        credentials_configured = bool(credential_status.get("configured"))
        withdrawal_disabled = account.withdrawal_disabled_confirmed
        trade_permission_confirmed = account.trade_permission_confirmed
        symbol_selected = bool(account.symbol)
        connection_fresh = account_connection_is_fresh(account, now=now)
        account_enabled = account.enabled
        steps = [
            {
                "id": "project_approved",
                "label": "Project approved",
                "complete": project_active,
            },
            {
                "id": "credential_vault_ready",
                "label": "Credential vault available",
                "complete": vault_available,
            },
            {
                "id": "withdrawal_disabled",
                "label": "Withdrawal permission disabled",
                "complete": withdrawal_disabled,
            },
            {
                "id": "trade_permission_confirmed",
                "label": "Trading permission confirmed",
                "complete": trade_permission_confirmed,
            },
            {
                "id": "credentials_saved",
                "label": "API credentials saved",
                "complete": credentials_configured,
            },
            {
                "id": "symbol_selected",
                "label": "Trading pair selected",
                "complete": symbol_selected,
            },
            {
                "id": "connection_test_fresh",
                "label": "Connection test passed",
                "complete": connection_fresh,
            },
            {
                "id": "account_enabled",
                "label": "Account enabled",
                "complete": account_enabled,
            },
        ]
        blockers: list[str] = []
        if project is None:
            blockers.append("project is unavailable")
        elif not project_active:
            blockers.append("project is not active")
        if not vault_available:
            blockers.append("credential vault is unavailable")
        if not withdrawal_disabled:
            blockers.append("withdrawal-disabled confirmation is missing")
        if not trade_permission_confirmed:
            blockers.append("trading permission confirmation is missing")
        if not credentials_configured:
            blockers.append("account credentials are missing")
        if not symbol_selected:
            blockers.append("account symbol is missing")
        if not connection_fresh:
            if account.connection_status == "error":
                detail = account.connection_error or "connection test failed"
                blockers.append(f"account connection error: {detail}")
            else:
                blockers.append("account connection test is missing or stale")
        if not account_enabled:
            blockers.append("account is disabled")

        if project is None or not project_active:
            next_action = {
                "code": "wait_for_project_approval",
                "label": "Wait for administrator approval",
            }
        elif not vault_available:
            next_action = {
                "code": "contact_administrator",
                "label": "Contact administrator about the credential vault",
            }
        elif not withdrawal_disabled:
            next_action = {
                "code": "confirm_withdrawal_disabled",
                "label": "Confirm withdrawal permission is disabled",
            }
        elif not trade_permission_confirmed:
            next_action = {
                "code": "confirm_trade_permission",
                "label": "Confirm the API key has trading permission",
            }
        elif not credentials_configured:
            next_action = {
                "code": "save_credentials",
                "label": "Save trade-only API credentials",
            }
        elif not symbol_selected:
            next_action = {
                "code": "select_symbol",
                "label": "Select a trading pair",
            }
        elif not connection_fresh:
            next_action = {
                "code": (
                    "fix_connection"
                    if account.connection_status == "error"
                    else "test_connection"
                ),
                "label": (
                    "Fix the connection error and test again"
                    if account.connection_status == "error"
                    else "Run the read-only connection test"
                ),
            }
        elif not account_enabled:
            next_action = {
                "code": "enable_account",
                "label": "Enable the exchange account",
            }
        else:
            next_action = {
                "code": "complete",
                "label": "Exchange account is ready",
            }

        completed_steps = sum(1 for step in steps if step["complete"])
        expires_at = (
            account.connection_checked_at + CONNECTION_MAX_AGE_SECONDS
            if account.connection_checked_at is not None
            and account.connection_status == "healthy"
            else None
        )
        return {
            "ready": not blockers,
            "status": "ready" if not blockers else str(next_action["code"]),
            "steps": steps,
            "completed_steps": completed_steps,
            "total_steps": len(steps),
            "progress_pct": round(completed_steps * 100.0 / len(steps), 1),
            "blockers": blockers,
            "next_action": next_action,
            "connection_expires_at": expires_at,
            "connection_remaining_seconds": (
                max(0.0, expires_at - now) if expires_at is not None else None
            ),
        }

    def account_readiness(
        self,
        account: UserExchangeAccount,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._account_readiness(
            account,
            project=self.get_project(account.project_id),
            credential_status=self.credential_status(account.id),
            now=now if now is not None else _now(),
        )

    def _strategy_readiness(
        self,
        strategy: UserStrategy,
        *,
        project: UserProject | None,
        accounts_by_id: dict[str, UserExchangeAccount],
        credential_statuses: dict[str, dict[str, Any]],
        now: float,
        risk_profile: UserRiskProfile | None = None,
    ) -> dict[str, Any]:
        blockers = list(strategy_parameter_blockers(strategy))
        warnings = ["paper mode only; live order submission is disabled"]
        definition = USER_STRATEGY_DEFINITIONS[strategy.strategy_type]
        if project is None:
            blockers.append("project is unavailable")
        elif project.status != "active":
            blockers.append("project is not active")
        elif project.owner_email != strategy.owner_email:
            blockers.append("project owner does not match strategy owner")

        account_count = len(strategy.account_ids)
        if account_count < int(definition["min_accounts"]):
            blockers.append(
                f"{strategy.strategy_type} requires at least "
                f"{definition['min_accounts']} account(s)"
            )
        if account_count > int(definition["max_accounts"]):
            blockers.append(
                f"{strategy.strategy_type} supports at most "
                f"{definition['max_accounts']} account(s)"
            )

        accounts: list[UserExchangeAccount] = []
        for account_id in strategy.account_ids:
            account = accounts_by_id.get(account_id)
            if account is None:
                blockers.append(f"account is unavailable: {account_id}")
                continue
            accounts.append(account)
            if account.owner_email != strategy.owner_email:
                blockers.append(f"account owner mismatch: {account.label}")
            if account.project_id != strategy.project_id:
                blockers.append(f"account project mismatch: {account.label}")
            if account.market_type != "spot":
                blockers.append(f"spot account required: {account.label}")
            if not account.symbol:
                blockers.append(f"account symbol is missing: {account.label}")
            elif (
                project is not None and account.symbol.split("/", 1)[0] != project.asset
            ):
                blockers.append(f"account symbol asset mismatch: {account.label}")
            elif (
                project is not None
                and strategy.strategy_type != "spot_spread"
                and account.symbol.split("/", 1)[1].split(":", 1)[0]
                != project.quote_currency
            ):
                blockers.append(f"account quote currency mismatch: {account.label}")
            if not account.enabled:
                blockers.append(f"account is disabled: {account.label}")
            if not account.withdrawal_disabled_confirmed:
                blockers.append(
                    f"withdrawal-disabled confirmation is missing: {account.label}"
                )
            if not account.trade_permission_confirmed:
                blockers.append(
                    f"trading permission confirmation is missing: {account.label}"
                )
            if not account_connection_is_fresh(account, now=now):
                blockers.append(f"account connection test is stale: {account.label}")
            credential_status = credential_statuses.get(account.id, {})
            if not credential_status.get("configured"):
                blockers.append(f"account credentials are missing: {account.label}")
            if not credential_status.get("vault_available"):
                blockers.append(f"credential vault is unavailable: {account.label}")

        if strategy.strategy_type == "spot_spread":
            exchanges = {account.exchange for account in accounts}
            if len(exchanges) < 2:
                blockers.append("spot arbitrage requires two different exchanges")

        profile = risk_profile or self.risk_profile(strategy.owner_email)
        if not profile.trading_enabled:
            blockers.append("user risk profile trading switch is disabled")

        unique_blockers = list(dict.fromkeys(blockers))
        return {
            "ready": not unique_blockers,
            "mode": "paper",
            "live_submit_allowed": False,
            "blockers": unique_blockers,
            "warnings": warnings,
        }

    def strategy_readiness(self, strategy: UserStrategy) -> dict[str, Any]:
        accounts = {
            account_id: account
            for account_id in strategy.account_ids
            for account in [self.get_account(account_id)]
            if account is not None
        }
        return self._strategy_readiness(
            strategy,
            project=self.get_project(strategy.project_id),
            accounts_by_id=accounts,
            credential_statuses=self.credential_statuses(strategy.account_ids),
            now=_now(),
            risk_profile=self.risk_profile(strategy.owner_email),
        )

    def _project_readiness(
        self,
        project: UserProject,
        *,
        accounts: list[UserExchangeAccount],
        strategies: list[UserStrategy],
        account_readiness: dict[str, dict[str, Any]],
        strategy_readiness: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        ready_accounts = [
            account
            for account in accounts
            if account_readiness.get(account.id, {}).get("ready")
        ]
        ready_strategies = [
            strategy
            for strategy in strategies
            if strategy_readiness.get(strategy.id, {}).get("ready")
        ]
        running_strategies = [
            strategy for strategy in ready_strategies if strategy.enabled
        ]
        steps = [
            {"id": "project_created", "label": "Project created", "complete": True},
            {
                "id": "project_approved",
                "label": "Project approved",
                "complete": project.status == "active",
            },
            {
                "id": "exchange_account_added",
                "label": "Exchange account added",
                "complete": bool(accounts),
            },
            {
                "id": "exchange_account_ready",
                "label": "Exchange account ready",
                "complete": bool(ready_accounts),
            },
            {
                "id": "strategy_created",
                "label": "Paper strategy created",
                "complete": bool(strategies),
            },
            {
                "id": "strategy_ready",
                "label": "Paper strategy checks passed",
                "complete": bool(ready_strategies),
            },
            {
                "id": "strategy_running",
                "label": "Paper strategy running",
                "complete": bool(running_strategies),
            },
        ]

        if project.status != "active":
            next_action = {
                "code": "wait_for_project_approval",
                "label": (
                    "Wait for administrator approval"
                    if project.status == "pending"
                    else "Ask an administrator to reactivate the project"
                ),
            }
        elif not accounts:
            next_action = {
                "code": "add_exchange_account",
                "label": "Add an exchange API account",
            }
        elif not ready_accounts:
            account = accounts[0]
            account_action = dict(
                account_readiness.get(account.id, {}).get("next_action") or {}
            )
            next_action = {
                "code": str(account_action.get("code") or "configure_account"),
                "label": str(
                    account_action.get("label") or "Configure the exchange account"
                ),
                "account_id": account.id,
            }
        elif not strategies:
            next_action = {
                "code": "create_strategy",
                "label": "Create a paper strategy",
            }
        elif not ready_strategies:
            next_action = {
                "code": "fix_strategy",
                "label": "Resolve the strategy blockers",
                "strategy_id": strategies[0].id,
            }
        elif not running_strategies:
            next_action = {
                "code": "enable_strategy",
                "label": "Enable the paper strategy",
                "strategy_id": ready_strategies[0].id,
            }
        else:
            next_action = {
                "code": "complete",
                "label": "Paper strategy is running",
                "strategy_id": running_strategies[0].id,
            }

        completed_steps = sum(1 for step in steps if step["complete"])
        return {
            "ready": bool(running_strategies),
            "mode": "paper",
            "live_submit_allowed": False,
            "status": "ready" if running_strategies else str(next_action["code"]),
            "steps": steps,
            "completed_steps": completed_steps,
            "total_steps": len(steps),
            "progress_pct": round(completed_steps * 100.0 / len(steps), 1),
            "next_action": next_action,
            "account_count": len(accounts),
            "ready_account_count": len(ready_accounts),
            "strategy_count": len(strategies),
            "ready_strategy_count": len(ready_strategies),
            "running_strategy_count": len(running_strategies),
        }

    def project_readiness(self, project: UserProject) -> dict[str, Any]:
        accounts = [
            account
            for account in self.list_accounts(
                owner_email=project.owner_email,
                is_admin=False,
            )
            if account.project_id == project.id
        ]
        strategies = [
            strategy
            for strategy in self.list_strategies(
                owner_email=project.owner_email,
                is_admin=False,
            )
            if strategy.project_id == project.id
        ]
        now = _now()
        credentials = self.credential_statuses([account.id for account in accounts])
        account_map = {account.id: account for account in accounts}
        account_readiness = {
            account.id: self._account_readiness(
                account,
                project=project,
                credential_status=credentials[account.id],
                now=now,
            )
            for account in accounts
        }
        strategy_readiness = {
            strategy.id: self._strategy_readiness(
                strategy,
                project=project,
                accounts_by_id=account_map,
                credential_statuses=credentials,
                now=now,
                risk_profile=self.risk_profile(project.owner_email),
            )
            for strategy in strategies
        }
        return self._project_readiness(
            project,
            accounts=accounts,
            strategies=strategies,
            account_readiness=account_readiness,
            strategy_readiness=strategy_readiness,
        )

    def delete_strategy(self, strategy_id: str) -> None:
        self._ensure()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM user_strategies WHERE id = ?", (strategy_id,)
            )
            connection.commit()

    def credential_statuses(
        self,
        account_ids: list[str] | tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        self._ensure()
        unique_ids = list(dict.fromkeys(str(item) for item in account_ids if item))
        default_status = {
            "configured": False,
            "storage": "encrypted",
            "fields": [],
            "updated_at": None,
            "vault_available": self.cipher.available,
            "rotation_due_at": None,
            "rotation_remaining_seconds": None,
            "rotation_required": False,
        }
        result = {account_id: dict(default_status) for account_id in unique_ids}
        if not unique_ids:
            return result
        placeholders = ",".join("?" for _ in unique_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_id, fields, updated_at "
                f"FROM user_api_credentials WHERE account_id IN ({placeholders})",
                tuple(unique_ids),
            ).fetchall()
        for row in rows:
            fields_payload = json.loads(row["fields"])
            fields = list(fields_payload.get("fields") or [])
            result[str(row["account_id"])] = {
                "configured": "api_key" in fields and "secret" in fields,
                "storage": "encrypted",
                "fields": fields,
                "updated_at": float(row["updated_at"]),
                "vault_available": self.cipher.available,
                "rotation_due_at": float(row["updated_at"])
                + CREDENTIAL_ROTATION_SECONDS,
                "rotation_remaining_seconds": max(
                    0.0,
                    float(row["updated_at"]) + CREDENTIAL_ROTATION_SECONDS - _now(),
                ),
                "rotation_required": (
                    _now() >= float(row["updated_at"]) + CREDENTIAL_ROTATION_SECONDS
                ),
            }
        return result

    def credential_status(self, account_id: str) -> dict[str, Any]:
        return self.credential_statuses([account_id])[account_id]

    def decrypt_credentials(
        self,
        *,
        account_id: str,
        owner_email: str,
    ) -> dict[str, str]:
        self._ensure()
        with self._connect() as connection:
            row = self._credential_row(connection, account_id)
        if row is None or row["owner_email"] != _clean_email(owner_email):
            raise ValueError("credentials not found")
        return self.cipher.decrypt(
            account_id=account_id,
            owner_email=row["owner_email"],
            nonce=row["nonce"],
            ciphertext=row["ciphertext"],
        )

    def delete_account(self, account_id: str) -> None:
        self._ensure()
        with self._connect() as connection:
            referenced_by = self._strategies_using_account_in_connection(
                connection,
                account_id,
            )
            if referenced_by:
                raise ValueError(
                    "delete or update strategies using this account first: "
                    + ", ".join(strategy.name for strategy in referenced_by[:5])
                )
            connection.execute(
                "DELETE FROM user_api_credentials WHERE account_id = ?",
                (account_id,),
            )
            connection.execute(
                "DELETE FROM user_exchange_accounts WHERE id = ?",
                (account_id,),
            )
            connection.commit()

    def purge_owner(self, owner_email: str) -> dict[str, int]:
        """Delete every workspace record owned by owner_email (account deletion)."""
        email = _clean_email(owner_email)
        self._ensure()
        removed: dict[str, int] = {}
        with self._connect() as connection:
            for table in (
                "user_wallet_challenges",
                "user_venue_connections",
                "user_wallet_connections",
                "user_api_credentials",
                "user_strategies",
                "user_exchange_accounts",
                "user_projects",
                "user_risk_profiles",
            ):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE owner_email = ?",  # noqa: S608
                    (email,),
                )
                removed[table] = cursor.rowcount
        return removed

    def reassign_owner(self, old_email: str, new_email: str) -> dict[str, int]:
        """Move workspace records to a new owner email (self-service change).

        Encrypted API credentials are bound to the owner email via their
        associated data and cannot be decrypted under the new identity, so
        they are deleted and their accounts disabled; the user re-enters the
        API key after the email change.
        """
        old = _clean_email(old_email)
        new = _clean_email(new_email)
        self._ensure()
        moved: dict[str, int] = {}
        with self._connect() as connection:
            dropped = connection.execute(
                "DELETE FROM user_api_credentials WHERE owner_email = ?",
                (old,),
            )
            moved["user_api_credentials_deleted"] = dropped.rowcount
            connection.execute(
                "DELETE FROM user_wallet_challenges WHERE owner_email = ?",
                (old,),
            )
            wallet_rows = connection.execute(
                "SELECT id, payload FROM user_wallet_connections WHERE owner_email = ?",
                (old,),
            ).fetchall()
            for row in wallet_rows:
                wallet = UserWalletConnection.from_dict(json.loads(row["payload"]))
                updated_wallet = replace(wallet, owner_email=new, last_seen_at=_now())
                connection.execute(
                    "UPDATE user_wallet_connections "
                    "SET owner_email = ?, updated_at = ?, payload = ? WHERE id = ?",
                    (
                        new,
                        updated_wallet.last_seen_at,
                        self._dump(updated_wallet.to_dict()),
                        updated_wallet.id,
                    ),
                )
            moved["user_wallet_connections"] = len(wallet_rows)
            venue_rows = connection.execute(
                "SELECT id, payload FROM user_venue_connections WHERE owner_email = ?",
                (old,),
            ).fetchall()
            for row in venue_rows:
                link = UserVenueConnection.from_dict(json.loads(row["payload"]))
                updated_link = replace(link, owner_email=new, checked_at=_now())
                connection.execute(
                    "UPDATE user_venue_connections "
                    "SET owner_email = ?, updated_at = ?, payload = ? WHERE id = ?",
                    (
                        new,
                        updated_link.checked_at,
                        self._dump(updated_link.to_dict()),
                        updated_link.id,
                    ),
                )
            moved["user_venue_connections"] = len(venue_rows)
            connection.execute(
                "UPDATE user_exchange_accounts SET owner_email = ? "
                "WHERE owner_email = ?",
                (new, old),
            )
            for table in ("user_strategies", "user_projects", "user_risk_profiles"):
                cursor = connection.execute(
                    f"UPDATE {table} SET owner_email = ? WHERE owner_email = ?",  # noqa: S608
                    (new, old),
                )
                moved[table] = cursor.rowcount
        return moved

    def public_payload(self, *, owner_email: str, is_admin: bool) -> dict[str, Any]:
        projects = self.list_projects(owner_email=owner_email, is_admin=is_admin)
        accounts, wallets, venue_connections = self._list_accounts_wallets_and_venues(
            owner_email=owner_email,
            is_admin=is_admin,
        )
        strategies = self.list_strategies(owner_email=owner_email, is_admin=is_admin)
        now = _now()
        risk_profile = self.risk_profile(owner_email)
        project_map = {project.id: project for project in projects}
        account_map = {account.id: account for account in accounts}
        credentials = self.credential_statuses([account.id for account in accounts])
        account_readiness = {
            account.id: self._account_readiness(
                account,
                project=project_map.get(account.project_id),
                credential_status=credentials[account.id],
                now=now,
            )
            for account in accounts
        }
        strategy_readiness = {
            strategy.id: self._strategy_readiness(
                strategy,
                project=project_map.get(strategy.project_id),
                accounts_by_id=account_map,
                credential_statuses=credentials,
                now=now,
                risk_profile=risk_profile,
            )
            for strategy in strategies
        }
        accounts_by_project: dict[str, list[UserExchangeAccount]] = {}
        for account in accounts:
            accounts_by_project.setdefault(account.project_id, []).append(account)
        strategies_by_project: dict[str, list[UserStrategy]] = {}
        for strategy in strategies:
            strategies_by_project.setdefault(strategy.project_id, []).append(strategy)

        project_rows = []
        for project in projects:
            row = project.to_dict()
            row["readiness"] = self._project_readiness(
                project,
                accounts=accounts_by_project.get(project.id, []),
                strategies=strategies_by_project.get(project.id, []),
                account_readiness=account_readiness,
                strategy_readiness=strategy_readiness,
            )
            project_rows.append(row)
        account_rows = []
        for account in accounts:
            row = account.to_dict()
            row["connection_fresh"] = account_connection_is_fresh(account, now=now)
            row["credentials"] = credentials[account.id]
            row["readiness"] = account_readiness[account.id]
            account_rows.append(row)
        strategy_rows = []
        for strategy in strategies:
            row = strategy.to_dict()
            readiness = strategy_readiness[strategy.id]
            row["readiness"] = readiness
            row["effective_enabled"] = strategy.enabled and readiness["ready"]
            row["status"] = (
                "paper_ready"
                if row["effective_enabled"]
                else "blocked"
                if strategy.enabled
                else "paused"
            )
            row["accounts"] = [
                {
                    "id": account.id,
                    "label": account.label,
                    "exchange": account.exchange,
                    "symbol": account.symbol,
                }
                for account_id in strategy.account_ids
                for account in [account_map.get(account_id)]
                if account is not None
            ]
            strategy_rows.append(row)
        completed_setup_steps = sum(
            int(row["readiness"]["completed_steps"]) for row in project_rows
        )
        total_setup_steps = sum(
            int(row["readiness"]["total_steps"]) for row in project_rows
        )
        next_project = next(
            (row for row in project_rows if not row["readiness"]["ready"]),
            project_rows[0] if project_rows else None,
        )
        venue_rows = [link.to_dict() for link in venue_connections]
        return {
            "status": "ok",
            "risk_profile": risk_profile.to_dict(),
            "projects": project_rows,
            "accounts": account_rows,
            "wallets": [wallet.to_dict() for wallet in wallets],
            "venue_connections": venue_rows,
            "strategies": strategy_rows,
            "exchange_catalog": exchange_catalog(),
            "dex_venue_catalog": dex_venue_catalog(),
            "strategy_catalog": user_strategy_catalog(),
            "vault_available": self.cipher.available,
            "summary": {
                "project_count": len(project_rows),
                "pending_project_count": sum(
                    1 for row in project_rows if row["status"] == "pending"
                ),
                "ready_project_count": sum(
                    1 for row in project_rows if row["readiness"]["ready"]
                ),
                "attention_project_count": sum(
                    1 for row in project_rows if not row["readiness"]["ready"]
                ),
                "setup_completed_steps": completed_setup_steps,
                "setup_total_steps": total_setup_steps,
                "setup_progress_pct": (
                    round(completed_setup_steps * 100.0 / total_setup_steps, 1)
                    if total_setup_steps
                    else 0.0
                ),
                "next_project_id": next_project["id"] if next_project else "",
                "next_action": (
                    dict(next_project["readiness"]["next_action"])
                    if next_project
                    else {
                        "code": "create_project",
                        "label": "Create your first trading project",
                    }
                ),
                "account_count": len(account_rows),
                "wallet_count": len(wallets),
                "venue_connection_count": len(venue_connections),
                "healthy_venue_connection_count": sum(
                    1 for row in venue_rows if row["read_only_verified"]
                ),
                "stale_venue_connection_count": sum(
                    1 for row in venue_rows if row["stale"]
                ),
                "error_venue_connection_count": sum(
                    1 for row in venue_rows if row["status"] == "error"
                ),
                "configured_account_count": sum(
                    1 for row in account_rows if row["credentials"]["configured"]
                ),
                "ready_account_count": sum(
                    1 for row in account_rows if row["readiness"]["ready"]
                ),
                "strategy_count": len(strategy_rows),
                "enabled_strategy_count": sum(
                    1 for row in strategy_rows if row["enabled"]
                ),
                "ready_strategy_count": sum(
                    1 for row in strategy_rows if row["effective_enabled"]
                ),
                "blocked_strategy_count": sum(
                    1
                    for row in strategy_rows
                    if row["enabled"] and not row["effective_enabled"]
                ),
            },
        }
