from __future__ import annotations

import unittest
from unittest.mock import patch

from arbitrage_bot.alerts import AlertService
from arbitrage_bot.config import AlertConfig


class AlertServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_alerts_do_not_send(self) -> None:
        service = AlertService(AlertConfig(enabled=False))

        result = await service.send(
            level="critical",
            title="Test",
            message="message",
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "disabled")

    async def test_force_bypasses_min_level_for_reports(self) -> None:
        service = AlertService(
            AlertConfig(
                enabled=True,
                min_level="warning",
                webhook_url_env="WEBHOOK_URL",
            )
        )

        with patch.dict("os.environ", {"WEBHOOK_URL": "https://example.com"}, clear=True):
            with patch.object(service, "_send_channel", return_value=None):
                result = await service.send(
                    level="info",
                    title="Daily report",
                    message="report",
                    force=True,
                )

        self.assertTrue(result["sent"])
        self.assertEqual(result["channels"], ["webhook"])

    def test_configured_channels_come_from_env(self) -> None:
        service = AlertService(
            AlertConfig(
                enabled=True,
                webhook_url_env="WEBHOOK_URL",
                telegram_bot_token_env="TG_TOKEN",
                telegram_chat_id_env="TG_CHAT",
                smtp_host_env="SMTP_HOST",
                email_to_env="EMAIL_TO",
            )
        )

        with patch.dict(
            "os.environ",
            {
                "WEBHOOK_URL": "https://example.com/hook",
                "TG_TOKEN": "token",
                "TG_CHAT": "chat",
                "SMTP_HOST": "smtp.example.com",
                "EMAIL_TO": "ops@example.com",
            },
            clear=True,
        ):
            channels = service.configured_channels()

        self.assertEqual(channels, ["webhook", "telegram", "email"])


if __name__ == "__main__":
    unittest.main()
