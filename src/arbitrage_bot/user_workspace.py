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

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PROJECT_STATUSES = {"pending", "active", "disabled"}
MARKET_TYPES = {"spot", "swap", "future"}
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ASSET_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,19}$")
CREDENTIAL_FIELDS = {"api_key", "secret", "passphrase", "password"}

EXCHANGE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "coinbase",
        "label": "Coinbase",
        "market_types": ["spot"],
        "required_credentials": ["api_key", "secret"],
    },
    {
        "id": "bithumb",
        "label": "Bithumb",
        "market_types": ["spot"],
        "required_credentials": ["api_key", "secret"],
    },
    {
        "id": "upbit",
        "label": "Upbit",
        "market_types": ["spot"],
        "required_credentials": ["api_key", "secret"],
    },
    {
        "id": "bybit",
        "label": "Bybit",
        "market_types": ["spot", "swap"],
        "required_credentials": ["api_key", "secret"],
    },
    {
        "id": "binance",
        "label": "Binance",
        "market_types": ["spot", "swap"],
        "required_credentials": ["api_key", "secret"],
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


def _clean_asset(value: Any, *, label: str) -> str:
    result = str(value or "").strip().upper()
    if not ASSET_RE.fullmatch(result):
        raise ValueError(f"{label} must be 2-20 letters, numbers, '.', '_' or '-'")
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
    enabled: bool = False
    withdrawal_disabled_confirmed: bool = False
    connection_status: str = "unverified"
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
        project_id = _clean_id(raw.get("project_id"), prefix="project")
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), prefix="account"),
            owner_email=_clean_email(raw.get("owner_email")),
            project_id=project_id,
            label=_clean_text(raw.get("label") or exchange_row["label"]),
            exchange=exchange,
            market_type=market_type,
            enabled=bool(raw.get("enabled", False)),
            withdrawal_disabled_confirmed=bool(
                raw.get("withdrawal_disabled_confirmed", False)
            ),
            connection_status=_clean_text(
                raw.get("connection_status") or "unverified",
                max_length=32,
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
            "enabled": self.enabled,
            "withdrawal_disabled_confirmed": self.withdrawal_disabled_confirmed,
            "connection_status": self.connection_status,
            "created_at": self.created_at,
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
        plaintext = AESGCM(self._key).decrypt(
            nonce,
            ciphertext,
            self._associated_data(account_id, owner_email),
        )
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
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._ready = True

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
        return [
            UserExchangeAccount.from_dict(json.loads(row["payload"])) for row in rows
        ]

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
        return UserExchangeAccount.from_dict(json.loads(row["payload"])) if row else None

    def upsert_project(self, project: UserProject) -> UserProject:
        self._ensure()
        updated = replace(project, updated_at=_now())
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
            if updated.status != "active":
                self._disable_project_accounts_in_connection(
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
        updated = replace(account, updated_at=_now())
        supplied = self._clean_credentials(credentials)
        with self._connect() as connection:
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
            connection.commit()
        return updated

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
        project_rows = [project.to_dict() for project in projects]
        account_rows = []
        for account in accounts:
            row = account.to_dict()
            row["credentials"] = self.credential_status(account.id)
            account_rows.append(row)
        return {
            "status": "ok",
            "projects": project_rows,
            "accounts": account_rows,
            "exchange_catalog": exchange_catalog(),
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
            },
        }
