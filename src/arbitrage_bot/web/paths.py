from __future__ import annotations

from pathlib import Path




from ..config import (
    BotConfig,
)


def default_runtime_store_path(cfg: BotConfig) -> str:
    return str(Path(cfg.trade_log.path).with_name("web_runtime_overrides.json"))

def default_web_user_store_path(cfg: BotConfig) -> str:
    return cfg.web_security.user_store_path or str(
        Path(cfg.trade_log.path).with_name("web_users.json")
    )

def default_user_workspace_path(cfg: BotConfig) -> str:
    return cfg.web_security.user_workspace_path or str(
        Path(cfg.trade_log.path).with_name("user_workspace.sqlite3")
    )

def default_user_paper_trading_path(cfg: BotConfig) -> str:
    return str(
        Path(default_user_workspace_path(cfg)).with_name("user_paper_trading.sqlite3")
    )

def default_user_backtest_path(cfg: BotConfig) -> str:
    return str(
        Path(default_user_workspace_path(cfg)).with_name("user_backtests.sqlite3")
    )

def default_market_watchlist_path(cfg: BotConfig) -> str:
    return str(Path(cfg.trade_log.path).with_name("market_watchlists.json"))

def default_strategy_center_path(cfg: BotConfig) -> str:
    return cfg.strategy_center.path or str(
        Path(cfg.trade_log.path).with_name("strategy_center.sqlite3")
    )
