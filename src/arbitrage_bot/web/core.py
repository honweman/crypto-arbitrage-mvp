"""Compatibility exports for web payload services and route handlers."""

from .services.shared import (
    _all_account_exchanges as _all_account_exchanges,
    _config_actor_email as _config_actor_email,
    _exchange_balance_symbols as _exchange_balance_symbols,
    _find_exchange_by_key as _find_exchange_by_key,
    _risk_account_enabled as _risk_account_enabled,
    _risk_strategy_enabled as _risk_strategy_enabled,
    _market_maker_fill_source as _market_maker_fill_source,
)
from .services.dashboard import (
    _account_payload_by_exchange as _account_payload_by_exchange,
    _account_payload_messages as _account_payload_messages,
    _compact_strategy_timeline_entry as _compact_strategy_timeline_entry,
    _compact_trade_log_entry as _compact_trade_log_entry,
    _dedupe_readiness_messages as _dedupe_readiness_messages,
    _derivative_account_messages as _derivative_account_messages,
    _derivatives_readiness_summary as _derivatives_readiness_summary,
    _readiness_action as _readiness_action,
    _readiness_message_key as _readiness_message_key,
    _readiness_strategy_reasons as _readiness_strategy_reasons,
    _top_level as _top_level,
    build_market_rows as build_market_rows,
    build_operations_payload as build_operations_payload,
    build_readiness_payload as build_readiness_payload,
    build_trading_console_payload as build_trading_console_payload,
)
from .services.exchange_data import (
    _account_balance_status as _account_balance_status,
    _activity_status as _activity_status,
    _add_reserve as _add_reserve,
    _aggregate_account_balance_totals as _aggregate_account_balance_totals,
    _apply_open_order_reserves_to_balance as _apply_open_order_reserves_to_balance,
    _configured_exchange_keys as _configured_exchange_keys,
    _derivatives_status as _derivatives_status,
    _fetch_derivative_exchange_risk_payload as _fetch_derivative_exchange_risk_payload,
    _fetch_exchange_balance_payload as _fetch_exchange_balance_payload,
    _fetch_exchange_market_limit_payload as _fetch_exchange_market_limit_payload,
    _fetch_exchange_order_activity as _fetch_exchange_order_activity,
    _fetch_open_order_reserves as _fetch_open_order_reserves,
    _normalize_order as _normalize_order,
    _normalize_trade as _normalize_trade,
    _number_or_none as _number_or_none,
    _open_order_price as _open_order_price,
    _open_order_remaining_amount as _open_order_remaining_amount,
    _order_fee_payload as _order_fee_payload,
    _sort_activity_rows as _sort_activity_rows,
    _symbol_base_quote as _symbol_base_quote,
    cancel_bulk_orders_payload as cancel_bulk_orders_payload,
    cancel_order_payload as cancel_order_payload,
    fetch_account_balances_payload as fetch_account_balances_payload,
    fetch_derivatives_risk_payload as fetch_derivatives_risk_payload,
    fetch_funding_basis_payload as fetch_funding_basis_payload,
    fetch_options_arbitrage_payload as fetch_options_arbitrage_payload,
    fetch_order_activity_payload as fetch_order_activity_payload,
)
from .services.workspace import (
    _merge_workspace_account_balances as _merge_workspace_account_balances,
    _sync_portfolio_with_account_balances as _sync_portfolio_with_account_balances,
    build_strategy_center_payload as build_strategy_center_payload,
    build_user_workspace_payload as build_user_workspace_payload,
)
from .services.runtime import (
    _build_initial_payload as _build_initial_payload,
    _build_market_maker_instance_payload as _build_market_maker_instance_payload,
    _cached_onchain_payload as _cached_onchain_payload,
    _converted_market_context as _converted_market_context,
    _dataclass_overrides as _dataclass_overrides,
    _global_scan_health_warnings as _global_scan_health_warnings,
    _load_runtime_overrides as _load_runtime_overrides,
    _missing_market_warnings as _missing_market_warnings,
    _onchain_error_payload as _onchain_error_payload,
    _save_runtime_overrides as _save_runtime_overrides,
    _strategy_quote_conversion as _strategy_quote_conversion,
    _strategy_safety_base as _strategy_safety_base,
    build_backtest_payload as build_backtest_payload,
    build_dca_payload as build_dca_payload,
    build_dca_safety_payload as build_dca_safety_payload,
    build_execution_algo_payload as build_execution_algo_payload,
    build_execution_algo_safety_payload as build_execution_algo_safety_payload,
    build_market_maker_payload as build_market_maker_payload,
    build_market_maker_safety_payload as build_market_maker_safety_payload,
    build_slow_execution_payload as build_slow_execution_payload,
    build_spot_grid_payload as build_spot_grid_payload,
    build_spot_grid_safety_payload as build_spot_grid_safety_payload,
    fetch_onchain_payload as fetch_onchain_payload,
)
from .services.owner import (
    _owner_live_market_maker_order_activity as _owner_live_market_maker_order_activity,
    _owner_live_trading_console as _owner_live_trading_console,
)
from .paths import (
    default_market_watchlist_path as default_market_watchlist_path,
    default_runtime_store_path as default_runtime_store_path,
    default_strategy_center_path as default_strategy_center_path,
    default_user_backtest_path as default_user_backtest_path,
    default_user_paper_trading_path as default_user_paper_trading_path,
    default_user_workspace_path as default_user_workspace_path,
    default_web_user_store_path as default_web_user_store_path,
)

