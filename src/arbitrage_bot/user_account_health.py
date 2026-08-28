from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from .asset_ledger import AssetLedgerStore
from .config import AssetLedgerConfig
from .exchanges import ExchangeManager
from .user_account_check import (
    WorkspaceAccountCheckService,
    workspace_exchange_config,
)
from .user_workspace import UserApiConnection, UserExchangeAccount, UserWorkspaceStore


LOGGER = logging.getLogger(__name__)
HEALTHY_REFRESH_SECONDS = 6 * 60 * 60.0
ERROR_RETRY_SECONDS = 5 * 60.0
API_CONNECTION_HEALTHY_REFRESH_SECONDS = 2 * 60.0
API_CONNECTION_ERROR_RETRY_SECONDS = 60.0
DEFAULT_LOOP_SECONDS = 30.0
DEFAULT_MAX_CONNECTION_CONCURRENCY = 2
DEFAULT_MAX_BATCH = 20
_NEXT_CASH_FLOW_SYNC_AT: dict[str, float] = {}


async def _sync_workspace_cash_flows(
    api_connection: UserApiConnection,
    credentials: dict[str, str],
    asset_ledger_cfg: AssetLedgerConfig,
    *,
    alias_account_keys: Sequence[str] = (),
    currencies: Sequence[str] = (),
) -> None:
    if not asset_ledger_cfg.enabled:
        return
    now = time.time()
    if now < _NEXT_CASH_FLOW_SYNC_AT.get(api_connection.id, 0.0):
        return
    _NEXT_CASH_FLOW_SYNC_AT[api_connection.id] = (
        now + asset_ledger_cfg.cash_flow_interval_seconds
    )
    exchange = workspace_exchange_config(
        exchange=api_connection.exchange,
        market_type="spot",
        api_variant=api_connection.api_variant,
        runtime_key=api_connection.id,
        egress_mode=api_connection.egress_mode,
        source_ip=(
            api_connection.egress_source_ip
            if api_connection.egress_mode == "source_ip"
            else ""
        ),
    )
    manager = ExchangeManager(credentials_by_key={exchange.key: credentials})
    ledger = AssetLedgerStore(asset_ledger_cfg)
    canonical_key = api_connection.id
    for alias_key in {api_connection.id, *alias_account_keys}:
        if alias_key != canonical_key:
            ledger.set_cash_flow_account_alias(
                account_key=alias_key,
                canonical_account_key=canonical_key,
                observed_at=now,
            )
        for market_type in ("spot", "swap", "future"):
            ledger.set_cash_flow_account_alias(
                account_key=f"workspace:{alias_key}:{market_type}",
                canonical_account_key=canonical_key,
                observed_at=now,
            )
    supported: list[str] = []
    try:
        supported = manager.cash_flow_capabilities(exchange)
        cursor = ledger.cash_flow_sync_cursor(
            account_key=canonical_key,
            supported_types=supported,
            observed_at=now,
        )
        if cursor is None:
            return
        payload = await asyncio.wait_for(
            manager.fetch_cash_flows(
                exchange,
                since_ms=cursor,
                limit=100,
                currencies=currencies,
            ),
            timeout=max(3.0, asset_ledger_cfg.worker_timeout_seconds),
        )
        ledger.record_cash_flows(
            account_key=canonical_key,
            transactions=payload.get("transactions", []),
            supported_types=payload.get("supported_types", supported),
            errors=payload.get("errors", []),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        ledger.record_cash_flow_error(
            account_key=canonical_key,
            supported_types=supported,
            error=_redacted_error(exc, credentials),
        )
        LOGGER.warning(
            "workspace cash-flow sync failed account=%s error=%s",
            canonical_key,
            _redacted_error(exc, credentials),
        )
    finally:
        await manager.close()


def workspace_account_check_due(
    account: UserExchangeAccount | UserApiConnection,
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


def workspace_api_connection_check_due(
    api_connection: UserApiConnection,
    *,
    now: float | None = None,
) -> bool:
    current = float(now if now is not None else time.time())
    checked_at = float(api_connection.connection_checked_at or 0.0)
    interval = (
        API_CONNECTION_HEALTHY_REFRESH_SECONDS
        if api_connection.connection_status == "healthy"
        else API_CONNECTION_ERROR_RETRY_SECONDS
    )
    return current >= checked_at + interval


async def refresh_workspace_api_connection(
    store: UserWorkspaceStore,
    checker: WorkspaceAccountCheckService,
    api_connection: UserApiConnection,
    asset_ledger_cfg: AssetLedgerConfig | None = None,
) -> UserApiConnection | None:
    credentials = store.decrypt_credentials(
        account_id=api_connection.id,
        owner_email=api_connection.owner_email,
    )
    try:
        try:
            check = await checker.check_api_connection(
                api_connection=api_connection,
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
        if asset_ledger_cfg is not None:
            linked_account_keys = [
                account.id
                for account in store.list_accounts(owner_email="", is_admin=True)
                if account.connection_id == api_connection.id
            ]
            await _sync_workspace_cash_flows(
                api_connection,
                credentials,
                asset_ledger_cfg,
                alias_account_keys=linked_account_keys,
                currencies=[
                    str(row.get("currency") or "").upper()
                    for row in check.get("balances", []) or []
                    if isinstance(row, dict) and row.get("currency")
                ],
            )
    finally:
        credentials.clear()
    current = store.get_api_connection(api_connection.id)
    if current is None or current.updated_at != api_connection.updated_at:
        return None
    return store.update_api_connection_check(
        api_connection.id,
        status=str(check.get("status") or "error"),
        error=str(check.get("error") or ""),
        check=check,
    )


async def refresh_workspace_api_connections(
    store: UserWorkspaceStore,
    checker: WorkspaceAccountCheckService,
    *,
    force: bool = False,
    now: float | None = None,
    max_batch: int = DEFAULT_MAX_BATCH,
    asset_ledger_cfg: AssetLedgerConfig | None = None,
) -> dict[str, Any]:
    candidates = store.list_api_connections(owner_email="", is_admin=True)
    credential_statuses = store.credential_statuses(
        [api_connection.id for api_connection in candidates]
    )
    current = float(now if now is not None else time.time())
    due = [
        api_connection
        for api_connection in sorted(
            candidates,
            key=lambda item: float(item.connection_checked_at or 0.0),
        )
        if (force or workspace_api_connection_check_due(api_connection, now=current))
        and api_connection.connection_status in {"healthy", "error"}
        and api_connection.withdrawal_disabled_confirmed
        and api_connection.trade_permission_confirmed
        and credential_statuses.get(api_connection.id, {}).get("configured")
    ][: max(1, int(max_batch))]
    refreshed = [
        item
        for item in await asyncio.gather(
            *(
                refresh_workspace_api_connection(
                    store,
                    checker,
                    api_connection,
                    asset_ledger_cfg,
                )
                for api_connection in due
            )
        )
        if item is not None
    ]
    return {
        "candidate_count": len(candidates),
        "due_count": len(due),
        "refreshed_count": len(refreshed),
        "healthy_count": sum(
            1 for item in refreshed if item.connection_status == "healthy"
        ),
        "error_count": sum(
            1 for item in refreshed if item.connection_status == "error"
        ),
        "checked_at": time.time(),
    }


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
    api_connection = store.get_api_connection(account.connection_id)
    credential_owner = api_connection or account
    credentials = store.decrypt_credentials(
        account_id=credential_owner.id,
        owner_email=credential_owner.owner_email,
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
        if (force or workspace_account_check_due(account, now=current))
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
                results.append(await refresh_workspace_account(store, checker, account))
            return results

    grouped_results = await asyncio.gather(
        *(run_group(rows) for rows in grouped.values())
    )
    refreshed = [
        item for group in grouped_results for item in group if item is not None
    ]
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
    asset_ledger_cfg: AssetLedgerConfig | None = None,
    loop_seconds: float = DEFAULT_LOOP_SECONDS,
) -> None:
    while True:
        try:
            if leader_check():
                connection_result = await refresh_workspace_api_connections(
                    store,
                    checker,
                    asset_ledger_cfg=asset_ledger_cfg,
                )
                account_result = await refresh_workspace_accounts(store, checker)
                if connection_result["error_count"] or account_result["error_count"]:
                    LOGGER.warning(
                        "workspace account health refresh completed with errors: %s",
                        {
                            "connection_errors": connection_result["error_count"],
                            "market_binding_errors": account_result["error_count"],
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("workspace account health refresh failed")
        await asyncio.sleep(max(5.0, float(loop_seconds)))
