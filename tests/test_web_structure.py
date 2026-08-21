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


def test_every_registered_route_handler_is_importable_from_the_package() -> None:
    """The package re-exports handlers so splitting routes stays invisible.

    Handlers reachable from `arbitrage_bot.web` must stay uniform: it is
    surprising for `web.api_control` to resolve while `web.api_state` raises,
    and that gap is exactly what a route split silently introduces.
    """
    import arbitrage_bot.web as web_package
    from arbitrage_bot.web import routes

    handler_names = sorted(
        name
        for name in dir(routes)
        if name.startswith("api_") and callable(getattr(routes, name))
    )
    assert handler_names, "expected route handlers to be exported from routes"

    missing = [name for name in handler_names if not hasattr(web_package, name)]
    assert missing == [], f"handlers dropped from the package surface: {missing}"

    for name in handler_names:
        assert getattr(web_package, name) is getattr(routes, name)
