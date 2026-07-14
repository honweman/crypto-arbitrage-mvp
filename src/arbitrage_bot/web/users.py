from __future__ import annotations

import base64
import binascii
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

from ..user_workspace import CredentialCipher


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
    try:
        for step in range(-window, window + 1):
            candidate_time = now + step * TOTP_INTERVAL_SECONDS
            if hmac.compare_digest(supplied, totp_code(secret, for_time=candidate_time)):
                return True
    except (binascii.Error, TypeError, ValueError):
        return False
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
    def __init__(
        self,
        path: str | Path,
        *,
        master_key_env: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.master_key_env = master_key_env
        self.cipher = CredentialCipher(
            os.environ.get(master_key_env) if master_key_env else None
        )

    @staticmethod
    def _decode_encrypted_field(value: Any) -> bytes:
        encoded = str(value or "")
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii"))

    def _user_from_stored_dict(self, raw: dict[str, Any]) -> WebUser:
        values = dict(raw)
        ciphertext = values.get("totp_secret_ciphertext")
        nonce = values.get("totp_secret_nonce")
        if ciphertext or nonce:
            try:
                credentials = self.cipher.decrypt(
                    account_id="web-user-totp",
                    owner_email=normalize_email(str(values.get("email") or "")),
                    nonce=self._decode_encrypted_field(nonce),
                    ciphertext=self._decode_encrypted_field(ciphertext),
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                raise ValueError("web user TOTP secret could not be decrypted") from exc
            values["totp_secret"] = credentials.get("secret", "")
        return WebUser.from_dict(values)

    def _user_to_stored_dict(self, user: WebUser) -> dict[str, Any]:
        values = user.to_dict()
        if not self.cipher.available or not user.totp_secret:
            return values
        nonce, ciphertext = self.cipher.encrypt(
            account_id="web-user-totp",
            owner_email=user.email,
            credentials={"secret": user.totp_secret},
        )
        values.pop("totp_secret", None)
        values["totp_secret_nonce"] = base64.urlsafe_b64encode(nonce).decode(
            "ascii"
        )
        values["totp_secret_ciphertext"] = base64.urlsafe_b64encode(
            ciphertext
        ).decode("ascii")
        return values

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
            user = self._user_from_stored_dict(row)
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
            "users": [
                self._user_to_stored_dict(user)
                for user in sorted(users.values(), key=lambda item: item.email)
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def migrate_totp_secrets(self) -> bool:
        if not self.cipher.available or not self.path.exists():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read web user store: {exc}") from exc
        rows = raw.get("users", []) if isinstance(raw, dict) else []
        if isinstance(rows, dict):
            rows = list(rows.values())
        has_plaintext_secret = any(
            isinstance(row, dict) and bool(row.get("totp_secret"))
            for row in rows or []
        )
        if not has_plaintext_secret:
            return False
        self._write_users(self._read_users())
        return True

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
        if user.totp_enabled and not verify_totp(user.totp_secret, totp):
            return None
        return user

    def ensure_totp_secret(self, *, email: str) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        user = users.get(normalized_email)
        if user is None:
            raise ValueError("user is not registered")
        if user.totp_secret:
            return user
        updated = replace(
            user,
            totp_secret=generate_totp_secret(),
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

    def set_totp_enabled(
        self,
        *,
        email: str,
        password: str,
        code: str,
        enabled: bool,
    ) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        user = users.get(normalized_email)
        if user is None:
            raise ValueError("user is not registered")
        if not verify_password(password, user.password_hash):
            raise ValueError("current password is incorrect")
        if enabled == user.totp_enabled:
            state = "enabled" if enabled else "disabled"
            raise ValueError(f"authenticator is already {state}")
        secret = user.totp_secret or generate_totp_secret()
        if not verify_totp(secret, code):
            raise ValueError("authenticator code is invalid")
        updated = replace(
            user,
            totp_secret=secret if enabled else generate_totp_secret(),
            totp_enabled=enabled,
            auth_version=user.auth_version + 1,
            updated_at=time.time(),
        )
        users[updated.email] = updated
        self._write_users(users)
        return updated

    def reset_password(self, *, email: str, new_password: str) -> WebUser:
        normalized_email = normalize_email(email)
        users = self._read_users()
        user = users.get(normalized_email)
        if user is None:
            raise ValueError("user is not registered")
        updated = replace(
            user,
            password_hash=hash_password(new_password),
            totp_secret=generate_totp_secret(),
            totp_enabled=False,
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

    def delete_own_account(
        self,
        *,
        email: str,
        password: str,
        totp: str = "",
    ) -> None:
        """Delete the caller's own account after re-verifying credentials."""
        user = self.authenticate(email=email, password=password, totp=totp)
        if user is None:
            raise PermissionError("password confirmation failed")
        users = self._read_users()
        normalized_email = normalize_email(email)
        if normalized_email not in users:
            raise ValueError("user is not registered")
        self._require_not_last_admin(users, normalized_email)
        del users[normalized_email]
        self._write_users(users)

    def change_email(self, *, email: str, new_email: str) -> WebUser:
        """Move an account to a new (verified) email address.

        Bumps auth_version so existing sessions for the old identity stop
        validating; the user signs in again with the new address.
        """
        users = self._read_users()
        normalized_old = normalize_email(email)
        normalized_new = normalize_email(new_email)
        user = users.get(normalized_old)
        if user is None:
            raise ValueError("user is not registered")
        if normalized_new == normalized_old:
            raise ValueError("new email matches the current email")
        if normalized_new in users:
            raise ValueError("an account with the new email already exists")
        moved = replace(
            user,
            email=normalized_new,
            auth_version=max(1, int(user.auth_version or 1)) + 1,
        )
        del users[normalized_old]
        users[normalized_new] = moved
        self._write_users(users)
        return moved

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
        new_totp_secret = (
            generate_totp_secret()
            if new_password_hash != user.password_hash
            else user.totp_secret
        )
        new_totp_enabled = (
            False
            if new_password_hash != user.password_hash
            else user.totp_enabled
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
            totp_secret=new_totp_secret,
            totp_enabled=new_totp_enabled,
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
