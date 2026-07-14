"""Security, session, auth, and audit layer for the web dashboard.

Extracted from ``web/__init__.py``: session cookies and signing, the login
rate limiter, IP allowlisting helpers, the security and performance
middlewares, the login/register/password-reset handlers, per-request
store accessors, and the web audit event log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import web

from .auth_views import (
    forgot_password_html as _forgot_password_html,
    login_html as _login_html,
    register_html as _register_html,
    security_html as _security_html,
)
from .users import (
    WebUser,
    WebUserStore,
    normalize_email,
    normalize_username,
    totp_provisioning_uri,
    validate_password,
)
from .verification import (
    EmailVerificationManager,
    VerificationEmailSender,
    VerificationRateLimited,
)
from ..config import BotConfig
from ..jsonl_rotation import rotate_jsonl_log_if_needed
from ..strategy_center import StrategyCenterStore
from ..user_account_check import (
    WorkspaceAccountCheckService,
    WorkspaceMarketDiscoveryService,
)
from ..user_paper_store import UserPaperTradingStore
from ..user_workspace import UserWorkspaceStore


SESSION_COOKIE = "crypto_arb_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}

# Brute-force protection for the login endpoint.
LOGIN_MAX_FAILURES = 8
LOGIN_FAILURE_WINDOW_SECONDS = 300.0
LOGIN_LOCKOUT_SECONDS = 300.0


class LoginRateLimiter:
    """In-memory, per-client throttle that slows down login brute forcing.

    Keyed by client IP rather than account so a flood of guesses for one
    account cannot lock a victim out (which would itself be a denial of
    service). After ``max_failures`` failed attempts inside ``window_seconds``
    the client is locked out for ``lockout_seconds``.
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
        lockout_seconds: float = LOGIN_LOCKOUT_SECONDS,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Return seconds the client must wait, or 0.0 if it may try now."""
        now = time.time() if now is None else now
        locked_until = self._locked_until.get(key)
        if locked_until is None:
            return 0.0
        if locked_until > now:
            return locked_until - now
        self._locked_until.pop(key, None)
        self._failures.pop(key, None)
        return 0.0

    def register_failure(self, key: str, *, now: float | None = None) -> float:
        """Record a failed attempt and return the lockout seconds if triggered."""
        now = time.time() if now is None else now
        bucket = self._failures.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) >= self.max_failures:
            self._locked_until[key] = now + self.lockout_seconds
            bucket.clear()
            return self.lockout_seconds
        return 0.0

    def register_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


class ApiWriteRateLimiter:
    """Sliding-window cap on authenticated write requests.

    Keyed by user email (or client IP for legacy password sessions) so one
    misbehaving script cannot saturate the SQLite-backed write path for
    everyone else. Read traffic is unaffected.
    """

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max(0, int(max_requests))
        self.window_seconds = max(1.0, float(window_seconds))
        self._requests: dict[str, deque[float]] = {}

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Record one request; return 0.0 if allowed, else seconds to wait."""
        if self.max_requests <= 0:
            return 0.0
        now = time.time() if now is None else now
        bucket = self._requests.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return max(0.0, bucket[0] + self.window_seconds - now)
        bucket.append(now)
        return 0.0


def default_web_audit_path(cfg: BotConfig) -> str:
    return str(Path(cfg.trade_log.path).with_name("web_audit_events.jsonl"))


