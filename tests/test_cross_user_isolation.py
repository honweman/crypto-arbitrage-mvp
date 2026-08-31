"""Cross-user isolation checks for the dashboard HTTP surface.

The existing permission tests are unit level (capability sets, scope
building). These drive the real endpoints as three logged-in identities —
an admin plus two unrelated owners — and assert that one owner can neither
read nor mutate the other's records, and cannot reach platform-wide
controls reserved for admins.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from arbitrage_bot.config import WebSecurityConfig
from arbitrage_bot.web import create_app
from arbitrage_bot.web.users import WebUserStore, totp_code

from tests.web_test_support import make_config


MASTER_KEY_ENV = "CRYPTO_ARB_ISOLATION_TEST_KEY"

ADMIN_ONLY_POSTS = (
    "/api/control",
    "/api/risk",
    "/api/markets",
    "/api/market-maker",
    "/api/spot-grid",
    "/api/dca",
    "/api/execution-algo",
    "/api/backtest",
    "/api/cash-and-carry-pairs",
    "/api/strategies/control",
    "/api/config-versions",
)


class CrossUserIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        # create_app sets CRYPTO_ARB_ORDER_JOURNAL_PATH process-wide via
        # setdefault. Pin it inside this fixture and restore it afterwards so
        # the journal cannot bleed into unrelated tests.
        self._journal_env = os.environ.get("CRYPTO_ARB_ORDER_JOURNAL_PATH")
        os.environ["CRYPTO_ARB_ORDER_JOURNAL_PATH"] = str(
            data_dir / "order_intents.sqlite3"
        )
        self._master_key_env = os.environ.get(MASTER_KEY_ENV)
        os.environ[MASTER_KEY_ENV] = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        store_path = data_dir / "web_users.json"
        # The app builds its user store with this key env; match it so the
        # test's own handle can read the same encrypted TOTP secrets.
        self.store = WebUserStore(store_path, master_key_env=MASTER_KEY_ENV)
        self.admin = self.store.create_user(
            email="admin@example.com", password="Strong-pass-1!"
        )
        self.alice = self.store.create_user(
            email="alice@example.com", password="Strong-pass-2!"
        )
        self.bob = self.store.create_user(
            email="bob@example.com", password="Strong-pass-3!"
        )
        cfg = make_config(
            web_security=WebSecurityConfig(
                password_env=None,
                cookie_secret_env=None,
                allowed_ips_env=None,
                cookie_secure=False,
                user_store_path=str(store_path),
                user_workspace_path=str(data_dir / "workspace.sqlite3"),
                credential_master_key_env=MASTER_KEY_ENV,
                api_write_rate_limit=0,
            ),
        )
        self.app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        self.server = TestServer(self.app)
        await self.server.start_server()

    async def asyncTearDown(self) -> None:
        await self.server.close()
        if self._journal_env is None:
            os.environ.pop("CRYPTO_ARB_ORDER_JOURNAL_PATH", None)
        else:
            os.environ["CRYPTO_ARB_ORDER_JOURNAL_PATH"] = self._journal_env
        if self._master_key_env is None:
            os.environ.pop(MASTER_KEY_ENV, None)
        else:
            os.environ[MASTER_KEY_ENV] = self._master_key_env
        self._tmp.cleanup()

    async def _client(self, user, password: str) -> TestClient:
        client = TestClient(self.server)
        await client.start_server()
        response = await client.post(
            "/login",
            data={
                "email": user.email,
                "password": password,
                "totp": totp_code(user.totp_secret),
            },
        )
        self.assertIn(response.status, (200, 302), await response.text())
        return client

    async def test_owner_cannot_reach_admin_only_endpoints(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        try:
            for path in ADMIN_ONLY_POSTS:
                response = await alice.post(path, json={})
                # The role check must run before any payload validation, so a
                # non-admin never learns whether the body would have been valid.
                self.assertEqual(
                    response.status,
                    403,
                    f"{path} returned {response.status} for a non-admin",
                )
            admin_users = await alice.post("/api/admin/users", json={"action": "list"})
            self.assertEqual(admin_users.status, 403)
        finally:
            await alice.close()

    async def test_owner_state_payload_hides_admin_and_other_owners(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        admin = await self._client(self.admin, "Strong-pass-1!")
        try:
            alice_state = await (await alice.get("/api/state?view=settings")).json()
            admin_state = await (await admin.get("/api/state?view=settings")).json()

            # The user roster is an admin-only projection.
            self.assertNotIn("admin_users", alice_state)
            self.assertIn("admin_users", admin_state)

            # Alice's own scope must name only Alice.
            workspace = alice_state.get("user_workspace") or {}
            for key in ("projects", "accounts", "connections", "strategies"):
                for row in workspace.get(key, []) or []:
                    owner = str(row.get("owner_email") or "").lower()
                    self.assertNotEqual(
                        owner,
                        self.bob.email,
                        f"{key} leaked a record owned by another user",
                    )
            serialized = repr(alice_state)
            self.assertNotIn(self.bob.email, serialized)
        finally:
            await alice.close()
            await admin.close()

    async def test_owner_cannot_create_or_read_records_for_another_owner(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        bob = await self._client(self.bob, "Strong-pass-3!")
        try:
            # Alice creates a project for herself.
            created = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_project",
                    "name": "Alice Project",
                    "asset": "ACS",
                    "quote_currency": "USDT",
                },
            )
            payload = await created.json()
            self.assertEqual(created.status, 200, payload)
            alice_projects = payload["workspace"]["projects"]
            self.assertTrue(alice_projects)
            alice_project_id = alice_projects[0]["id"]

            # Bob must not see it.
            bob_state = await (await bob.get("/api/state?view=settings")).json()
            bob_projects = (bob_state.get("user_workspace") or {}).get("projects") or []
            self.assertNotIn(alice_project_id, [row.get("id") for row in bob_projects])

            # Bob must not mutate it, even naming Alice as the owner.
            for attempt in (
                {"action": "delete_project", "project_id": alice_project_id},
                {"action": "disable_project", "project_id": alice_project_id},
                {
                    "action": "upsert_project",
                    "id": alice_project_id,
                    "owner_email": self.alice.email,
                    "name": "Hijacked",
                    "asset": "ACS",
                    "quote_currency": "USDT",
                },
            ):
                response = await bob.post("/api/user-workspace", json=attempt)
                self.assertIn(
                    response.status,
                    (400, 403, 404),
                    f"{attempt['action']} unexpectedly returned {response.status}",
                )

            # Alice's project survived every attempt, unrenamed.
            after = await (await alice.get("/api/state?view=settings")).json()
            rows = (after.get("user_workspace") or {}).get("projects") or []
            match = [row for row in rows if row.get("id") == alice_project_id]
            self.assertEqual(len(match), 1)
            self.assertNotEqual(match[0].get("name"), "Hijacked")
            self.assertEqual(
                str(match[0].get("owner_email") or "").lower(), self.alice.email
            )
        finally:
            await alice.close()
            await bob.close()

    async def test_owner_cannot_bind_an_account_to_another_owners_connection(
        self,
    ) -> None:
        """A supplied connection id must belong to the caller.

        Connection identity is derived from the owner's accounts, and the
        runtime resolves an account's connection id to that connection's
        encrypted credentials. Adopting someone else's id would leave two
        owners sharing one identifier on the credential path.
        """
        alice = await self._client(self.alice, "Strong-pass-2!")
        bob = await self._client(self.bob, "Strong-pass-3!")
        try:
            created = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_project",
                    "name": "Alice Project",
                    "asset": "ACS",
                    "quote_currency": "USDT",
                },
            )
            alice_project = (await created.json())["workspace"]["projects"][0]["id"]
            account = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_account",
                    "project_id": alice_project,
                    "label": "Alice Coinbase",
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDT",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                    "credentials": {"api_key": "alice-key", "secret": "alice-secret"},
                },
            )
            payload = await account.json()
            self.assertEqual(account.status, 200, payload)
            alice_connection = payload["workspace"]["connections"][0]["id"]

            bob_project_response = await bob.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_project",
                    "name": "Bob Project",
                    "asset": "ACS",
                    "quote_currency": "USDT",
                },
            )
            bob_project = (await bob_project_response.json())["workspace"]["projects"][
                0
            ]["id"]

            hijack = await bob.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_account",
                    "project_id": bob_project,
                    "connection_id": alice_connection,
                    "label": "Bob Account",
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDT",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                },
            )
            self.assertEqual(hijack.status, 403, await hijack.text())

            bob_state = await (await bob.get("/api/state?view=settings")).json()
            bob_connections = (bob_state.get("user_workspace") or {}).get(
                "connections"
            ) or []
            self.assertNotIn(
                alice_connection,
                [row.get("id") for row in bob_connections],
                "another owner's connection id was adopted",
            )

            alice_state = await (await alice.get("/api/state?view=settings")).json()
            alice_connections = (alice_state.get("user_workspace") or {}).get(
                "connections"
            ) or []
            self.assertIn(alice_connection, [row.get("id") for row in alice_connections])
        finally:
            await alice.close()
            await bob.close()

    async def test_owner_may_reuse_their_own_connection_id(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        try:
            created = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_project",
                    "name": "Alice Project",
                    "asset": "ACS",
                    "quote_currency": "USDT",
                },
            )
            project = (await created.json())["workspace"]["projects"][0]["id"]
            first = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_account",
                    "project_id": project,
                    "label": "Alice Coinbase",
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDT",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                    "credentials": {"api_key": "alice-key", "secret": "alice-secret"},
                },
            )
            connection_id = (await first.json())["workspace"]["connections"][0]["id"]

            reuse = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_account",
                    "project_id": project,
                    "connection_id": connection_id,
                    "label": "Alice Coinbase",
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDT",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                },
            )
            self.assertEqual(reuse.status, 200, await reuse.text())

            unknown = await alice.post(
                "/api/user-workspace",
                json={
                    "action": "upsert_account",
                    "project_id": project,
                    "connection_id": "account-does-not-exist",
                    "label": "Alice Coinbase",
                    "exchange": "coinbase",
                    "market_type": "spot",
                    "symbol": "ACS/USDT",
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                },
            )
            # Unknown ids stay a validation error, so the 403 above is a real
            # ownership signal rather than a generic rejection.
            self.assertEqual(unknown.status, 400, await unknown.text())
        finally:
            await alice.close()

    async def test_owner_cannot_escalate_own_role_through_profile(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        try:
            await alice.post(
                "/api/profile",
                json={
                    "preferred_asset": "",
                    "role": "admin",
                    "allowed_assets": ["ACS", "BTC", "ETH"],
                },
            )
            refreshed = self.store.get_user(self.alice.email)
            self.assertEqual(refreshed.role, "user")
            self.assertEqual(
                list(refreshed.allowed_assets), list(self.alice.allowed_assets)
            )
        finally:
            await alice.close()

    async def test_owner_cannot_delete_another_users_account(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        try:
            response = await alice.post(
                "/api/account",
                json={
                    "action": "delete_account",
                    "email": self.bob.email,
                    "owner_email": self.bob.email,
                    "password": "Strong-pass-3!",
                },
            )
            self.assertIn(response.status, (400, 403))
            self.assertIsNotNone(self.store.get_user(self.bob.email))
        finally:
            await alice.close()

    async def test_owner_backtests_are_scoped_to_the_caller(self) -> None:
        alice = await self._client(self.alice, "Strong-pass-2!")
        bob = await self._client(self.bob, "Strong-pass-3!")
        try:
            alice_runs = await (await alice.get("/api/user-backtests")).json()
            bob_runs = await (await bob.get("/api/user-backtests")).json()
            for payload, other in (
                (alice_runs, self.bob.email),
                (bob_runs, self.alice.email),
            ):
                self.assertNotIn(other, repr(payload))
        finally:
            await alice.close()
            await bob.close()


class LegacySessionPrivilegeTest(unittest.IsolatedAsyncioTestCase):
    """Emailless password sessions hold admin capabilities by design.

    That is only safe while the instance has no user accounts at all, so pin
    the boundary: as soon as one account exists, a session without an owner
    identity must stop being accepted.
    """

    async def test_legacy_session_is_refused_once_user_accounts_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store_path = data_dir / "web_users.json"
            password_env = "CRYPTO_ARB_ISOLATION_LEGACY_PW"
            previous = os.environ.get(password_env)
            os.environ[password_env] = "legacy-password"
            # See CrossUserIsolationTest: create_app pins this process-wide.
            previous_journal = os.environ.get("CRYPTO_ARB_ORDER_JOURNAL_PATH")
            os.environ["CRYPTO_ARB_ORDER_JOURNAL_PATH"] = str(
                data_dir / "order_intents.sqlite3"
            )
            try:
                cfg = make_config(
                    web_security=WebSecurityConfig(
                        password_env=password_env,
                        cookie_secret_env=None,
                        allowed_ips_env=None,
                        cookie_secure=False,
                        user_store_path=str(store_path),
                        user_workspace_path=str(data_dir / "workspace.sqlite3"),
                        api_write_rate_limit=0,
                    ),
                )
                app = create_app(cfg, "spot-spread", cfg.poll_seconds)
                server = TestServer(app)
                await server.start_server()
                client = TestClient(server)
                await client.start_server()
                try:
                    login = await client.post(
                        "/login", data={"password": "legacy-password"}
                    )
                    self.assertIn(login.status, (200, 302))
                    # No accounts yet: the operator session may reach admin routes.
                    self.assertEqual((await client.post("/api/risk", json={})).status, 200)

                    WebUserStore(store_path).create_user(
                        email="admin@example.com", password="Strong-pass-1!"
                    )

                    # The same cookie must now be refused rather than silently
                    # retaining platform capabilities alongside real accounts.
                    self.assertEqual(
                        (await client.post("/api/risk", json={})).status, 401
                    )
                    self.assertEqual((await client.get("/api/state")).status, 401)
                finally:
                    await client.close()
                    await server.close()
            finally:
                if previous is None:
                    os.environ.pop(password_env, None)
                else:
                    os.environ[password_env] = previous
                if previous_journal is None:
                    os.environ.pop("CRYPTO_ARB_ORDER_JOURNAL_PATH", None)
                else:
                    os.environ["CRYPTO_ARB_ORDER_JOURNAL_PATH"] = previous_journal


if __name__ == "__main__":
    unittest.main()
