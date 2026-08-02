from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from .user_account_check import WorkspaceAccountCheckService
from .user_workspace import UserExchangeAccount, UserWorkspaceStore


LOGGER = logging.getLogger(__name__)
HEALTHY_REFRESH_SECONDS = 6 * 60 * 60.0
ERROR_RETRY_SECONDS = 5 * 60.0
DEFAULT_LOOP_SECONDS = 30.0
DEFAULT_MAX_CONNECTION_CONCURRENCY = 2
DEFAULT_MAX_BATCH = 20


def workspace_account_check_due(
    account: UserExchangeAccount,
    *,
    now: float | None = None,
) -> bool:
    current = float(now if now is not None else time.time())
    checked_at = float(account.connection_checked_at or 0.0)
    interval = (
        HEALTHY_REFRESH_SECONDS
        if account.connection_status == "healthy"
        else ERROR_RETRY_SECONDS
    )
    return current >= checked_at + interval


def _redacted_error(exc: Exception, credentials: dict[str, str]) -> str:
    message = f"{exc.__class__.__name__}: {exc}"
    for secret in sorted(credentials.values(), key=len, reverse=True):
        if secret and len(secret) >= 4:
            message = message.replace(secret, "[redacted]")
    return message[:240]


async def refresh_workspace_account(
    store: UserWorkspaceStore,
    checker: WorkspaceAccountCheckService,
    account: UserExchangeAccount,
) -> UserExchangeAccount | None:
    project = store.get_project(account.project_id)
    if project is None or project.status != "active":
        return None
    credentials = store.decrypt_credentials(
        account_id=account.id,
        owner_email=account.owner_email,
    )
    try:
        try:
            check = await checker.check(
                account=account,
                project=project,
                credentials=credentials,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            check = {
                "status": "error",
                "error": _redacted_error(exc, credentials),
                "latency_ms": 0.0,
            }
    finally:
        credentials.clear()

    current_account = store.get_account(account.id)
    current_project = store.get_project(project.id)
    if (
        current_account is None
        or current_project is None
        or current_account.updated_at != account.updated_at
        or current_project.updated_at != project.updated_at
    ):
        return None
    return store.update_account_connection(
        account.id,
        status=str(check.get("status") or "error"),
        error=str(check.get("error") or ""),
        check=check,
    )


async def refresh_workspace_accounts(
    store: UserWorkspaceStore,
    checker: WorkspaceAccountCheckService,
    accounts: Sequence[UserExchangeAccount] | None = None,
    *,
    force: bool = False,
    now: float | None = None,
    max_connection_concurrency: int = DEFAULT_MAX_CONNECTION_CONCURRENCY,
    max_batch: int = DEFAULT_MAX_BATCH,
) -> dict[str, Any]:
    candidates = list(
        accounts
        if accounts is not None
        else store.list_accounts(owner_email="", is_admin=True)
    )
    credential_statuses = store.credential_statuses(
        [account.id for account in candidates]
    )
    current = float(now if now is not None else time.time())
    due = [
        account
        for account in sorted(
            candidates,
            key=lambda item: float(item.connection_checked_at or 0.0),
        )
        if (
            force or workspace_account_check_due(account, now=current)
        )
        and account.connection_status in {"healthy", "error"}
        and account.withdrawal_disabled_confirmed
        and account.trade_permission_confirmed
        and credential_statuses.get(account.id, {}).get("configured")
    ][: max(1, int(max_batch))]

    grouped: dict[str, list[UserExchangeAccount]] = defaultdict(list)
    for account in due:
        grouped[account.connection_id].append(account)
    semaphore = asyncio.Semaphore(max(1, int(max_connection_concurrency)))

    async def run_group(
        rows: list[UserExchangeAccount],
    ) -> list[UserExchangeAccount | None]:
        async with semaphore:
            results: list[UserExchangeAccount | None] = []
            for account in rows:
                results.append(
                    await refresh_workspace_account(store, checker, account)
                )
            return results

    grouped_results = await asyncio.gather(
        *(run_group(rows) for rows in grouped.values())
    )
    refreshed = [item for group in grouped_results for item in group if item is not None]
    return {
        "candidate_count": len(candidates),
        "due_count": len(due),
        "refreshed_count": len(refreshed),
        "healthy_count": sum(
            1 for account in refreshed if account.connection_status == "healthy"
        ),
        "error_count": sum(
            1 for account in refreshed if account.connection_status == "error"
        ),
        "enabled_count": sum(1 for account in refreshed if account.enabled),
        "checked_at": time.time(),
    }


async def workspace_account_health_loop(
    store: UserWorkspaceStore,
    checker: WorkspaceAccountCheckService,
    *,
    leader_check: Callable[[], bool],
    loop_seconds: float = DEFAULT_LOOP_SECONDS,
) -> None:
    while True:
        try:
            if leader_check():
                result = await refresh_workspace_accounts(store, checker)
                if result["error_count"]:
                    LOGGER.warning(
                        "workspace account health refresh completed with errors: %s",
                        {
                            "refreshed_count": result["refreshed_count"],
                            "error_count": result["error_count"],
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("workspace account health refresh failed")
        await asyncio.sleep(max(5.0, float(loop_seconds)))
