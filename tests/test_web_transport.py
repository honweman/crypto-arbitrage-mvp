from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from arbitrage_bot.config import WebSecurityConfig
from arbitrage_bot.web import create_app
from arbitrage_bot.web.routes.market_tickers import api_market_tickers
from arbitrage_bot.web.routes.monitor import api_state, api_state_stream
from tests.web_test_support import make_config


class WebTransportTest(unittest.IsolatedAsyncioTestCase):
    def test_monitor_and_ticker_handlers_live_outside_web_package_root(self) -> None:
        self.assertEqual(api_state.__module__, "arbitrage_bot.web.routes.monitor")
        self.assertEqual(
            api_state_stream.__module__,
            "arbitrage_bot.web.routes.monitor",
        )
        self.assertEqual(
            api_market_tickers.__module__,
            "arbitrage_bot.web.routes.market_tickers",
        )

    async def test_api_state_is_gzip_compressed_for_gzip_clients(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/state")
            payload = await response.json()

            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
            self.assertIn("status", payload)
        finally:
            await client.close()

    async def test_static_assets_get_immutable_cache_control_and_gzip(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/static/app.js")

            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers.get("Cache-Control"),
                "public, max-age=31536000, immutable",
            )
            self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        finally:
            await client.close()

    async def test_status_details_dialog_and_click_handlers_are_shipped(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            page_response = await client.get("/")
            script_response = await client.get("/static/app.js")
            style_response = await client.get("/static/styles.css")
            page = await page_response.text()
            script = await script_response.text()
            style = await style_response.text()

            self.assertIn('id="status-detail-dialog"', page)
            self.assertIn('aria-haspopup="dialog"', page)
            self.assertIn("function statusIssueRows", script)
            self.assertIn("function openStatusDetails", script)
            self.assertIn(".status-detail-dialog", style)
            self.assertIn(".status-trigger.is-clickable", style)
        finally:
            await client.close()

    async def test_favicon_is_served_even_without_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env="TEST_WEB_PASSWORD",
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(data_dir / "web_users.json"),
                ),
            )
            with patch.dict(os.environ, {"TEST_WEB_PASSWORD": "123456"}, clear=False):
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    ico = await client.get("/favicon.ico", allow_redirects=False)
                    svg = await client.get(
                        "/static/favicon.svg",
                        allow_redirects=False,
                    )
                    page = await client.get("/", allow_redirects=False)

                    self.assertEqual(ico.status, 200)
                    self.assertEqual(ico.headers.get("Content-Type"), "image/svg+xml")
                    self.assertEqual(svg.status, 200)
                    self.assertEqual(page.status, 302)
                finally:
                    await client.close()

    async def test_state_stream_pushes_snapshots_matching_state_payload(self) -> None:
        cfg = make_config()
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            state_response = await client.get("/api/state?view=status")
            state_payload = await state_response.json()

            stream_response = await client.get(
                "/api/state/stream?view=status&interval=1"
            )
            self.assertEqual(stream_response.status, 200)
            self.assertEqual(
                stream_response.headers.get("Content-Type"),
                "text/event-stream",
            )
            event = await asyncio.wait_for(
                stream_response.content.readuntil(b"\n\n"),
                timeout=10,
            )
            stream_response.close()

            self.assertTrue(event.startswith(b"data: "))
            streamed_payload = json.loads(event[len(b"data: ") :].decode("utf-8"))
            self.assertEqual(
                sorted(streamed_payload.keys()),
                sorted(state_payload.keys()),
            )
            self.assertIn("status", streamed_payload)
        finally:
            await client.close()
