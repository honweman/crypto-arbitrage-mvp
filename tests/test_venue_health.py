from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eth_account import Account
from eth_account.messages import encode_defunct

from arbitrage_bot.user_workspace import UserWorkspaceStore
from arbitrage_bot.venue_health import (
    refresh_venue_connections,
    venue_connection_health_loop,
)


class VenueConnectionHealthTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _verified_wallet(store: UserWorkspaceStore):
        signer = Account.create()
        challenge = store.create_wallet_challenge(
            owner_email="trader@example.com",
            address=signer.address,
            chain_id=137,
            wallet_type="metamask",
            domain="daydayuptrade.com",
        )
        signature = Account.sign_message(
            encode_defunct(text=challenge["message"]),
            signer.key,
        ).signature.hex()
        return store.verify_wallet_challenge(
            owner_email="trader@example.com",
            challenge_id=challenge["challenge_id"],
            signature=signature,
        )

    async def test_due_connection_refresh_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3", master_key_env=None
            )
            wallet = self._verified_wallet(store)
            link = store.upsert_venue_connection(
                owner_email=wallet.owner_email,
                venue="polymarket",
                wallet=wallet,
                check={
                    "status": "healthy",
                    "checked_at": time.time() - 301.0,
                },
            )
            checked_at = time.time()
            with patch(
                "arbitrage_bot.venue_health.probe_dex_venue",
                new=AsyncMock(
                    return_value={
                        "status": "healthy",
                        "checked_at": checked_at,
                        "latency_ms": 17.0,
                        "detail": {"position_count": 4},
                    }
                ),
            ):
                result = await refresh_venue_connections(store)

            refreshed = store.get_venue_connection(link.id)
            self.assertEqual(result["due_count"], 1)
            self.assertEqual(result["healthy_count"], 1)
            self.assertEqual(refreshed.checked_at, checked_at)
            self.assertEqual(refreshed.detail, {"position_count": 4})

    async def test_fresh_connection_is_not_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3", master_key_env=None
            )
            store.upsert_venue_connection(
                owner_email="trader@example.com",
                venue="dydx",
                wallet=None,
                check={"status": "healthy", "checked_at": time.time()},
            )
            with patch(
                "arbitrage_bot.venue_health.probe_dex_venue",
                new=AsyncMock(),
            ) as probe:
                result = await refresh_venue_connections(store)

            self.assertEqual(result["due_count"], 0)
            self.assertEqual(result["refreshed_count"], 0)
            probe.assert_not_awaited()

    async def test_revoked_connection_is_not_recreated_after_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3", master_key_env=None
            )
            wallet = self._verified_wallet(store)
            link = store.upsert_venue_connection(
                owner_email=wallet.owner_email,
                venue="hyperliquid",
                wallet=wallet,
                check={"status": "healthy", "checked_at": time.time()},
            )

            async def revoke_during_probe(**_):
                store.delete_wallet(wallet.id, owner_email=wallet.owner_email)
                return {
                    "status": "healthy",
                    "checked_at": time.time(),
                    "latency_ms": 12.0,
                }

            with patch(
                "arbitrage_bot.venue_health.probe_dex_venue",
                side_effect=revoke_during_probe,
            ):
                result = await refresh_venue_connections(
                    store,
                    [link],
                    force=True,
                )

            self.assertEqual(result["removed_during_check_count"], 1)
            self.assertIsNone(store.get_venue_connection(link.id))

    async def test_probe_exception_becomes_connection_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3", master_key_env=None
            )
            link = store.upsert_venue_connection(
                owner_email="trader@example.com",
                venue="dydx",
                wallet=None,
                check={"status": "healthy", "checked_at": time.time()},
            )
            with patch(
                "arbitrage_bot.venue_health.probe_dex_venue",
                new=AsyncMock(side_effect=RuntimeError("temporary outage")),
            ):
                result = await refresh_venue_connections(
                    store,
                    [link],
                    force=True,
                )

            refreshed = store.get_venue_connection(link.id)
            self.assertEqual(result["error_count"], 1)
            self.assertEqual(refreshed.status, "error")
            self.assertIn("temporary outage", refreshed.error)

    async def test_background_failure_does_not_escape_into_trading_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UserWorkspaceStore(
                Path(tmp) / "workspace.sqlite3", master_key_env=None
            )
            with (
                patch(
                    "arbitrage_bot.venue_health.refresh_venue_connections",
                    new=AsyncMock(side_effect=RuntimeError("database busy")),
                ) as refresh,
                patch(
                    "arbitrage_bot.venue_health.asyncio.sleep",
                    new=AsyncMock(side_effect=asyncio.CancelledError),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await venue_connection_health_loop(
                        store,
                        leader_check=lambda: True,
                    )

            refresh.assert_awaited_once_with(store)


if __name__ == "__main__":
    unittest.main()