# Compatibility exports. New code should import handlers from their focused modules.
from .application import (  # noqa: E402
    build_parser as build_parser,
    create_app as create_app,
    main as main,
)
from .routes.control import (  # noqa: E402
    _consume_strategy_preflight as _consume_strategy_preflight,
    _preflight_candidate_from_payload as _preflight_candidate_from_payload,
    _require_user_owned_execution as _require_user_owned_execution,
    _schedule_started_config_guard as _schedule_started_config_guard,
    _user_execution_preflight as _user_execution_preflight,
    _watch_startup_configuration as _watch_startup_configuration,
    api_control as api_control,
)
from .routes.profile import (  # noqa: E402
    _state_payload_for_request as _state_payload_for_request,
    _user_auto_buy_sell_payload as _user_auto_buy_sell_payload,
    _user_auto_buy_sell_runtime_config as _user_auto_buy_sell_runtime_config,
    api_account as api_account,
    api_admin_users as api_admin_users,
    api_profile as api_profile,
    index as index,
)
from .routes.strategies import (  # noqa: E402
    api_backtest as api_backtest,
    api_cross_exchange_rebalance as api_cross_exchange_rebalance,
    api_dca as api_dca,
    api_execution_algo as api_execution_algo,
    api_slow_execution as api_slow_execution,
    api_spot_grid as api_spot_grid,
    api_strategy_preflight as api_strategy_preflight,
)
from .routes.strategy_center import (  # noqa: E402
    api_signal_webhook as api_signal_webhook,
    api_strategy_center as api_strategy_center,
)
from .routes.trading import (  # noqa: E402
    api_cancel_bulk_orders as api_cancel_bulk_orders,
    api_cancel_order as api_cancel_order,
    api_cash_and_carry_pairs as api_cash_and_carry_pairs,
    api_cleanup_auto_buy_sell_tasks as api_cleanup_auto_buy_sell_tasks,
    api_config_versions_get as api_config_versions_get,
    api_config_versions_post as api_config_versions_post,
    api_control_auto_buy_sell_task as api_control_auto_buy_sell_task,
    api_create_auto_buy_sell_task as api_create_auto_buy_sell_task,
    api_market_maker as api_market_maker,
    api_markets as api_markets,
    api_risk as api_risk,
    api_strategy_control as api_strategy_control,
)
from .routes.workspace import (  # noqa: E402
    api_user_backtests_get as api_user_backtests_get,
    api_user_backtests_post as api_user_backtests_post,
    api_user_workspace as api_user_workspace,
)
