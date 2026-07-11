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

from .user_strategies import (
    USER_STRATEGY_DEFINITIONS,
    UserStrategy,
    strategy_parameter_blockers,
    user_strategy_catalog,
)


PROJECT_STATUSES = {"pending", "active", "disabled"}
MARKET_TYPES = {"spot", "swap", "future"}
CONNECTION_STATUSES = {"unverified", "healthy", "error"}
CONNECTION_MAX_AGE_SECONDS = 86_400.0
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
)
EXCHANGES_BY_ID = {row["id"]: row for row in EXCHANGE_CATALOG}


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
        raise ValueError("account symbol must use BASE/QUOTE or BASE/QUOTE:SETTLE format")
    return result


def exchange_catalog() -> list[dict[str, Any]]:
    return [dict(row) for row in EXCHANGE_CATALOG]


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
        if market_type not in MARKET_TYPES or market_type not in exchange_row["market_types"]:
            raise ValueError(f"{exchange} does not support {market_type} accounts")
        variants = {
            str(item.get("id") or "")
            for item in exchange_row.get("variants", [])
            if isinstance(item, dict)
        }
        api_variant = str(
            raw.get("api_variant") or exchange_row.get("default_variant") or "default"
        ).strip().lower()
        if api_variant not in variants:
            raise ValueError(f"{exchange} does not support API variant {api_variant}")
        connection_status = str(
            raw.get("connection_status") or "unverified"
        ).strip().lower()
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
            created_at=existing.created_at if existing is not None else project.created_at,
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
            required = set(EXCHANGES_BY_ID[account.exchange]["required_credentials"])
            missing = sorted(required.difference(supplied))
            if missing:
                raise ValueError("re-enter API key and secret when changing exchange")
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
            created_at=existing.created_at if existing is not None else account.created_at,
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
                    raise ValueError("confirm that API withdrawal permission is disabled")
                if existing_credential is not None and not replace_credentials:
                    merged_credentials = self.cipher.decrypt(
                        account_id=account.id,
                        owner_email=account.owner_email,
                        nonce=existing_credential["nonce"],
                        ciphertext=existing_credential["ciphertext"],
                    )
                merged_credentials.update(supplied)
                required = set(EXCHANGES_BY_ID[account.exchange]["required_credentials"])
                missing = sorted(required.difference(merged_credentials))
                if missing:
                    raise ValueError(
                        "missing required API credential fields: " + ", ".join(missing)
                    )
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
                if not self.cipher.available:
                    raise RuntimeError("credential encryption is not configured")
                required = set(
                    EXCHANGES_BY_ID[updated.exchange]["required_credentials"]
                )
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
                raise ValueError("strategy accounts must belong to the selected project")
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
                    "strategy cannot be enabled: "
                    + "; ".join(readiness["blockers"])
                )
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

    def strategy_readiness(self, strategy: UserStrategy) -> dict[str, Any]:
        blockers = list(strategy_parameter_blockers(strategy))
        warnings = ["paper mode only; live order submission is disabled"]
        project = self.get_project(strategy.project_id)
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
            account = self.get_account(account_id)
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
            elif project is not None and account.symbol.split("/", 1)[0] != project.asset:
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
            if not account_connection_is_fresh(account):
                blockers.append(f"account connection test is stale: {account.label}")
            credential_status = self.credential_status(account.id)
            if not credential_status["configured"]:
                blockers.append(f"account credentials are missing: {account.label}")
            if not credential_status["vault_available"]:
                blockers.append(f"credential vault is unavailable: {account.label}")

        if strategy.strategy_type == "spot_spread":
            exchanges = {account.exchange for account in accounts}
            if len(exchanges) < 2:
                blockers.append("spot arbitrage requires two different exchanges")

        unique_blockers = list(dict.fromkeys(blockers))
        return {
            "ready": not unique_blockers,
            "mode": "paper",
            "live_submit_allowed": False,
            "blockers": unique_blockers,
            "warnings": warnings,
        }

    def delete_strategy(self, strategy_id: str) -> None:
        self._ensure()
        with self._connect() as connection:
            connection.execute("DELETE FROM user_strategies WHERE id = ?", (strategy_id,))
            connection.commit()

    def credential_status(self, account_id: str) -> dict[str, Any]:
        self._ensure()
        with self._connect() as connection:
            row = self._credential_row(connection, account_id)
        if row is None:
            return {
                "configured": False,
                "storage": "encrypted",
                "fields": [],
                "updated_at": None,
                "vault_available": self.cipher.available,
            }
        fields_payload = json.loads(row["fields"])
        fields = list(fields_payload.get("fields") or [])
        return {
            "configured": "api_key" in fields and "secret" in fields,
            "storage": "encrypted",
            "fields": fields,
            "updated_at": float(row["updated_at"]),
            "vault_available": self.cipher.available,
        }

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

    def public_payload(self, *, owner_email: str, is_admin: bool) -> dict[str, Any]:
        projects = self.list_projects(owner_email=owner_email, is_admin=is_admin)
        accounts = self.list_accounts(owner_email=owner_email, is_admin=is_admin)
        strategies = self.list_strategies(owner_email=owner_email, is_admin=is_admin)
        project_rows = [project.to_dict() for project in projects]
        account_rows = []
        for account in accounts:
            row = account.to_dict()
            row["connection_fresh"] = account_connection_is_fresh(account)
            row["credentials"] = self.credential_status(account.id)
            account_rows.append(row)
        account_map = {account.id: account for account in accounts}
        strategy_rows = []
        for strategy in strategies:
            row = strategy.to_dict()
            readiness = self.strategy_readiness(strategy)
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
        return {
            "status": "ok",
            "projects": project_rows,
            "accounts": account_rows,
            "strategies": strategy_rows,
            "exchange_catalog": exchange_catalog(),
            "strategy_catalog": user_strategy_catalog(),
            "vault_available": self.cipher.available,
            "summary": {
                "project_count": len(project_rows),
                "pending_project_count": sum(
                    1 for row in project_rows if row["status"] == "pending"
                ),
                "account_count": len(account_rows),
                "configured_account_count": sum(
                    1 for row in account_rows if row["credentials"]["configured"]
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
