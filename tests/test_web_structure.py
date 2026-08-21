"""Regression checks for the web package module boundaries."""

from arbitrage_bot.web import MonitorState, create_app
from arbitrage_bot.web import core as legacy_core
from arbitrage_bot.web import loops as legacy_loops
from arbitrage_bot.web.background import (
    auto_buy_sell_task_loop,
    market_maker_task_loop,
    monitor_loop,
    spot_grid_task_loop,
)
from arbitrage_bot.web.routes.monitor import api_state
from arbitrage_bot.web.routes.profile import index
from arbitrage_bot.web.routes.strategies import api_spot_grid
from arbitrage_bot.web.routes.trading import api_market_maker


def test_web_entrypoints_are_owned_by_focused_modules() -> None:
    assert create_app.__module__ == "arbitrage_bot.web.application"
    assert MonitorState.__module__ == "arbitrage_bot.web.state"
    assert index.__module__ == "arbitrage_bot.web.routes.profile"
    assert api_state.__module__ == "arbitrage_bot.web.routes.monitor"
    assert api_market_maker.__module__ == "arbitrage_bot.web.routes.trading"
    assert api_spot_grid.__module__ == "arbitrage_bot.web.routes.strategies"


def test_background_entrypoints_are_owned_by_strategy_modules() -> None:
    assert monitor_loop.__module__ == "arbitrage_bot.web.background.monitor"
    assert market_maker_task_loop.__module__ == "arbitrage_bot.web.background.market_maker"
    assert auto_buy_sell_task_loop.__module__ == "arbitrage_bot.web.background.auto_buy_sell"
    assert spot_grid_task_loop.__module__ == "arbitrage_bot.web.background.spot_grid"


def test_legacy_facades_keep_import_compatibility() -> None:
    assert legacy_core.create_app is create_app
    assert legacy_loops.monitor_loop is monitor_loop
    assert legacy_loops.market_maker_task_loop is market_maker_task_loop
