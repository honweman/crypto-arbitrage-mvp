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
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")


def normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("email is invalid")
    return email


def normalize_username(value: str) -> str:
    username = str(value or "").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(
            "username must be 3-32 characters using letters, numbers, '.', '_' or '-'"
        )
    return username


def default_username_for_email(email: str) -> str:
    local_part = normalize_email(email).split("@", 1)[0].lower()
    username = re.sub(r"[^a-z0-9_.-]+", "-", local_part).strip("._-")
    if len(username) < 3:
        username = f"user-{username or 'account'}"
    return normalize_username(username[:32])


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


def validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8:
        raise ValueError("password must be at least 8 characters")
    if not any(char.isalpha() for char in value):
        raise ValueError("password must include at least one letter")
    if not any(char.isdigit() for char in value):
        raise ValueError("password must include at least one number")
    if not any(not char.isalnum() and not char.isspace() for char in value):
        raise ValueError("password must include at least one special character")
    return value


def hash_password(password: str) -> str:
    password = validate_password(password)
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
    username: str
    password_hash: str
    totp_secret: str
    totp_enabled: bool = False
    allowed_assets: list[str] = field(default_factory=list)
    preferred_asset: str = ""
    role: str = "user"
    auth_version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WebUser":
        email = normalize_email(str(raw.get("email", "")))
        return cls(
            email=email,
            username=normalize_username(
                str(raw.get("username") or default_username_for_email(email))
            ),
            password_hash=str(raw.get("password_hash", "")),
            totp_secret=str(raw.get("totp_secret", "")),
            totp_enabled=bool(raw.get("totp_enabled", False)),
            allowed_assets=normalize_assets(raw.get("allowed_assets", [])),
            preferred_asset=str(raw.get("preferred_asset", "")).strip().upper(),
            role=str(raw.get("role", "user") or "user"),
            auth_version=max(1, int(raw.get("auth_version") or 1)),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "username": self.username,
            "password_hash": self.password_hash,
            "totp_secret": self.totp_secret,
            "totp_enabled": self.totp_enabled,
            "allowed_assets": self.allowed_assets,
            "preferred_asset": self.preferred_asset,
            "role": self.role,
            "auth_version": self.auth_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self, *, available_assets: list[str] | None = None) -> dict[str, Any]:
        return {
            "email": self.email,
            "username": self.username,
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
        usernames: set[str] = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            user = WebUser.from_dict(row)
            if user.username in usernames:
                base = user.username[:27]
                suffix = 2
                candidate = f"{base}-{suffix}"
                while candidate in usernames:
                    suffix += 1
                    candidate = f"{base}-{suffix}"
                user = replace(user, username=candidate)
            usernames.add(user.username)
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

    def get_user_by_username(self, username: str) -> WebUser | None:
        normalized = normalize_username(username)
        return next(
            (
                user
                for user in self._read_users().values()
                if hmac.compare_digest(user.username, normalized)
            ),
            None,
        )

    @staticmethod
    def _require_unique_username(
        users: dict[str, WebUser],
        username: str,
        *,
        exclude_email: str = "",
    ) -> None:
        for user in users.values():
            if user.email != exclude_email and hmac.compare_digest(
                user.username,
                username,
            ):
                raise ValueError("username is already registered")

    def create_user(
        self,
        *,
        email: str,
        username: str = "",
        password: str,
        allowed_assets: list[str] | str | None = None,
        preferred_asset: str = "",
    ) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        if normalized_email in users:
            raise ValueError("email is already registered")
        normalized_username = normalize_username(
            username or default_username_for_email(normalized_email)
        )
        self._require_unique_username(users, normalized_username)
        assets = normalize_assets(allowed_assets or [])
        preferred = str(preferred_asset or "").strip().upper()
        if preferred and assets and preferred not in assets:
            raise ValueError("preferred asset must be in allowed assets")
        user = WebUser(
            email=normalized_email,
            username=normalized_username,
            password_hash=hash_password(password),
            totp_secret=generate_totp_secret(),
            allowed_assets=assets,
            preferred_asset=preferred or (assets[0] if len(assets) == 1 else ""),
            role="admin" if not users else "user",
        )
        users[user.email] = user
        self._write_users(users)
        return user

    def authenticate(
        self,
        *,
        username: str = "",
        password: str,
        email: str = "",
        totp: str = "",
    ) -> WebUser | None:
        # Email and TOTP remain accepted by the Python API for legacy callers,
        # while the dashboard now authenticates with username and password.
        _ = totp
        try:
            user = (
                self.get_user_by_username(username)
                if username
                else self.get_user(email)
            )
        except ValueError:
            return None
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def reset_password(self, *, email: str, new_password: str) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        user = users.get(normalized_email)
        if user is None:
            raise ValueError("user is not registered")
        updated = replace(
            user,
            password_hash=hash_password(new_password),
            auth_version=user.auth_version + 1,
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

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
            username=user.username,
            password_hash=user.password_hash,
            totp_secret=user.totp_secret,
            totp_enabled=user.totp_enabled,
            allowed_assets=user.allowed_assets,
            preferred_asset=preferred,
            role=user.role,
            auth_version=user.auth_version,
            created_at=user.created_at,
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

    def list_users(self) -> list[WebUser]:
        return sorted(self._read_users().values(), key=lambda item: item.email)

    def admin_grant_asset(self, *, email: str, asset: str) -> WebUser:
        normalized_email = normalize_email(email)
        normalized_asset = str(asset or "").strip().upper()
        if not normalized_asset:
            raise ValueError("asset is required")
        users = self._read_users()
        user = users.get(normalized_email)
        if user is None:
            raise ValueError("user is not registered")
        assets = list(user.allowed_assets)
        if normalized_asset not in assets:
            assets.append(normalized_asset)
        updated = replace(
            user,
            allowed_assets=assets,
            preferred_asset=user.preferred_asset or normalized_asset,
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

    def admin_create_user(
        self,
        *,
        email: str,
        username: str = "",
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
        normalized_username = normalize_username(
            username or default_username_for_email(normalized_email)
        )
        self._require_unique_username(users, normalized_username)
        assets = normalize_assets(allowed_assets or [])
        preferred = str(preferred_asset or "").strip().upper()
        if preferred and assets and preferred not in assets:
            raise ValueError("preferred asset must be in allowed assets")
        user = WebUser(
            email=normalized_email,
            username=normalized_username,
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
        username: str | None = None,
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

        new_username = user.username
        if username is not None:
            new_username = normalize_username(username)
            self._require_unique_username(
                users,
                new_username,
                exclude_email=normalized_email,
            )

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
        new_auth_version = (
            user.auth_version + 1
            if new_password_hash != user.password_hash
            else user.auth_version
        )

        if (
            new_role == user.role
            and new_username == user.username
            and new_assets == user.allowed_assets
            and new_preferred == user.preferred_asset
            and new_password_hash == user.password_hash
        ):
            raise ValueError("no changes supplied")

        updated = replace(
            user,
            role=new_role,
            username=new_username,
            allowed_assets=new_assets,
            preferred_asset=new_preferred,
            password_hash=new_password_hash,
            auth_version=new_auth_version,
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
