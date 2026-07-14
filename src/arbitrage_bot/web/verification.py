from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass

from ..alerts import _send_email
from ..config import AlertConfig
from .users import normalize_email


VERIFICATION_PURPOSES = {"register", "password_reset", "change_email"}


class VerificationRateLimited(ValueError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(1.0, retry_after)
        super().__init__(
            f"please wait {int(self.retry_after + 0.999)} seconds before requesting another code"
        )


@dataclass
class _VerificationChallenge:
    digest: str
    expires_at: float
    next_send_at: float
    attempts: int = 0


class EmailVerificationManager:
    """Short-lived email codes with resend, attempt, and hourly limits."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        resend_seconds: float = 60.0,
        max_attempts: int = 5,
        max_sends_per_hour: int = 5,
    ) -> None:
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.resend_seconds = max(10.0, float(resend_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.max_sends_per_hour = max(1, int(max_sends_per_hour))
        self._secret = secrets.token_bytes(32)
        self._challenges: dict[tuple[str, str], _VerificationChallenge] = {}
        self._send_history: dict[str, deque[float]] = {}

    def _digest(self, *, purpose: str, email: str, code: str) -> str:
        payload = f"{purpose}:{email}:{code}".encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _normalize_purpose(purpose: str) -> str:
        normalized = str(purpose or "").strip().lower()
        if normalized not in VERIFICATION_PURPOSES:
            raise ValueError("unsupported verification purpose")
        return normalized

    def issue(
        self,
        *,
        email: str,
        purpose: str,
        client_key: str = "",
        now: float | None = None,
    ) -> str:
        current = time.time() if now is None else float(now)
        normalized_email = normalize_email(email)
        normalized_purpose = self._normalize_purpose(purpose)
        key = (normalized_purpose, normalized_email)
        existing = self._challenges.get(key)
        if existing is not None and current < existing.next_send_at:
            raise VerificationRateLimited(existing.next_send_at - current)

        history_keys = [f"email:{normalized_email}"]
        if client_key:
            history_keys.append(f"client:{client_key}")
        for history_key in history_keys:
            bucket = self._send_history.setdefault(history_key, deque())
            cutoff = current - 3600.0
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_sends_per_hour:
                raise VerificationRateLimited(bucket[0] + 3600.0 - current)

        code = str(secrets.randbelow(1_000_000)).zfill(6)
        self._challenges[key] = _VerificationChallenge(
            digest=self._digest(
                purpose=normalized_purpose,
                email=normalized_email,
                code=code,
            ),
            expires_at=current + self.ttl_seconds,
            next_send_at=current + self.resend_seconds,
        )
        for history_key in history_keys:
            self._send_history[history_key].append(current)
        return code

    def discard(self, *, email: str, purpose: str) -> None:
        normalized_email = normalize_email(email)
        normalized_purpose = self._normalize_purpose(purpose)
        self._challenges.pop((normalized_purpose, normalized_email), None)

    def verify(
        self,
        *,
        email: str,
        purpose: str,
        code: str,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        normalized_email = normalize_email(email)
        normalized_purpose = self._normalize_purpose(purpose)
        key = (normalized_purpose, normalized_email)
        challenge = self._challenges.get(key)
        supplied = str(code or "").strip().replace(" ", "")
        if challenge is None or current > challenge.expires_at:
            self._challenges.pop(key, None)
            return False
        if len(supplied) != 6 or not supplied.isdigit():
            matched = False
        else:
            matched = hmac.compare_digest(
                challenge.digest,
                self._digest(
                    purpose=normalized_purpose,
                    email=normalized_email,
                    code=supplied,
                ),
            )
        if matched:
            self._challenges.pop(key, None)
            return True
        challenge.attempts += 1
        if challenge.attempts >= self.max_attempts:
            self._challenges.pop(key, None)
        return False


def _env_value(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    return value if value else None


class VerificationEmailSender:
    def __init__(self, cfg: AlertConfig) -> None:
        self.cfg = cfg

    def configured(self) -> bool:
        host = _env_value(self.cfg.smtp_host_env)
        username = _env_value(self.cfg.smtp_username_env)
        sender = _env_value(self.cfg.email_from_env) or username
        return bool(host and sender)

    async def send_code(self, *, email: str, code: str, purpose: str) -> None:
        if not self.configured():
            raise RuntimeError("email verification service is not configured")
        normalized_email = normalize_email(email)
        host = _env_value(self.cfg.smtp_host_env)
        username = _env_value(self.cfg.smtp_username_env)
        password = _env_value(self.cfg.smtp_password_env)
        sender = _env_value(self.cfg.email_from_env) or username
        port = int(_env_value(self.cfg.smtp_port_env) or "587")
        if not host or not sender:
            raise RuntimeError("email verification service is not configured")
        if purpose == "register":
            subject = "Crypto Trading registration code"
            action = "complete your account registration"
        elif purpose == "change_email":
            subject = "Crypto Trading email change code"
            action = "confirm your new account email"
        else:
            subject = "Crypto Trading password reset code"
            action = "reset your account password"
        body = (
            f"Your verification code is: {code}\n\n"
            f"Use this code to {action}. The code expires shortly.\n"
            "If you did not request this code, ignore this email.\n\n"
            f"验证码：{code}\n"
            "验证码将在短时间内失效，请勿转发给任何人。"
        )
        await asyncio.to_thread(
            _send_email,
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=self.cfg.smtp_tls,
            sender=sender,
            recipients=[normalized_email],
            subject=subject,
            body=body,
        )
