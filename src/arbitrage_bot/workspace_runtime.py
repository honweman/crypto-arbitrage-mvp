from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .config import BotConfig, ExchangeConfig, SpotMarketConfig
from .user_account_check import workspace_exchange_config
from .user_workspace import (
    api_connection_egress_blockers,
    api_connection_is_fresh,
    UserRiskProfile,
    UserWorkspaceStore,
)


DEFAULT_WORKSPACE_FEE_BPS = {
    "binance": 10.0,
    "bithumb": 25.0,
    "bybit": 10.0,
    "coinbase": 60.0,
    "gateio": 20.0,
    "htx": 20.0,
    "mexc": 20.0,
    "upbit": 5.0,
}


@dataclass(frozen=True)
class WorkspaceRuntimeAccounts:
    spot_exchanges: tuple[ExchangeConfig, ...] = ()
    derivative_exchanges: tuple[ExchangeConfig, ...] = ()
    spot_markets: tuple[SpotMarketConfig, ...] = ()


def _runtime_key(connection_id: str, market_type: str) -> str:
    return f"workspace:{connection_id}:{market_type}"


def workspace_exchange_allowed_symbols(exchange: ExchangeConfig) -> set[str] | None:
    """Return an explicit owner-selected symbol scope, or None for all markets."""

    connection_id = str(exchange.credential_connection_id or "").strip()
    owner_email = str(exchange.credential_owner_email or "").strip().lower()
    store_path = str(exchange.credential_store_path or "").strip()
    if not connection_id or not owner_email or not store_path:
        return None
    store = UserWorkspaceStore(
        store_path,
        master_key_env=exchange.credential_master_key_env,
    )
    connection = store.get_api_connection(connection_id)
    if connection is not None and connection.allow_all_markets is True:
        return None
    active_projects = {
        project.id
        for project in store.list_projects(owner_email=owner_email, is_admin=False)
        if project.status == "active"
    }
    bindings = [
        account
        for account in store.list_accounts(owner_email=owner_email, is_admin=False)
        if account.connection_id == connection_id
        and account.market_type == exchange.market_type
    ]
    if not bindings:
        return None
    return {
        account.symbol.upper()
        for account in bindings
        if account.enabled
        and account.project_id in active_projects
    }


def build_workspace_runtime_accounts(
    store: UserWorkspaceStore,
    *,
    owner_emails: Iterable[str],
    include_unbound_market_types: bool = False,
) -> WorkspaceRuntimeAccounts:
    owners = {str(email).strip().lower() for email in owner_emails if str(email).strip()}
    if not owners:
        return WorkspaceRuntimeAccounts()
    all_connections = store.list_api_connections(owner_email="", is_admin=True)
    connections = {
        row.id: row
        for row in all_connections
        if row.owner_email in owners
        and not row.runtime_keys
        and not api_connection_egress_blockers(row, all_connections)
    }
    configured = store.credential_statuses(connections)
    projects = {
        row.id: row
        for row in store.list_projects(owner_email="", is_admin=True)
        if row.owner_email in owners and row.status == "active"
    }
    grouped: dict[tuple[str, str], list[tuple[object, object]]] = {}
    for account in store.list_accounts(owner_email="", is_admin=True):
        connection = connections.get(account.connection_id)
        project = projects.get(account.project_id)
        if connection is None or project is None or not account.enabled:
            continue
        if not (
            connection.withdrawal_disabled_confirmed
            and connection.trade_permission_confirmed
            and account.withdrawal_disabled_confirmed
            and account.trade_permission_confirmed
            and configured.get(connection.id, {}).get("configured")
        ):
            continue
        grouped.setdefault((connection.id, account.market_type), []).append(
            (account, project)
        )
    if include_unbound_market_types:
        for connection in connections.values():
            if not (
                connection.withdrawal_disabled_confirmed
                and connection.trade_permission_confirmed
                and configured.get(connection.id, {}).get("configured")
                and api_connection_is_fresh(connection)
            ):
                continue
            for market_type in connection.market_types:
                if market_type in {"spot", "swap"}:
                    grouped.setdefault((connection.id, market_type), [])

    spot_exchanges: list[ExchangeConfig] = []
    derivative_exchanges: list[ExchangeConfig] = []
    spot_markets: list[SpotMarketConfig] = []
    for (connection_id, market_type), bindings in sorted(grouped.items()):
        connection = connections[connection_id]
        runtime_key = _runtime_key(connection.id, market_type)
        base = workspace_exchange_config(
            exchange=connection.exchange,
            market_type=market_type,
            api_variant=connection.api_variant,
            runtime_key=f"{connection.id}:{market_type}",
            egress_mode=connection.egress_mode,
        )
        exchange = replace(
            base,
            display_label=f"{connection.label} · {market_type.upper()}",
            fee_bps=DEFAULT_WORKSPACE_FEE_BPS.get(connection.exchange, 25.0),
            credential_connection_id=connection.id,
            credential_owner_email=connection.owner_email,
            credential_store_path=str(store.path),
            credential_master_key_env=store.master_key_env,
            all_markets_enabled=connection.allow_all_markets or not bindings,
            source_ip=(
                connection.egress_source_ip
                if connection.egress_mode == "source_ip"
                else None
            ),
        )
        if market_type == "spot":
            spot_exchanges.append(exchange)
            for account, project in bindings:
                spot_markets.append(
                    SpotMarketConfig(
                        asset=project.asset,
                        exchange=runtime_key,
                        symbol=account.symbol,
                        quote_currency=project.quote_currency,
                    )
                )
        else:
            derivative_exchanges.append(exchange)
    return WorkspaceRuntimeAccounts(
        spot_exchanges=tuple(spot_exchanges),
        derivative_exchanges=tuple(derivative_exchanges),
        spot_markets=tuple(spot_markets),
    )


