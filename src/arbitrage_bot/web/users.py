from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote


PASSWORD_HASH_ITERATIONS = 260_000
TOTP_INTERVAL_SECONDS = 30
TOTP_DIGITS = 6
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("email is invalid")
    return email


def normalize_assets(values: Any) -> list[str]:
    if isinstance(values, str):
        raw_values = re.split(r"[\s,]+", values)
    else:
        raw_values = values or []
    assets = []
    seen = set()
    for raw in raw_values:
        asset = str(raw or "").strip().upper()
        if not asset or asset in seen:
            continue
        assets.append(asset)
        seen.add(asset)
    return assets


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "$".join(
        [
            "pbkdf2_sha256",
            str(PASSWORD_HASH_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_totp_secret(secret: str) -> bytes:
    normalized = str(secret or "").strip().replace(" ", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    return base64.b32decode((normalized + padding).encode("ascii"), casefold=True)


def totp_code(
    secret: str,
    *,
    for_time: float | None = None,
    interval_seconds: int = TOTP_INTERVAL_SECONDS,
    digits: int = TOTP_DIGITS,
) -> str:
    now = time.time() if for_time is None else for_time
    counter = int(now // interval_seconds)
    key = _decode_totp_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify_totp(
    secret: str,
    code: str,
    *,
    for_time: float | None = None,
    window: int = 1,
) -> bool:
    supplied = str(code or "").strip().replace(" ", "")
    if not supplied.isdigit():
        return False
    now = time.time() if for_time is None else for_time
    for step in range(-window, window + 1):
        candidate_time = now + step * TOTP_INTERVAL_SECONDS
        if hmac.compare_digest(supplied, totp_code(secret, for_time=candidate_time)):
            return True
    return False


def totp_provisioning_uri(
    *,
    email: str,
    secret: str,
    issuer: str,
) -> str:
    label = f"{issuer}:{email}"
    return (
        "otpauth://totp/"
        f"{quote(label)}?secret={quote(secret)}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_INTERVAL_SECONDS}"
    )


@dataclass(frozen=True)
class WebUser:
    email: str
    password_hash: str
    totp_secret: str
    totp_enabled: bool = True
    allowed_assets: list[str] = field(default_factory=list)
    preferred_asset: str = ""
    role: str = "user"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WebUser":
        return cls(
            email=normalize_email(str(raw.get("email", ""))),
            password_hash=str(raw.get("password_hash", "")),
            totp_secret=str(raw.get("totp_secret", "")),
            totp_enabled=bool(raw.get("totp_enabled", True)),
            allowed_assets=normalize_assets(raw.get("allowed_assets", [])),
            preferred_asset=str(raw.get("preferred_asset", "")).strip().upper(),
            role=str(raw.get("role", "user") or "user"),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "password_hash": self.password_hash,
            "totp_secret": self.totp_secret,
            "totp_enabled": self.totp_enabled,
            "allowed_assets": self.allowed_assets,
            "preferred_asset": self.preferred_asset,
            "role": self.role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self, *, available_assets: list[str] | None = None) -> dict[str, Any]:
        return {
            "email": self.email,
            "role": self.role,
            "totp_enabled": self.totp_enabled,
            "allowed_assets": self.allowed_assets,
            "preferred_asset": self.preferred_asset,
            "available_assets": available_assets or [],
        }


class WebUserStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_users(self) -> dict[str, WebUser]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read web user store: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("web user store must be a JSON object")
        rows = raw.get("users", [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        users: dict[str, WebUser] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            user = WebUser.from_dict(row)
            users[user.email] = user
        return users

    def _write_users(self, users: dict[str, WebUser]) -> None:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "users": [user.to_dict() for user in sorted(users.values(), key=lambda item: item.email)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def has_users(self) -> bool:
        return bool(self._read_users())

    def get_user(self, email: str) -> WebUser | None:
        return self._read_users().get(normalize_email(email))

    def create_user(
        self,
        *,
        email: str,
        password: str,
        allowed_assets: list[str] | str | None = None,
        preferred_asset: str = "",
    ) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        if normalized_email in users:
            raise ValueError("email is already registered")
        assets = normalize_assets(allowed_assets or [])
        preferred = str(preferred_asset or "").strip().upper()
        if preferred and assets and preferred not in assets:
            raise ValueError("preferred asset must be in allowed assets")
        user = WebUser(
            email=normalized_email,
            password_hash=hash_password(password),
            totp_secret=generate_totp_secret(),
            allowed_assets=assets,
            preferred_asset=preferred or (assets[0] if len(assets) == 1 else ""),
            role="admin" if not users else "user",
        )
        users[user.email] = user
        self._write_users(users)
        return user

    def authenticate(self, *, email: str, password: str, totp: str) -> WebUser | None:
        user = self.get_user(email)
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        if user.totp_enabled and not verify_totp(user.totp_secret, totp):
            return None
        return user

    def update_profile(
        self,
        *,
        email: str,
        preferred_asset: str,
    ) -> WebUser:
        users = self._read_users()
        user = users.get(normalize_email(email))
        if user is None:
            raise ValueError("user is not registered")
        preferred = str(preferred_asset or "").strip().upper()
        if preferred and user.allowed_assets and preferred not in user.allowed_assets:
            raise ValueError("preferred asset is not allowed for this user")
        updated = WebUser(
            email=user.email,
            password_hash=user.password_hash,
            totp_secret=user.totp_secret,
            totp_enabled=user.totp_enabled,
            allowed_assets=user.allowed_assets,
            preferred_asset=preferred,
            role=user.role,
            created_at=user.created_at,
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

    def list_users(self) -> list[WebUser]:
        return sorted(self._read_users().values(), key=lambda item: item.email)

    def admin_create_user(
        self,
        *,
        email: str,
        password: str,
        role: str = "user",
        allowed_assets: list[str] | str | None = None,
        preferred_asset: str = "",
    ) -> WebUser:
        normalized_role = str(role or "user").strip().lower()
        if normalized_role not in {"admin", "user"}:
            raise ValueError("role must be admin or user")
        normalized_email = normalize_email(email)
        users = self._read_users()
        if normalized_email in users:
            raise ValueError("email is already registered")
        assets = normalize_assets(allowed_assets or [])
        preferred = str(preferred_asset or "").strip().upper()
        if preferred and assets and preferred not in assets:
            raise ValueError("preferred asset must be in allowed assets")
        user = WebUser(
            email=normalized_email,
            password_hash=hash_password(password),
            totp_secret=generate_totp_secret(),
            allowed_assets=assets,
            preferred_asset=preferred or (assets[0] if len(assets) == 1 else ""),
            role=normalized_role,
        )
        users[user.email] = user
        self._write_users(users)
        return user

    def _require_not_last_admin(self, users: dict[str, WebUser], email: str) -> None:
        admin_count = sum(1 for item in users.values() if item.role == "admin")
        if admin_count <= 1 and users[email].role == "admin":
            raise ValueError("cannot remove the last remaining admin")

    def admin_update_user(
        self,
        *,
        email: str,
        role: str | None = None,
        allowed_assets: list[str] | str | None = None,
        allowed_assets_provided: bool = False,
        preferred_asset: str | None = None,
        preferred_asset_provided: bool = False,
        new_password: str | None = None,
    ) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        user = users.get(normalized_email)
        if user is None:
            raise ValueError("user is not registered")

        new_role = user.role
        if role is not None:
            new_role = str(role or "").strip().lower()
            if new_role not in {"admin", "user"}:
                raise ValueError("role must be admin or user")
        if new_role != "admin":
            self._require_not_last_admin(users, normalized_email)

        new_assets = (
            normalize_assets(allowed_assets or [])
            if allowed_assets_provided
            else user.allowed_assets
        )
        if preferred_asset_provided:
            new_preferred = str(preferred_asset or "").strip().upper()
        else:
            new_preferred = user.preferred_asset
            if allowed_assets_provided and new_preferred not in new_assets:
                new_preferred = ""
        if new_preferred and new_assets and new_preferred not in new_assets:
            raise ValueError("preferred asset must be in allowed assets")

        new_password_hash = user.password_hash
        if new_password:
            new_password_hash = hash_password(new_password)

        if (
            new_role == user.role
            and new_assets == user.allowed_assets
            and new_preferred == user.preferred_asset
            and new_password_hash == user.password_hash
        ):
            raise ValueError("no changes supplied")

        updated = replace(
            user,
            role=new_role,
            allowed_assets=new_assets,
            preferred_asset=new_preferred,
            password_hash=new_password_hash,
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

    def admin_delete_user(self, *, email: str) -> None:
        users = self._read_users()
        normalized_email = normalize_email(email)
        if normalized_email not in users:
            raise ValueError("user is not registered")
        self._require_not_last_admin(users, normalized_email)
        del users[normalized_email]
        self._write_users(users)

