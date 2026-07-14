import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from arbitrage_bot.config import BackupConfig, WebSecurityConfig
from arbitrage_bot.data_backup import (
    create_backup_archive,
    prune_backup_archives,
    run_backup_cycle,
)
from arbitrage_bot.web import create_app
from arbitrage_bot.web.preflight import collect_preflight_issues
from arbitrage_bot.web.security import ApiWriteRateLimiter
from arbitrage_bot.web.users import WebUserStore, totp_code

from test_web import make_config


class PreflightTest(unittest.TestCase):
    def test_unsecured_local_instance_warns_but_does_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    registration_enabled=False,
                    user_store_path=str(Path(tmp) / "web_users.json"),
                ),
            )
            errors, warnings = collect_preflight_issues(cfg)

            self.assertEqual(errors, [])
            self.assertTrue(any("without any authentication" in w for w in warnings))

    def test_registration_without_smtp_or_master_key_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    registration_enabled=True,
                    bootstrap_admin_email_env="MISSING_BOOTSTRAP_ADMIN_TEST",
                    credential_master_key_env="MISSING_MASTER_KEY_TEST",
                    user_store_path=str(Path(tmp) / "web_users.json"),
                ),
            )
            errors, _ = collect_preflight_issues(cfg)

            self.assertTrue(any("verification email" in e for e in errors))
            self.assertTrue(any("bootstrap administrator" in e for e in errors))
            self.assertTrue(any("credential master key" in e for e in errors))


class BackupTest(unittest.TestCase):
    def _cfg(self, data_dir: Path, **backup_kwargs):
        from arbitrage_bot.config import TradeLogConfig
        from dataclasses import replace

        cfg = make_config()
        return replace(
            cfg,
            trade_log=TradeLogConfig(
                enabled=False, path=str(data_dir / "trade_events.jsonl")
            ),
            backup=BackupConfig(enabled=True, **backup_kwargs),
        )

    def test_backup_archives_sqlite_and_plain_files_and_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "web_users.json").write_text(json.dumps({"users": {}}))
            db_path = data_dir / "user_workspace.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
                connection.execute("INSERT INTO t (v) VALUES ('hello')")
            cfg = self._cfg(data_dir, keep=1)

            first = create_backup_archive(cfg)
            self.assertIsNotNone(first)
            with tarfile.open(first) as archive:
                names = archive.getnames()
            self.assertTrue(any(n.endswith("web_users.json") for n in names))
            self.assertTrue(any(n.endswith("user_workspace.sqlite3") for n in names))
            # archives themselves must not be re-archived
            self.assertFalse(any("backups" in n for n in names))

            # snapshot of the sqlite file must be a valid database
            with tarfile.open(first) as archive:
                member = next(
                    m for m in archive.getmembers()
                    if m.name.endswith("user_workspace.sqlite3")
                )
                extracted = Path(tmp) / "restored.sqlite3"
                with archive.extractfile(member) as src:
                    extracted.write_bytes(src.read())
            with sqlite3.connect(extracted) as connection:
                rows = connection.execute("SELECT v FROM t").fetchall()
            self.assertEqual(rows, [("hello",)])

            second = run_backup_cycle(cfg)
            self.assertIsNotNone(second)
            remaining = sorted(
                p.name for p in first.parent.iterdir() if p.suffix == ".gz"
            )
            self.assertEqual(len(remaining), 1)

    def test_prune_keeps_newest_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            backups = data_dir / "backups"
            backups.mkdir(parents=True)
            for stamp in ("20260101_000000", "20260102_000000", "20260103_000000"):
                (backups / f"data_backup_{stamp}.tar.gz").write_bytes(b"x")
            cfg = self._cfg(data_dir, keep=2)

            removed = prune_backup_archives(cfg)

            names = sorted(p.name for p in backups.iterdir())
            self.assertEqual(removed, 1)
            self.assertEqual(
                names,
                [
                    "data_backup_20260102_000000.tar.gz",
                    "data_backup_20260103_000000.tar.gz",
                ],
            )


class ApiWriteRateLimiterTest(unittest.TestCase):
    def test_allows_within_window_then_throttles(self) -> None:
        limiter = ApiWriteRateLimiter(max_requests=3, window_seconds=60.0)

        self.assertEqual(limiter.retry_after("u", now=0.0), 0.0)
        self.assertEqual(limiter.retry_after("u", now=1.0), 0.0)
        self.assertEqual(limiter.retry_after("u", now=2.0), 0.0)
        retry = limiter.retry_after("u", now=3.0)
        self.assertGreater(retry, 0.0)
        # other identities are unaffected
        self.assertEqual(limiter.retry_after("other", now=3.0), 0.0)
        # window slides: oldest entry expires
        self.assertEqual(limiter.retry_after("u", now=61.0), 0.0)

    def test_zero_limit_disables_throttling(self) -> None:
        limiter = ApiWriteRateLimiter(max_requests=0, window_seconds=60.0)
        for i in range(50):
            self.assertEqual(limiter.retry_after("u", now=float(i)), 0.0)


class SelfServiceAccountTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_account_requires_password_and_purges_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            store = WebUserStore(store_path)
            store.create_user(email="admin@example.com", password="Strong-pass-1!")
            trader = store.create_user(
                email="trader@example.com", password="Strong-pass-2!"
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": trader.email,
                        "password": "Strong-pass-2!",
                        "totp": totp_code(trader.totp_secret),
                    },
                )

                wrong_password = await client.post(
                    "/api/account",
                    json={"action": "delete_account", "password": "nope"},
                )
                self.assertEqual(wrong_password.status, 403)
                self.assertIsNotNone(store.get_user("trader@example.com"))

                deleted = await client.post(
                    "/api/account",
                    json={
                        "action": "delete_account",
                        "password": "Strong-pass-2!",
                        "totp": totp_code(trader.totp_secret),
                    },
                )
                payload = await deleted.json()
                self.assertEqual(deleted.status, 200, payload)
                self.assertTrue(payload["deleted"])
                self.assertIsNone(store.get_user("trader@example.com"))

                # the session is gone with the account
                after = await client.post(
                    "/api/account",
                    json={"action": "delete_account", "password": "x"},
                )
                self.assertIn(after.status, (401, 403))
            finally:
                await client.close()

    async def test_last_admin_cannot_delete_own_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "web_users.json"
            store = WebUserStore(store_path)
            admin = store.create_user(
                email="admin@example.com", password="Strong-pass-1!"
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                    user_workspace_path=str(Path(tmp) / "user_workspace.sqlite3"),
                ),
            )
            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": admin.email,
                        "password": "Strong-pass-1!",
                        "totp": totp_code(admin.totp_secret),
                    },
                )
                response = await client.post(
                    "/api/account",
                    json={
                        "action": "delete_account",
                        "password": "Strong-pass-1!",
                        "totp": totp_code(admin.totp_secret),
                    },
                )
                self.assertEqual(response.status, 400)
                self.assertIsNotNone(store.get_user("admin@example.com"))
            finally:
                await client.close()

    async def test_change_email_flow_moves_account_and_invalidates_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            store = WebUserStore(store_path)
            store.create_user(email="admin@example.com", password="Strong-pass-1!")
            trader = store.create_user(
                email="trader@example.com", password="Strong-pass-2!"
            )
            cfg = make_config(
                web_security=WebSecurityConfig(
                    password_env=None,
                    cookie_secret_env=None,
                    allowed_ips_env=None,
                    cookie_secure=False,
                    user_store_path=str(store_path),
                    user_workspace_path=str(data_dir / "user_workspace.sqlite3"),
                ),
            )

            class CapturingEmailSender:
                def __init__(self) -> None:
                    self.codes: dict[tuple[str, str], str] = {}

                def configured(self) -> bool:
                    return True

                async def send_code(
                    self, *, email: str, code: str, purpose: str
                ) -> None:
                    self.codes[(email, purpose)] = code

            app = create_app(cfg, "spot-spread", cfg.poll_seconds)
            sender = CapturingEmailSender()
            app["verification_email_sender"] = sender
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await client.post(
                    "/login",
                    data={
                        "email": trader.email,
                        "password": "Strong-pass-2!",
                        "totp": totp_code(trader.totp_secret),
                    },
                )

                requested = await client.post(
                    "/api/account",
                    json={
                        "action": "request_email_change",
                        "new_email": "newtrader@example.com",
                        "password": "Strong-pass-2!",
                        "totp": totp_code(trader.totp_secret),
                    },
                )
                self.assertEqual(requested.status, 200, await requested.text())
                code = sender.codes[("newtrader@example.com", "change_email")]

                confirmed = await client.post(
                    "/api/account",
                    json={
                        "action": "confirm_email_change",
                        "new_email": "newtrader@example.com",
                        "code": code,
                    },
                )
                payload = await confirmed.json()
                self.assertEqual(confirmed.status, 200, payload)
                self.assertTrue(payload["reauth_required"])
                self.assertIsNone(store.get_user("trader@example.com"))
                moved = store.get_user("newtrader@example.com")
                self.assertIsNotNone(moved)

                # old session no longer validates (auth_version bumped)
                stale = await client.get("/api/state")
                self.assertEqual(stale.status, 401)
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
