from __future__ import annotations

import asyncio
import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from email.message import EmailMessage
from typing import Any

from .config import AlertConfig


LEVEL_RANK = {
    "info": 10,
    "warning": 20,
    "error": 30,
    "critical": 40,
}


def _env_value(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    return value if value else None


def _enabled(level: str, min_level: str) -> bool:
    return LEVEL_RANK.get(level, 0) >= LEVEL_RANK.get(min_level, 20)


def _post_json(url: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        response.read()


def _post_form(url: str, payload: dict[str, Any]) -> None:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        response.read()


def _send_email(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_tls: bool,
    sender: str,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password or "")
        smtp.send_message(message)


class AlertService:
    def __init__(self, cfg: AlertConfig, *, cooldown_seconds: float = 300.0) -> None:
        self.cfg = cfg
        self.cooldown_seconds = cooldown_seconds
        self._last_sent_at: dict[str, float] = {}

    def configured_channels(self) -> list[str]:
        channels = []
        if _env_value(self.cfg.webhook_url_env):
            channels.append("webhook")
        if _env_value(self.cfg.telegram_bot_token_env) and _env_value(
            self.cfg.telegram_chat_id_env
        ):
            channels.append("telegram")
        if _env_value(self.cfg.smtp_host_env) and _env_value(self.cfg.email_to_env):
            channels.append("email")
        return channels

    async def send(
        self,
        *,
        level: str,
        title: str,
        message: str,
        key: str | None = None,
        payload: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        key = key or f"{level}:{title}"
        now = time.time()
        if not self.cfg.enabled or (not force and not _enabled(level, self.cfg.min_level)):
            return {"sent": False, "reason": "disabled"}
        if not force and now - self._last_sent_at.get(key, 0.0) < self.cooldown_seconds:
            return {"sent": False, "reason": "cooldown"}

        channels = self.configured_channels()
        if not channels:
            return {"sent": False, "reason": "no_channels"}

        alert_payload = {
            "level": level,
            "title": title,
            "message": message,
            "payload": payload or {},
            "config": asdict(self.cfg),
            "sent_at": now,
        }
        results = await asyncio.gather(
            *[
                asyncio.to_thread(self._send_channel, channel, alert_payload)
                for channel in channels
            ],
            return_exceptions=True,
        )
        self._last_sent_at[key] = now
        return {
            "sent": True,
            "channels": channels,
            "errors": [
                f"{channel}: {result}"
                for channel, result in zip(channels, results)
                if isinstance(result, Exception)
            ],
        }

    def _send_channel(self, channel: str, payload: dict[str, Any]) -> None:
        if channel == "webhook":
            url = _env_value(self.cfg.webhook_url_env)
            if url:
                _post_json(url, payload)
            return
        if channel == "telegram":
            token = _env_value(self.cfg.telegram_bot_token_env)
            chat_id = _env_value(self.cfg.telegram_chat_id_env)
            if token and chat_id:
                text = (
                    f"[{payload['level'].upper()}] {payload['title']}\n"
                    f"{payload['message']}"
                )
                _post_form(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    {"chat_id": chat_id, "text": text},
                )
            return
        if channel == "email":
            host = _env_value(self.cfg.smtp_host_env)
            to_value = _env_value(self.cfg.email_to_env)
            if not host or not to_value:
                return
            port = int(_env_value(self.cfg.smtp_port_env) or "587")
            username = _env_value(self.cfg.smtp_username_env)
            password = _env_value(self.cfg.smtp_password_env)
            sender = _env_value(self.cfg.email_from_env) or username or to_value
            recipients = [item.strip() for item in to_value.split(",") if item.strip()]
            body = (
                f"{payload['message']}\n\n"
                f"Level: {payload['level']}\n"
                f"Sent at: {payload['sent_at']}\n\n"
                f"{json.dumps(payload.get('payload') or {}, ensure_ascii=True, indent=2, sort_keys=True)}"
            )
            _send_email(
                host=host,
                port=port,
                username=username,
                password=password,
                use_tls=self.cfg.smtp_tls,
                sender=sender,
                recipients=recipients,
                subject=f"[Crypto Arb] {payload['title']}",
                body=body,
            )
