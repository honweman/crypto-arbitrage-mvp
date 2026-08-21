from __future__ import annotations


from aiohttp import web


from ..security import (
    _request_user,
)

from ...config import (
    BotConfig,
    ExchangeConfig,
    SlowExecutionConfig,
)


def _config_actor_email(request: web.Request) -> str:
    user = _request_user(request)
    return user.email if user is not None else "legacy-admin"

def _risk_strategy_enabled(cfg: BotConfig, strategy_id: str) -> bool:
    return cfg.risk.strategy_enabled.get(strategy_id, True)

def _risk_account_enabled(cfg: BotConfig, exchange_key: str) -> bool:
    return cfg.risk.account_enabled.get(exchange_key, True)

def _exchange_balance_symbols(
    cfg: BotConfig,
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for market in cfg.spot_markets:
        symbols.setdefault(market.exchange, set()).add(market.symbol)

    for pair in cfg.cash_and_carry_pairs:
        for exchange in cfg.spot_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.spot_symbol)
        for exchange in cfg.derivative_exchanges:
            symbols.setdefault(exchange.key, set()).add(pair.derivative_symbol)

    if cfg.market_maker.exchange and cfg.market_maker.symbol:
        symbols.setdefault(cfg.market_maker.exchange, set()).add(
            cfg.market_maker.symbol
        )

    runtime_exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    if runtime_exec_cfg.exchange and runtime_exec_cfg.symbol:
        symbols.setdefault(runtime_exec_cfg.exchange, set()).add(
            runtime_exec_cfg.symbol
        )

    for exchange, symbol in (
        (
            cfg.cross_exchange_rebalance.buy_exchange,
            cfg.cross_exchange_rebalance.buy_symbol,
        ),
        (
            cfg.cross_exchange_rebalance.sell_exchange,
            cfg.cross_exchange_rebalance.sell_symbol,
        ),
    ):
        if exchange and symbol:
            symbols.setdefault(exchange, set()).add(symbol)

    if cfg.spot_grid.exchange and cfg.spot_grid.symbol:
        symbols.setdefault(cfg.spot_grid.exchange, set()).add(cfg.spot_grid.symbol)

    if cfg.dca.exchange and cfg.dca.symbol:
        symbols.setdefault(cfg.dca.exchange, set()).add(cfg.dca.symbol)

    if cfg.execution_algo.exchange and cfg.execution_algo.symbol:
        symbols.setdefault(cfg.execution_algo.exchange, set()).add(
            cfg.execution_algo.symbol
        )

    if cfg.backtest.exchange and cfg.backtest.symbol:
        symbols.setdefault(cfg.backtest.exchange, set()).add(cfg.backtest.symbol)

    if cfg.contract_strategies.spot_exchange and cfg.contract_strategies.spot_symbol:
        symbols.setdefault(cfg.contract_strategies.spot_exchange, set()).add(
            cfg.contract_strategies.spot_symbol
        )

    if (
        cfg.contract_strategies.derivative_exchange
        and cfg.contract_strategies.derivative_symbol
    ):
        symbols.setdefault(cfg.contract_strategies.derivative_exchange, set()).add(
            cfg.contract_strategies.derivative_symbol
        )

    return {exchange: sorted(items) for exchange, items in symbols.items()}

def _all_account_exchanges(cfg: BotConfig) -> list[ExchangeConfig]:
    return [*cfg.spot_exchanges, *cfg.derivative_exchanges]

def _find_exchange_by_key(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in _all_account_exchanges(cfg):
        if exchange.key == key:
            return exchange
    raise ValueError(f"unknown exchange account: {key}")


def _market_maker_fill_source(instance_id: str) -> str:
    return f"market-maker:{instance_id}"
