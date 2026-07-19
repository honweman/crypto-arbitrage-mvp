from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from .dex_venues import DEFAULT_TIMEOUT_SECONDS, probe_dex_venue
from .user_workspace import (
    DEX_VENUES_BY_ID,
    VENUE_ERROR_RETRY_SECONDS,
    VENUE_HEALTHY_REFRESH_SECONDS,
    UserVenueConnection,
    UserWorkspaceStore,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_LOOP_SECONDS = 15.0
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_BATCH = 40


def venue_connection_due(
    connection: UserVenueConnection,
    *,
    now: float | None = None,
) -> bool:
    interval = (
        VENUE_HEALTHY_REFRESH_SECONDS
        if connection.status == "healthy"
        else VENUE_ERROR_RETRY_SECONDS
    )
    return float(now if now is not None else time.time()) >= (
        connection.checked_at + interval
    )


def _error_check(connection: UserVenueConnection, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "venue": connection.venue,
        "wallet_address": connection.wallet_address,
        "error": str(message)[:240],
        "latency_ms": 0.0,
        "checked_at": time.time(),
        "live_trading_authorized": False,
    }


async def refresh_venue_connection(
    store: UserWorkspaceStore,
    connection: UserVenueConnection,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> UserVenueConnection | None:
    venue = DEX_VENUES_BY_ID[connection.venue]
    wallet = store.get_wallet(connection.wallet_id) if connection.wallet_id else None
    if venue.get("wallet_required") and wallet is None:
        return store.record_venue_connection_check(
            connection.id,
            _error_check(connection, "verified wallet is no longer available"),
        )
    if wallet is not None and wallet.owner_email != connection.owner_email:
        return store.record_venue_connection_check(
            connection.id,
            _error_check(connection, "wallet ownership no longer matches connection"),
        )
    try:
        check = await probe_dex_venue(
            venue=connection.venue,
            wallet_address=wallet.address if wallet else "",
            timeout_seconds=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        check = _error_check(connection, f"{exc.__class__.__name__}: {exc}")
    return store.record_venue_connection_check(connection.id, check)


async def refresh_venue_connections(
    store: UserWorkspaceStore,
    connections: Sequence[UserVenueConnection] | None = None,
    *,
    force: bool = False,
    now: float | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_batch: int = DEFAULT_MAX_BATCH,
) -> dict[str, Any]:
    candidates = list(
        connections
        if connections is not None
        else store.list_venue_connections(owner_email="", is_admin=True)
    )
    current = float(now if now is not None else time.time())
    due = [
        connection
        for connection in sorted(candidates, key=lambda item: item.checked_at)
        if force or venue_connection_due(connection, now=current)
    ][: max(1, int(max_batch))]
    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def run(connection: UserVenueConnection) -> UserVenueConnection | None:
        async with semaphore:
            return await refresh_venue_connection(
                store,
                connection,
                timeout_seconds=timeout_seconds,
            )

    refreshed = await asyncio.gather(*(run(connection) for connection in due))
    retained = [connection for connection in refreshed if connection is not None]
    return {
        "candidate_count": len(candidates),
        "due_count": len(due),
        "refreshed_count": len(retained),
        "removed_during_check_count": len(refreshed) - len(retained),
        "healthy_count": sum(1 for connection in retained if connection.status == "healthy"),
        "error_count": sum(1 for connection in retained if connection.status == "error"),
        "connections": [connection.to_dict() for connection in retained],
        "checked_at": time.time(),
        "live_trading_authorized": False,
    }


async def venue_connection_health_loop(
    store: UserWorkspaceStore,
    *,
    leader_check: Callable[[], bool],
    loop_seconds: float = DEFAULT_LOOP_SECONDS,
) -> None:
    while True:
        try:
            if leader_check():
                result = await refresh_venue_connections(store)
                if result["error_count"]:
                    LOGGER.warning(
                        "decentralized venue health refresh completed with errors: %s",
                        {
                            "refreshed_count": result["refreshed_count"],
                            "error_count": result["error_count"],
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("decentralized venue health refresh failed")
        await asyncio.sleep(max(5.0, float(loop_seconds)))