def merge_workspace_runtime_accounts(
    cfg: BotConfig,
    workspace: WorkspaceRuntimeAccounts,
) -> BotConfig:
    spot_keys = {row.key for row in cfg.spot_exchanges}
    derivative_keys = {row.key for row in cfg.derivative_exchanges}
    market_keys = {(row.exchange, row.symbol) for row in cfg.spot_markets}
    account_enabled = dict(cfg.risk.account_enabled)
    for exchange in (*workspace.spot_exchanges, *workspace.derivative_exchanges):
        # Workspace accounts have already passed credential, permission, project,
        # and account-enable checks. Make their risk switch explicit so the
        # fail-closed live preflight and the execution risk gate agree.
        account_enabled.setdefault(exchange.key, True)
    return replace(
        cfg,
        spot_exchanges=[
            *cfg.spot_exchanges,
            *(row for row in workspace.spot_exchanges if row.key not in spot_keys),
        ],
        derivative_exchanges=[
            *cfg.derivative_exchanges,
            *(
                row
                for row in workspace.derivative_exchanges
                if row.key not in derivative_keys
            ),
        ],
        spot_markets=[
            *cfg.spot_markets,
            *(
                row
                for row in workspace.spot_markets
                if (row.exchange, row.symbol) not in market_keys
            ),
        ],
        risk=replace(cfg.risk, account_enabled=account_enabled),
    )


def isolated_workspace_runtime_config(
    cfg: BotConfig,
    workspace: WorkspaceRuntimeAccounts,
    *,
    risk_profile: UserRiskProfile,
) -> BotConfig:
    """Build a live runtime containing only one user's encrypted accounts."""

    exchanges = [*workspace.spot_exchanges, *workspace.derivative_exchanges]
    exchange_keys = [row.key for row in exchanges]
    risk = replace(
        cfg.risk,
        trading_enabled=bool(risk_profile.trading_enabled),
        allow_live_trading=bool(risk_profile.trading_enabled),
        allow_slow_execution=True,
        strategy_enabled={**cfg.risk.strategy_enabled, "slow_execution": True},
        account_enabled={key: True for key in exchange_keys},
        allowed_exchanges=exchange_keys,
        blocked_exchanges=[],
        # Platform symbol lists describe the administrator's strategy universe.
        # Owner runtimes validate the exact live market and existing position in
        # their reduce-only preflight instead of inheriting that unrelated scope.
        allowed_symbols=[],
        blocked_symbols=[],
        strategy_overrides={},
        max_position_base_by_asset={},
        max_exposure_quote_by_asset={},
        max_order_quote=(
            risk_profile.max_order_quote
            if risk_profile.max_order_quote > 0
            else cfg.risk.max_order_quote
        ),
        max_cycle_quote=(
            risk_profile.max_cycle_quote
            if risk_profile.max_cycle_quote > 0
            else cfg.risk.max_cycle_quote
        ),
        max_exposure_quote=(
            risk_profile.max_total_exposure_quote
            if risk_profile.max_total_exposure_quote > 0
            else cfg.risk.max_exposure_quote
        ),
        max_daily_loss_quote=(
            risk_profile.max_daily_loss_quote
            if risk_profile.max_daily_loss_quote > 0
            else cfg.risk.max_daily_loss_quote
        ),
        max_open_orders=(
            risk_profile.max_open_orders
            if risk_profile.max_open_orders > 0
            else cfg.risk.max_open_orders
        ),
    )
    return replace(
        cfg,
        spot_exchanges=list(workspace.spot_exchanges),
        derivative_exchanges=list(workspace.derivative_exchanges),
        spot_markets=list(workspace.spot_markets),
        portfolio=replace(
            cfg.portfolio,
            enabled=False,
            asset="",
            position_base=0.0,
            average_entry_price=0.0,
            positions=[],
            cash_balances={},
            realized_pnl={},
        ),
        risk=risk,
    )