def _sanitize_audit_payload(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if any(
                marker in key_lower
                for marker in ("api_key", "secret", "password", "token", "cookie")
            ):
                clean[key_text] = "[redacted]"
            else:
                clean[key_text] = _sanitize_audit_payload(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_audit_payload(item) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _audit_event_id(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def read_recent_web_audit_events(
    cfg: BotConfig,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    path = Path(default_web_audit_path(cfg))
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in reversed(lines[-max(limit * 3, limit) :]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
        if len(events) >= limit:
            break
    return events


def write_web_audit_event(
    cfg: BotConfig,
    request: web.Request,
    *,
    action: str,
    status: str = "ok",
    target: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return write_system_web_audit_event(
        cfg,
        action=action,
        status=status,
        target=target,
        detail=detail,
        payload=payload,
        error=error,
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )


def write_system_web_audit_event(
    cfg: BotConfig,
    *,
    action: str,
    status: str = "ok",
    target: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    actor_ip: str = "system",
    path: str = "system",
    method: str = "SYSTEM",
    user_agent: str = "system",
) -> dict[str, Any]:
    event = {
        "logged_at": time.time(),
        "action": action,
        "status": status,
        "target": target,
        "detail": detail,
        "actor_ip": actor_ip,
        "path": path,
        "method": method,
        "user_agent": user_agent[:160],
        "payload": _sanitize_audit_payload(payload or {}),
    }
    if error:
        event["error"] = error
    event["event_id"] = _audit_event_id(event)
    path = Path(default_web_audit_path(cfg))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_jsonl_log_if_needed(
            path,
            max_bytes=cfg.trade_log.rotate_max_bytes,
            keep_files=cfg.trade_log.rotate_keep_files,
            compress=cfg.trade_log.rotate_compress,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True))
            handle.write("\n")
    except OSError as exc:
        return {
            **event,
            "status": "error",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    return event


def _env_optional(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    return value if value else None


def _web_password(cfg: BotConfig) -> str | None:
    return _env_optional(cfg.web_security.password_env)


# Generated once per process. Used only when neither a cookie secret env var
# nor a web password is configured, so the session signing key is never a
# publicly known constant that would let anyone forge session tokens. Sessions
# are invalidated on restart in that case, which is the safe default for an
# otherwise unconfigured deployment.
_FALLBACK_COOKIE_SECRET = secrets.token_urlsafe(32)


def _cookie_secret(cfg: BotConfig) -> str:
    return (
        _env_optional(cfg.web_security.cookie_secret_env)
        or _web_password(cfg)
        or _FALLBACK_COOKIE_SECRET
    )


def _user_store(request: web.Request) -> WebUserStore:
    return request.app["web_user_store"]


def _require_allowed_registration_email(
    cfg: BotConfig,
    store: WebUserStore,
    email: str,
) -> None:
    if store.has_users():
        return
    admin_email = _env_optional(cfg.web_security.bootstrap_admin_email_env)
    if not admin_email:
        raise RuntimeError("initial administrator email is not configured")
    try:
        expected = normalize_email(admin_email)
    except ValueError:
        raise RuntimeError("initial administrator email is invalid") from None
    if not hmac.compare_digest(email, expected):
        raise PermissionError("registration is not available for this email")


def _login_rate_limiter(request: web.Request) -> LoginRateLimiter:
    return request.app["login_rate_limiter"]


def _verification_manager(request: web.Request) -> EmailVerificationManager:
    return request.app["email_verification_manager"]


def _verification_email_sender(request: web.Request) -> VerificationEmailSender:
    return request.app["verification_email_sender"]


def _user_workspace_store(request: web.Request) -> UserWorkspaceStore:
    return request.app["user_workspace_store"]


def _user_paper_store(request: web.Request) -> UserPaperTradingStore:
    return request.app["user_paper_store"]


def _workspace_market_discovery(
    request: web.Request,
) -> WorkspaceMarketDiscoveryService:
    return request.app["workspace_market_discovery"]


def _workspace_account_checker(request: web.Request) -> WorkspaceAccountCheckService:
    return request.app["workspace_account_checker"]


def _strategy_center_store(request: web.Request) -> StrategyCenterStore:
    return request.app["strategy_center_store"]


def _request_user(request: web.Request) -> WebUser | None:
    email = str(request.get("user_email") or "")
    if not email:
        return None
    try:
        return _user_store(request).get_user(email)
    except ValueError:
        return None


def _owner_email_from_payload(payload: dict[str, Any], user: WebUser | None) -> str:
    if user is None:
        return str(payload.get("owner_email") or "").strip().lower()
    if user.role == "admin":
        return str(payload.get("owner_email") or user.email).strip().lower()
    return user.email


def _require_owner_or_admin(user: WebUser | None, owner_email: str) -> None:
    if user is None or user.role == "admin":
        return
    if str(owner_email or "").strip().lower() != user.email:
        raise PermissionError("user can only manage their own strategy center records")


def _request_is_https(request: web.Request, cfg: BotConfig) -> bool:
    if request.secure:
        return True
    if not cfg.web_security.trust_proxy_headers:
        return False
    return request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _client_ip(request: web.Request, cfg: BotConfig) -> str:
    if cfg.web_security.trust_proxy_headers:
        # X-Real-IP is set wholesale by a well-configured reverse proxy (e.g.
        # nginx's $remote_addr), so it cannot carry a client-supplied value.
        # X-Forwarded-For is normally *appended to* by the proxy
        # (nginx's $proxy_add_x_forwarded_for), so a client can prepend an
        # arbitrary spoofed address; only the rightmost hop added by our
        # immediate trusted proxy is safe to read. Preferring the leftmost
        # entry (or trusting X-Forwarded-For over X-Real-IP) would let any
        # remote client forge the IP used for the allowlist, login lockout,
        # and audit logging.
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.rsplit(",", 1)[-1].strip()
    return request.remote or ""


def _is_local_ip(value: str) -> bool:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    return parsed.is_loopback


def _allowed_ip_specs(cfg: BotConfig) -> list[str]:
    value = _env_optional(cfg.web_security.allowed_ips_env)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _ip_allowed(ip_value: str, allowed_specs: list[str]) -> bool:
    if not allowed_specs:
        return True
    try:
        parsed_ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    for spec in allowed_specs:
        try:
            if "/" in spec:
                if parsed_ip in ipaddress.ip_network(spec, strict=False):
                    return True
            elif parsed_ip == ipaddress.ip_address(spec):
                return True
        except ValueError:
            continue
    return False


def _sign_session(
    cfg: BotConfig,
    timestamp: int,
    email: str = "",
    auth_version: int = 0,
) -> str:
    secret = _cookie_secret(cfg).encode("utf-8")
    payload = (
        f"{timestamp}:{auth_version}:{email}"
        if auth_version
        else f"{timestamp}:{email}"
    ).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _make_session_token(
    cfg: BotConfig,
    email: str = "",
    auth_version: int = 0,
) -> str:
    timestamp = int(time.time())
    if email:
        version = max(1, int(auth_version or 1))
        signature = _sign_session(cfg, timestamp, email, version)
        raw = f"v3:{timestamp}:{version}:{email}:{signature}"
    else:
        raw = f"{timestamp}:{_sign_session(cfg, timestamp)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _session_details(cfg: BotConfig, token: str | None) -> tuple[bool, str, int]:
    if not token:
        return False, "", 0
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        if raw.startswith("v3:"):
            _, timestamp_text, version_text, email, signature = raw.split(":", 4)
            auth_version = int(version_text)
        elif raw.startswith("v2:"):
            _, timestamp_text, email, signature = raw.split(":", 3)
            auth_version = 0
        else:
            timestamp_text, signature = raw.split(":", 1)
            email = ""
            auth_version = 0
        timestamp = int(timestamp_text)
    except (ValueError, TypeError):
        return False, "", 0
    age = time.time() - timestamp
    if age > SESSION_MAX_AGE_SECONDS or age < -60:
        return False, "", 0
    valid = hmac.compare_digest(
        signature,
        _sign_session(cfg, timestamp, email, auth_version),
    )
    return valid, email if valid else "", auth_version if valid else 0


def _session_identity(cfg: BotConfig, token: str | None) -> tuple[bool, str]:
    valid, email, _ = _session_details(cfg, token)
    return valid, email


def _session_valid(cfg: BotConfig, token: str | None) -> bool:
    valid, _ = _session_identity(cfg, token)
    return valid


def _add_security_headers(response: web.StreamResponse) -> web.StreamResponse:
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


# Static assets are referenced with a `?v=<version>` cache-busting query
# string, so they can be cached aggressively; bump the version when an
# asset changes.
STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
_COMPRESSIBLE_CONTENT_TYPES = (
    "application/json",
    "text/html",
    "text/css",
    "text/plain",
    "application/javascript",
    "text/javascript",
    "image/svg+xml",
)
_COMPRESSIBLE_STATIC_SUFFIXES = (".js", ".css", ".svg", ".map", ".json", ".html")
_COMPRESS_MIN_BYTES = 1024


def _client_accepts_gzip(request: web.Request) -> bool:
    return "gzip" in request.headers.get("Accept-Encoding", "").lower()


@web.middleware
async def performance_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", STATIC_CACHE_CONTROL)
        if (
            not response.prepared
            and request.path.endswith(_COMPRESSIBLE_STATIC_SUFFIXES)
            and _client_accepts_gzip(request)
        ):
            response.enable_compression(web.ContentCoding.gzip)
        return response
    if response.prepared or not _client_accepts_gzip(request):
        return response
    if isinstance(response, web.Response):
        body = response.body
        if (
            body is not None
            and len(body) >= _COMPRESS_MIN_BYTES
            and response.content_type.startswith(_COMPRESSIBLE_CONTENT_TYPES)
        ):
            response.enable_compression(web.ContentCoding.gzip)
    return response


def _email_login_enabled(request: web.Request) -> bool:
    try:
        return _user_store(request).has_users()
    except ValueError:
        return False


async def login_get(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    return web.Response(
        text=_login_html(
            email_login=_email_login_enabled(request),
            registration_enabled=cfg.web_security.registration_enabled,
        ),
        content_type="text/html",
    )


def _login_throttled_response(
    cfg: BotConfig,
    *,
    email_login: bool,
    retry_after: float,
) -> web.Response:
    wait_seconds = max(1, int(retry_after + 0.999))
    response = web.Response(
        text=_login_html(
            error=(
                f"Too many failed attempts. Try again in about {wait_seconds} seconds."
            ),
            email_login=email_login,
            registration_enabled=cfg.web_security.registration_enabled,
        ),
        content_type="text/html",
        status=429,
    )
    response.headers["Retry-After"] = str(wait_seconds)
    return response


async def login_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    form = await request.post()
    email_login = _email_login_enabled(request)
    supplied_password = str(form.get("password", ""))
    username = str(form.get("username") or form.get("email") or "")
    supplied_totp = str(form.get("totp") or "")

    limiter = _login_rate_limiter(request)
    throttle_key = _client_ip(request, cfg) or "unknown"
    retry_after = limiter.retry_after(throttle_key)
    if retry_after > 0:
        return _login_throttled_response(
            cfg,
            email_login=email_login,
            retry_after=retry_after,
        )

    if email_login:
        user = (
            _user_store(request).authenticate(
                email=username,
                password=supplied_password,
                totp=supplied_totp,
            )
            if "@" in username
            else _user_store(request).authenticate(
                username=username,
                password=supplied_password,
                totp=supplied_totp,
            )
        )
        if user is None:
            limiter.register_failure(throttle_key)
            return web.Response(
                text=_login_html(
                    error=(
                        "登录凭据或动态码错误 / "
                        "Invalid username, password, or authenticator code"
                    ),
                    email_login=True,
                    registration_enabled=cfg.web_security.registration_enabled,
                ),
                content_type="text/html",
                status=401,
            )
        limiter.register_success(throttle_key)
        response = web.HTTPFound("/")
        response.set_cookie(
            SESSION_COOKIE,
            _make_session_token(cfg, user.email, user.auth_version),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=cfg.web_security.cookie_secure and _request_is_https(request, cfg),
            samesite="Strict",
        )
        raise response

    password = _web_password(cfg)
    if not password:
        raise web.HTTPFound("/")
    if not hmac.compare_digest(supplied_password, password):
        limiter.register_failure(throttle_key)
        return web.Response(
            text=_login_html(
                error="Invalid password",
                email_login=False,
                registration_enabled=cfg.web_security.registration_enabled,
            ),
            content_type="text/html",
            status=401,
        )
    limiter.register_success(throttle_key)
    response = web.HTTPFound("/")
    response.set_cookie(
        SESSION_COOKIE,
        _make_session_token(cfg),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=cfg.web_security.cookie_secure and _request_is_https(request, cfg),
        samesite="Strict",
    )
    raise response


def _purge_user_data(request: web.Request, email: str) -> None:
    """Remove all per-user records after an account is deleted."""
    _user_workspace_store(request).purge_owner(email)
    request.app["user_paper_store"].purge_owner(email)
    request.app["user_backtest_service"].store.purge_owner(email)


def _reassign_user_data(request: web.Request, old_email: str, new_email: str) -> None:
    """Move per-user records to a new owner email after an email change."""
    _user_workspace_store(request).reassign_owner(old_email, new_email)
    request.app["user_paper_store"].reassign_owner(old_email, new_email)
    request.app["user_backtest_service"].store.reassign_owner(old_email, new_email)


def _security_page_response(
    request: web.Request,
    user: WebUser,
    *,
    error: str = "",
    notice: str = "",
    signed_out: bool = False,
    pending_new_email: str = "",
    status: int = 200,
) -> web.Response:
    cfg: BotConfig = request.app["config"]
    provisioning_uri = (
        totp_provisioning_uri(
            email=user.email,
            secret=user.totp_secret,
            issuer=cfg.web_security.totp_issuer,
        )
        if not user.totp_enabled and user.totp_secret
        else ""
    )
    response = web.Response(
        text=_security_html(
            user=user,
            issuer=cfg.web_security.totp_issuer,
            provisioning_uri=provisioning_uri,
            error=error,
            notice=notice,
            signed_out=signed_out,
            pending_new_email=pending_new_email,
        ),
        content_type="text/html",
        status=status,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if signed_out:
        response.del_cookie(SESSION_COOKIE)
    return response


async def security_get(request: web.Request) -> web.Response:
    user = _request_user(request)
    if user is None:
        raise web.HTTPNotFound(text="User account security is unavailable")
    user = _user_store(request).ensure_totp_secret(email=user.email)
    return _security_page_response(request, user)


async def security_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    user = _request_user(request)
    if user is None:
        raise web.HTTPNotFound(text="User account security is unavailable")
    form = await request.post()
    action = str(form.get("action") or "").strip().lower()
    if action in {"request_email_change", "confirm_email_change", "delete_account"}:
        return await _account_management_post(request, cfg, user, form, action)
    try:
        if action not in {"enable", "disable"}:
            raise ValueError("action must be enable or disable")
        updated = _user_store(request).set_totp_enabled(
            email=user.email,
            password=str(form.get("password") or ""),
            code=str(form.get("totp") or ""),
            enabled=action == "enable",
        )
    except ValueError as exc:
        current = _user_store(request).ensure_totp_secret(email=user.email)
        return _security_page_response(
            request,
            current,
            error=str(exc),
            status=400,
        )

    write_web_audit_event(
        cfg,
        request,
        action=f"totp_{action}",
        target=updated.email,
        detail=f"{action}d authenticator two-factor authentication",
        payload={"totp_enabled": updated.totp_enabled},
    )
    notice = (
        "Authenticator 二次验证已启用 / Authenticator 2FA is enabled"
        if updated.totp_enabled
        else "Authenticator 二次验证已关闭 / Authenticator 2FA is disabled"
    )
    return _security_page_response(
        request,
        updated,
        notice=notice,
        signed_out=True,
    )


async def _account_management_post(
    request: web.Request,
    cfg: BotConfig,
    user: WebUser,
    form: Any,
    action: str,
) -> web.Response:
    store = _user_store(request)
    try:
        if action == "request_email_change":
            new_email = normalize_email(str(form.get("new_email") or ""))
            if store.authenticate(
                email=user.email,
                password=str(form.get("password") or ""),
                totp=str(form.get("totp") or ""),
            ) is None:
                raise PermissionError("password confirmation failed")
            if store.get_user(new_email) is not None:
                raise ValueError("an account with the new email already exists")
            sender = _verification_email_sender(request)
            if not sender.configured():
                raise RuntimeError("email verification service is not configured")
            code = _verification_manager(request).issue(
                email=new_email,
                purpose="change_email",
                client_key=_client_ip(request, cfg) or "unknown",
            )
            try:
                await sender.send_code(
                    email=new_email,
                    code=code,
                    purpose="change_email",
                )
            except Exception:
                _verification_manager(request).discard(
                    email=new_email,
                    purpose="change_email",
                )
                raise RuntimeError("verification email could not be sent") from None
            write_web_audit_event(
                cfg,
                request,
                action="account_email_change_requested",
                target=user.email,
                detail=f"verification code sent to {new_email}",
            )
            return _security_page_response(
                request,
                user,
                notice=(
                    "验证码已发送至新邮箱，请在下方输入完成更换 / "
                    "A verification code was sent to the new email"
                ),
                pending_new_email=new_email,
            )
        if action == "confirm_email_change":
            new_email = normalize_email(str(form.get("new_email") or ""))
            if not _verification_manager(request).verify(
                email=new_email,
                purpose="change_email",
                code=str(form.get("code") or ""),
            ):
                raise PermissionError("verification code is invalid or expired")
            old_email = user.email
            moved = store.change_email(email=old_email, new_email=new_email)
            _reassign_user_data(request, old_email, new_email)
            write_web_audit_event(
                cfg,
                request,
                action="account_email_changed",
                target=moved.email,
                detail=f"email changed from {old_email}",
            )
            return _security_page_response(
                request,
                moved,
                notice=(
                    "登录邮箱已更换，交易所 API 凭证需重新录入 / Email changed; "
                    "re-enter your exchange API credentials"
                ),
                signed_out=True,
            )
        # delete_account
        if str(form.get("confirm_delete") or "") != "on":
            raise ValueError("confirm account deletion by ticking the checkbox")
        store.delete_own_account(
            email=user.email,
            password=str(form.get("password") or ""),
            totp=str(form.get("totp") or ""),
        )
        _purge_user_data(request, user.email)
        write_web_audit_event(
            cfg,
            request,
            action="account_deleted",
            target=user.email,
            detail="user deleted their own account",
        )
        return _security_page_response(
            request,
            user,
            notice="账户及全部数据已删除 / Account and all data deleted",
            signed_out=True,
        )
    except VerificationRateLimited as exc:
        response = _security_page_response(
            request,
            user,
            error=str(exc),
            status=429,
        )
        response.headers["Retry-After"] = str(int(exc.retry_after + 0.999))
        return response
    except PermissionError as exc:
        return _security_page_response(request, user, error=str(exc), status=403)
    except RuntimeError as exc:
        return _security_page_response(request, user, error=str(exc), status=503)
    except ValueError as exc:
        return _security_page_response(request, user, error=str(exc), status=400)


async def register_get(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    if not cfg.web_security.registration_enabled:
        return web.Response(text="Registration is disabled", status=404)
    return web.Response(
        text=_register_html(),
        content_type="text/html",
    )


async def register_code_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    if not cfg.web_security.registration_enabled:
        return web.Response(text="Registration is disabled", status=404)
    form = await request.post()
    email = str(form.get("email") or "")
    username = str(form.get("username") or "")
    try:
        normalized_email = normalize_email(email)
        normalized_username = normalize_username(username)
        store = _user_store(request)
        _require_allowed_registration_email(cfg, store, normalized_email)
        if store.get_user(normalized_email) is not None:
            raise ValueError("email is already registered")
        if store.get_user_by_username(normalized_username) is not None:
            raise ValueError("username is already registered")
        sender = _verification_email_sender(request)
        if not sender.configured():
            raise RuntimeError("email verification service is not configured")
        code = _verification_manager(request).issue(
            email=normalized_email,
            purpose="register",
            client_key=_client_ip(request, cfg) or "unknown",
        )
        try:
            await sender.send_code(
                email=normalized_email,
                code=code,
                purpose="register",
            )
        except Exception:
            _verification_manager(request).discard(
                email=normalized_email,
                purpose="register",
            )
            raise RuntimeError("verification email could not be sent") from None
    except VerificationRateLimited as exc:
        response = web.Response(
            text=_register_html(
                email=email,
                username=username,
                error=str(exc),
            ),
            content_type="text/html",
            status=429,
        )
        response.headers["Retry-After"] = str(int(exc.retry_after + 0.999))
        return response
    except RuntimeError as exc:
        return web.Response(
            text=_register_html(email=email, username=username, error=str(exc)),
            content_type="text/html",
            status=503,
        )
    except PermissionError as exc:
        return web.Response(
            text=_register_html(email=email, username=username, error=str(exc)),
            content_type="text/html",
            status=403,
        )
    except ValueError as exc:
        return web.Response(
            text=_register_html(email=email, username=username, error=str(exc)),
            content_type="text/html",
            status=400,
        )
    return web.Response(
        text=_register_html(
            email=normalized_email,
            username=normalized_username,
            notice="验证码已发送，请在有效期内完成注册。",
        ),
        content_type="text/html",
    )


async def register_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    if not cfg.web_security.registration_enabled:
        return web.Response(text="Registration is disabled", status=404)
    form = await request.post()
    email = str(form.get("email") or "")
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    password_confirm = str(form.get("password_confirm") or "")
    try:
        normalized_email = normalize_email(email)
        normalized_username = normalize_username(username)
        if password != password_confirm:
            raise ValueError("passwords do not match")
        validate_password(password)
        store = _user_store(request)
        _require_allowed_registration_email(cfg, store, normalized_email)
        if store.get_user(normalized_email) is not None:
            raise ValueError("email is already registered")
        if store.get_user_by_username(normalized_username) is not None:
            raise ValueError("username is already registered")
        if not _verification_manager(request).verify(
            email=normalized_email,
            purpose="register",
            code=str(form.get("verification_code") or ""),
        ):
            raise ValueError("verification code is invalid or expired")
        user = _user_store(request).create_user(
            email=normalized_email,
            username=normalized_username,
            password=password,
        )
    except PermissionError as exc:
        return web.Response(
            text=_register_html(
                email=email,
                username=username,
                error=str(exc),
            ),
            content_type="text/html",
            status=403,
        )
    except RuntimeError as exc:
        return web.Response(
            text=_register_html(
                email=email,
                username=username,
                error=str(exc),
            ),
            content_type="text/html",
            status=503,
        )
    except ValueError as exc:
        return web.Response(
            text=_register_html(
                email=email,
                username=username,
                error=str(exc),
            ),
            content_type="text/html",
            status=400,
        )
    write_system_web_audit_event(
        cfg,
        action="user_register",
        target=user.email,
        detail="registered web user",
        payload={"email": user.email, "username": user.username},
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )
    return web.Response(
        text=_register_html(user=user),
        content_type="text/html",
    )


async def forgot_password_get(_: web.Request) -> web.Response:
    return web.Response(
        text=_forgot_password_html(),
        content_type="text/html",
    )


async def forgot_password_code_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    form = await request.post()
    email = str(form.get("email") or "")
    try:
        normalized_email = normalize_email(email)
        sender = _verification_email_sender(request)
        if not sender.configured():
            raise RuntimeError("email verification service is not configured")
        user = _user_store(request).get_user(normalized_email)
        code = _verification_manager(request).issue(
            email=normalized_email,
            purpose="password_reset",
            client_key=_client_ip(request, cfg) or "unknown",
        )
        if user is not None:
            try:
                await sender.send_code(
                    email=normalized_email,
                    code=code,
                    purpose="password_reset",
                )
            except Exception:
                _verification_manager(request).discard(
                    email=normalized_email,
                    purpose="password_reset",
                )
                raise RuntimeError("verification email could not be sent") from None
    except VerificationRateLimited as exc:
        response = web.Response(
            text=_forgot_password_html(email=email, error=str(exc)),
            content_type="text/html",
            status=429,
        )
        response.headers["Retry-After"] = str(int(exc.retry_after + 0.999))
        return response
    except RuntimeError as exc:
        return web.Response(
            text=_forgot_password_html(email=email, error=str(exc)),
            content_type="text/html",
            status=503,
        )
    except ValueError as exc:
        return web.Response(
            text=_forgot_password_html(email=email, error=str(exc)),
            content_type="text/html",
            status=400,
        )
    return web.Response(
        text=_forgot_password_html(
            email=normalized_email,
            notice="如果该邮箱已注册，验证码已经发送。",
        ),
        content_type="text/html",
    )


async def reset_password_post(request: web.Request) -> web.Response:
    cfg: BotConfig = request.app["config"]
    form = await request.post()
    email = str(form.get("email") or "")
    password = str(form.get("password") or "")
    password_confirm = str(form.get("password_confirm") or "")
    try:
        normalized_email = normalize_email(email)
        if password != password_confirm:
            raise ValueError("passwords do not match")
        validate_password(password)
        if _user_store(request).get_user(normalized_email) is None:
            raise ValueError("verification code is invalid or expired")
        if not _verification_manager(request).verify(
            email=normalized_email,
            purpose="password_reset",
            code=str(form.get("verification_code") or ""),
        ):
            raise ValueError("verification code is invalid or expired")
        user = _user_store(request).reset_password(
            email=normalized_email,
            new_password=password,
        )
    except ValueError as exc:
        return web.Response(
            text=_forgot_password_html(email=email, error=str(exc)),
            content_type="text/html",
            status=400,
        )
    write_system_web_audit_event(
        cfg,
        action="user_password_reset",
        target=user.email,
        detail="reset password by verified email",
        payload={"email": user.email, "username": user.username},
        actor_ip=_client_ip(request, cfg),
        path=request.path,
        method=request.method,
        user_agent=str(request.headers.get("User-Agent", ""))[:160],
    )
    return web.Response(
        text=_forgot_password_html(reset_complete=True),
        content_type="text/html",
    )


async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    response.del_cookie(SESSION_COOKIE)
    raise response


def build_security_middleware(cfg: BotConfig) -> web.middleware:
    write_limiter = ApiWriteRateLimiter(
        max_requests=cfg.web_security.api_write_rate_limit,
        window_seconds=cfg.web_security.api_write_rate_window_seconds,
    )

    def _write_rate_limited(request: web.Request, identity: str) -> web.Response | None:
        if request.method != "POST" or not request.path.startswith("/api/"):
            return None
        # Signal webhooks authenticate with their own shared secret and may
        # legitimately arrive in bursts from external systems.
        if request.path == "/api/signal" or request.path.startswith("/api/signal/"):
            return None
        retry_after = write_limiter.retry_after(identity or "unknown")
        if retry_after <= 0:
            return None
        response = web.json_response(
            {"error": "too many requests; slow down"},
            status=429,
        )
        response.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
        return _add_security_headers(response)

    @web.middleware
    async def security_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        async def call_handler() -> web.StreamResponse:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                _add_security_headers(exc)
                raise
            return _add_security_headers(response)

        remote = request.remote or ""
        client_ip = _client_ip(request, cfg)
        allowed_specs = _allowed_ip_specs(cfg)
        proxy_ip_present = bool(
            request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
        )
        if (
            allowed_specs
            and not (_is_local_ip(remote) and not proxy_ip_present)
            and not _ip_allowed(client_ip, allowed_specs)
        ):
            return _add_security_headers(web.Response(text="Forbidden", status=403))

        if request.path in {
            "/login",
            "/logout",
            "/register",
            "/register/code",
            "/forgot-password",
            "/forgot-password/code",
            "/reset-password",
            "/favicon.ico",
            "/static/favicon.svg",
        }:
            return await call_handler()
        if request.path == "/api/signal" or request.path.startswith("/api/signal/"):
            return await call_handler()

        password = _web_password(cfg)
        email_login = _email_login_enabled(request)
        auth_required = bool(password) or email_login
        if not auth_required:
            return await call_handler()
        if (
            request.path
            in {
                "/api/health",
                "/api/metrics",
                "/metrics",
            }
            and _is_local_ip(remote)
            and not proxy_ip_present
        ):
            return await call_handler()
        session_valid, session_email, session_auth_version = _session_details(
            cfg,
            request.cookies.get(SESSION_COOKIE),
        )
        if not session_valid or (email_login and not session_email):
            if request.path.startswith("/api/"):
                return _add_security_headers(
                    web.json_response({"error": "authentication required"}, status=401)
                )
            redirect = web.HTTPFound("/login")
            _add_security_headers(redirect)
            raise redirect
        if session_email:
            try:
                session_user = _user_store(request).get_user(session_email)
                if session_user is None:
                    raise ValueError("unknown user")
                if session_user.auth_version != session_auth_version:
                    raise ValueError("expired user session")
            except ValueError:
                if request.path.startswith("/api/"):
                    return _add_security_headers(
                        web.json_response(
                            {"error": "authentication required"}, status=401
                        )
                    )
                redirect = web.HTTPFound("/login")
                _add_security_headers(redirect)
                raise redirect
            request["user_email"] = session_email
        limited = _write_rate_limited(request, session_email or client_ip)
        if limited is not None:
            return limited
        return await call_handler()

    return security_middleware
