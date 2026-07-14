"""Startup self-check for multi-user production deployments.

Missing environment configuration (SMTP, credential master key, bootstrap
admin) used to surface only when a user hit the broken flow — a registration
that never receives its code, an exchange account that cannot be saved.
This module validates the deployment up front: hard errors refuse to start,
warnings are logged prominently.
"""

from __future__ import annotations

import logging
import os

from ..config import BotConfig
from .users import WebUserStore
from .verification import VerificationEmailSender

logger = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    """Raised when the deployment is missing configuration it cannot run without."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        summary = "; ".join(errors)
        super().__init__(f"production preflight failed: {summary}")


def _env_set(name: str | None) -> bool:
    return bool(name) and bool(os.environ.get(name or ""))


def collect_preflight_issues(
    cfg: BotConfig,
    *,
    user_store: WebUserStore | None = None,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) describing production-readiness gaps."""
    errors: list[str] = []
    warnings: list[str] = []
    security = cfg.web_security

    store = user_store or WebUserStore(security.user_store_path)
    try:
        has_users = store.has_users()
    except ValueError:
        has_users = False

    password_set = _env_set(security.password_env)
    secured = password_set or has_users or security.registration_enabled

    if not secured:
        warnings.append(
            "dashboard runs without any authentication: no web password is set, "
            "no user accounts exist, and registration is disabled. Do not expose "
            "this instance beyond localhost."
        )
        return errors, warnings

    if security.registration_enabled:
        sender = VerificationEmailSender(cfg.alerts)
        if not sender.configured():
            errors.append(
                "registration_enabled is true but the verification email service "
                "is not configured: set the SMTP host/sender environment "
                "variables referenced by the alerts config "
                f"(smtp_host_env={cfg.alerts.smtp_host_env!r}, "
                f"email_from_env={cfg.alerts.email_from_env!r})"
            )
        if not has_users and not _env_set(security.bootstrap_admin_email_env):
            errors.append(
                "registration_enabled is true, the user store is empty, and the "
                "bootstrap administrator email is not set: the first registration "
                "will be rejected. Set the environment variable referenced by "
                f"bootstrap_admin_email_env ({security.bootstrap_admin_email_env!r})."
            )

    if security.registration_enabled or has_users:
        if not _env_set(security.credential_master_key_env):
            errors.append(
                "user accounts are enabled but the credential master key is not "
                "set: users cannot save exchange API accounts. Set the "
                "environment variable referenced by credential_master_key_env "
                f"({security.credential_master_key_env!r}) to a base64-encoded "
                "32-byte key."
            )

    if not _env_set(security.cookie_secret_env) and not password_set:
        warnings.append(
            "no cookie secret is configured: session tokens are signed with a "
            "random per-process key, so every restart logs all users out. Set "
            "the environment variable referenced by cookie_secret_env "
            f"({security.cookie_secret_env!r})."
        )

    if not security.cookie_secure:
        warnings.append(
            "cookie_secure is false: session cookies can leak over plain HTTP. "
            "Keep this false only for local development."
        )

    return errors, warnings


def enforce_preflight(
    cfg: BotConfig,
    *,
    user_store: WebUserStore | None = None,
    strict: bool = True,
) -> None:
    """Log warnings and raise PreflightError on fatal gaps (unless strict=False)."""
    errors, warnings = collect_preflight_issues(cfg, user_store=user_store)
    for message in warnings:
        logger.warning("preflight: %s", message)
    if not errors:
        return
    for message in errors:
        logger.error("preflight: %s", message)
    if strict:
        raise PreflightError(errors)
    logger.error(
        "preflight: continuing despite %d error(s) because --skip-preflight was set",
        len(errors),
    )
