from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from .config import BotConfig, MarketMakerConfig
from .user_strategies import LIVE_USER_STRATEGY_TYPES, UserStrategy
from .user_workspace import UserExchangeAccount, UserWorkspaceStore
from .workspace_runtime import (
    build_workspace_runtime_accounts,
    isolated_workspace_runtime_config,
)


USER_MARKET_MAKER_PREFIX = "user-mm-"


@dataclass(frozen=True)
class UserMarketMakerBinding:
    strategy_id: str
    owner_email: str
    config: MarketMakerConfig
    runtime_config: BotConfig


def user_market_maker_instance_id(strategy: UserStrategy) -> str:
    identity = f"{strategy.owner_email}:{strategy.id}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return f"{USER_MARKET_MAKER_PREFIX}{suffix}"


def _positive_min(*values: float) -> float:
    positive = [float(value) for value in values if float(value) > 0]
    return min(positive) if positive else 0.0


def _market_maker_config(
    strategy: UserStrategy,
    account: UserExchangeAccount,
) -> MarketMakerConfig:
    parameters = strategy.parameters
    risk = strategy.risk
    return MarketMakerConfig(
        id=user_market_maker_instance_id(strategy),
        enabled=bool(strategy.enabled),
        live_enabled=bool(strategy.enabled and strategy.mode == "live"),
        exchange=f"workspace:{account.connection_id}:{account.market_type}",
        symbol=account.symbol,
        levels=int(parameters["levels"]),
        price_band_pct=float(parameters["price_band_pct"]),
        quote_per_level=float(parameters["quote_per_level"]),
        depth_shape=str(parameters["depth_shape"]),
        min_order_quote=float(parameters["min_order_quote"]),
        min_distance_bps=float(parameters["min_distance_bps"]),
        reprice_threshold_bps=float(parameters["reprice_threshold_bps"]),
        reprice_hysteresis_bps=float(parameters["reprice_hysteresis_bps"]),
        full_reprice_threshold_bps=float(parameters["full_reprice_threshold_bps"]),
        adaptive_reprice_enabled=bool(parameters["adaptive_reprice_enabled"]),
        adaptive_reprice_spread_fraction=float(
            parameters["adaptive_reprice_spread_fraction"]
        ),
        max_order_quote=float(risk["max_order_quote"]),
        max_cycle_quote=float(risk["max_total_quote"]),
        max_open_orders=int(risk["max_open_orders"]),
        max_cancels_per_cycle=int(parameters["max_cancels_per_cycle"]),
        max_slippage_bps=float(risk["max_slippage_bps"]),
        max_order_book_gap_bps=float(parameters["max_order_book_gap_bps"]),
        max_order_book_age_seconds=float(risk["max_order_book_age_seconds"]),
        poll_seconds=float(parameters["refresh_seconds"]),
        post_only=bool(parameters["post_only"]),
        cancel_existing_orders=False,
        client_order_prefix=f"arb-umm-{strategy.id[-12:]}",
        inventory_control_enabled=bool(parameters["inventory_control_enabled"]),
        inventory_target_base=float(parameters["inventory_target_base"]),
        inventory_band_base=float(parameters["inventory_band_base"]),
        inventory_max_deviation_base=float(
            parameters["inventory_max_deviation_base"]
        ),
    )


def user_market_maker_binding(
    base_cfg: BotConfig,
    store: UserWorkspaceStore,
    strategy: UserStrategy,
) -> UserMarketMakerBinding | None:
    if (
        strategy.strategy_type != "market_maker"
        or strategy.mode != "live"
        or strategy.strategy_type not in LIVE_USER_STRATEGY_TYPES
        or len(strategy.account_ids) != 1
    ):
        return None
    account = store.get_account(strategy.account_ids[0])
    if (
        account is None
        or account.owner_email != strategy.owner_email
        or account.market_type != "spot"
    ):
        return None
    readiness = store.strategy_readiness(strategy)
    if strategy.enabled and not readiness["ready"]:
        return None
    workspace = build_workspace_runtime_accounts(
        store,
        owner_emails=[strategy.owner_email],
    )
    profile = store.risk_profile(strategy.owner_email)
    runtime_cfg = isolated_workspace_runtime_config(
        base_cfg,
        workspace,
        risk_profile=profile,
    )
    maker_cfg = _market_maker_config(strategy, account)
    if not any(row.key == maker_cfg.exchange for row in runtime_cfg.spot_exchanges):
        return None
    strategy_risk = strategy.risk
    effective_risk = replace(
        runtime_cfg.risk,
        strategy_enabled={
            **runtime_cfg.risk.strategy_enabled,
            "market_maker": True,
        },
        max_order_quote=_positive_min(
            runtime_cfg.risk.max_order_quote,
            float(strategy_risk["max_order_quote"]),
        ),
        max_cycle_quote=_positive_min(
            runtime_cfg.risk.max_cycle_quote,
            float(strategy_risk["max_total_quote"]),
        ),
        max_exposure_quote=_positive_min(
            runtime_cfg.risk.max_exposure_quote,
            float(strategy_risk["max_total_quote"]),
        ),
        max_daily_loss_quote=_positive_min(
            runtime_cfg.risk.max_daily_loss_quote,
            float(strategy_risk["max_daily_loss_quote"]),
        ),
        max_open_orders=min(
            runtime_cfg.risk.max_open_orders,
            int(strategy_risk["max_open_orders"]),
        ),
    )
    runtime_cfg = replace(
        runtime_cfg,
        risk=effective_risk,
        market_maker=maker_cfg,
        market_makers=[maker_cfg],
    )
    return UserMarketMakerBinding(
        strategy_id=strategy.id,
        owner_email=strategy.owner_email,
        config=maker_cfg,
        runtime_config=runtime_cfg,
    )


def user_market_maker_bindings(
    base_cfg: BotConfig,
    store: UserWorkspaceStore,
) -> list[UserMarketMakerBinding]:
    bindings: list[UserMarketMakerBinding] = []
    for strategy in store.list_strategies(owner_email="", is_admin=True):
        binding = user_market_maker_binding(base_cfg, store, strategy)
        if binding is not None:
            bindings.append(binding)
    return bindings


__all__ = [
    "USER_MARKET_MAKER_PREFIX",
    "UserMarketMakerBinding",
    "user_market_maker_binding",
    "user_market_maker_bindings",
    "user_market_maker_instance_id",
]
