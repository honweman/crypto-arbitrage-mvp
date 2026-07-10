from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..config import BotConfig, CashAndCarryPair, SpotMarketConfig
from .users import WebUser


def _base_asset_from_symbol(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].split(":", 1)[0].strip().upper()


def _configured_assets(cfg: BotConfig) -> list[str]:
    assets = {market.asset.upper() for market in cfg.spot_markets if market.asset}
    assets.update(
        position.asset.upper()
        for position in cfg.portfolio.positions
        if position.asset
    )
    if cfg.portfolio.asset:
        assets.add(cfg.portfolio.asset.upper())
    if cfg.market_maker.symbol:
        assets.add(_base_asset_from_symbol(cfg.market_maker.symbol))
    if cfg.slow_execution.symbol:
        assets.add(_base_asset_from_symbol(cfg.slow_execution.symbol))
    if cfg.spot_grid.symbol:
        assets.add(_base_asset_from_symbol(cfg.spot_grid.symbol))
    if cfg.dca.symbol:
        assets.add(_base_asset_from_symbol(cfg.dca.symbol))
    if cfg.execution_algo.symbol:
        assets.add(_base_asset_from_symbol(cfg.execution_algo.symbol))
    if cfg.backtest.symbol:
        assets.add(_base_asset_from_symbol(cfg.backtest.symbol))
    for combo in cfg.option_combos:
        assets.add(combo.underlying.upper() or _base_asset_from_symbol(combo.spot_symbol))
    return sorted(asset for asset in assets if asset)


def _user_asset_scope(user: WebUser | None) -> set[str]:
    if user is None:
        return set()
    if user.preferred_asset:
        return {user.preferred_asset}
    return set(user.allowed_assets)


def _user_can_access_asset(user: WebUser | None, asset: str) -> bool:
    if user is None:
        return True
    if user.role == "admin":
        return True
    normalized = str(asset or "").strip().upper()
    if not normalized:
        return True
    return normalized in set(user.allowed_assets)


def _require_admin_user(user: WebUser | None) -> None:
    if user is not None and user.role != "admin":
        raise PermissionError("admin role is required for this action")


def _assets_from_spot_markets(markets: list[SpotMarketConfig]) -> list[str]:
    return sorted({market.asset.upper() for market in markets if market.asset})


def _assets_from_cash_and_carry_pairs(pairs: list[CashAndCarryPair]) -> list[str]:
    return sorted(
        {
            _base_asset_from_symbol(pair.spot_symbol)
            for pair in pairs
            if pair.spot_symbol
        }
    )


def _require_user_assets(user: WebUser | None, assets: Iterable[str]) -> None:
    denied = sorted(
        {
            str(asset or "").strip().upper()
            for asset in assets
            if asset and not _user_can_access_asset(user, str(asset))
        }
    )
    if denied:
        raise PermissionError(
            "user is not allowed to manage asset(s): " + ", ".join(denied)
        )


def _opportunity_asset(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    asset = str(metadata.get("asset") or row.get("asset") or "").strip().upper()
    if asset:
        return asset
    legs = row.get("legs")
    if isinstance(legs, list) and legs:
        first = legs[0] if isinstance(legs[0], dict) else {}
        return _base_asset_from_symbol(str(first.get("symbol") or ""))
    return ""


def _filter_state_payload_for_user(
    payload: dict[str, Any],
    *,
    cfg: BotConfig,
    user: WebUser | None,
    order_activity_limit: int = 20,
) -> dict[str, Any]:
    available_assets = _configured_assets(cfg)
    if user is None:
        payload["auth"] = {
            "mode": "legacy",
            "email": "",
            "allowed_assets": [],
            "preferred_asset": "",
            "available_assets": available_assets,
            "asset_scope": [],
        }
        return payload

    allowed_assets = set(user.allowed_assets)
    asset_scope = _user_asset_scope(user)
    unassigned_user = user.role != "admin" and not allowed_assets

    def in_scope(asset: str) -> bool:
        normalized = str(asset or "").strip().upper()
        if not normalized:
            return True
        if unassigned_user:
            return False
        if allowed_assets and normalized not in allowed_assets:
            return False
        return not asset_scope or normalized in asset_scope

    def symbol_in_scope(symbol: str) -> bool:
        return in_scope(_base_asset_from_symbol(symbol))

    def filter_symbols(symbols: Iterable[Any]) -> list[str]:
        return [
            str(symbol)
            for symbol in symbols or []
            if symbol and symbol_in_scope(str(symbol))
        ]

    currency_scope: set[str] | None = set() if unassigned_user else None
    if not unassigned_user and (allowed_assets or asset_scope):
        currency_scope = set(asset_scope or allowed_assets)
        for market in cfg.spot_markets:
            if in_scope(market.asset):
                currency_scope.add(market.quote_currency.upper())
        if cfg.common_quote_currency:
            currency_scope.add(cfg.common_quote_currency.upper())

    def currency_in_scope(currency: str) -> bool:
        if currency_scope is None:
            return True
        return str(currency or "").strip().upper() in currency_scope

    def filter_symbol_rows(
        rows: Any,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        filtered = [
            row
            for row in rows
            if isinstance(row, dict) and symbol_in_scope(str(row.get("symbol") or ""))
        ]
        return filtered[:limit] if limit is not None else filtered

    def filter_accounts(
        accounts: Any,
        *,
        include_balances: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(accounts, list):
            return []
        filtered_accounts: list[dict[str, Any]] = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            row = dict(account)
            has_symbol_list = isinstance(row.get("symbols"), list)
            if has_symbol_list:
                row["symbols"] = filter_symbols(row.get("symbols", []))
            for key in ("open_orders", "closed_orders", "recent_trades"):
                if key in row:
                    row[key] = filter_symbol_rows(row.get(key))
                    row[f"{key[:-1]}_count"] = len(row[key])
            if include_balances and isinstance(row.get("balance"), dict):
                balance = dict(row["balance"])
                if isinstance(balance.get("currencies"), list):
                    balance["currencies"] = [
                        item
                        for item in balance["currencies"]
                        if isinstance(item, dict)
                        and currency_in_scope(str(item.get("currency") or ""))
                    ]
                reserves = balance.get("open_order_reserves")
                if isinstance(reserves, dict) and isinstance(
                    reserves.get("currencies"),
                    dict,
                ):
                    reserves = dict(reserves)
                    reserves["currencies"] = {
                        currency: value
                        for currency, value in reserves["currencies"].items()
                        if currency_in_scope(str(currency))
                    }
                    balance["open_order_reserves"] = reserves
                row["balance"] = balance
            has_rows = (not has_symbol_list) or any(
                row.get(key)
                for key in ("symbols", "open_orders", "closed_orders", "recent_trades")
            )
            has_balance = (
                bool(row.get("balance", {}).get("currencies"))
                if include_balances
                else False
            )
            if has_rows or has_balance:
                filtered_accounts.append(row)
        return filtered_accounts

    def filtered_pnl_summary(
        trades: list[dict[str, Any]],
        original: Any,
    ) -> dict[str, Any]:
        source_rows: dict[str, dict[str, Any]] = {}
        for trade in trades:
            source = str(trade.get("source") or "unattributed")
            row = source_rows.setdefault(
                source,
                {
                    "source": source,
                    "trade_count": 0,
                    "notional_common": 0.0,
                    "fees_common": 0.0,
                    "realized_pnl": 0.0,
                },
            )
            row["trade_count"] += 1
            row["notional_common"] += float(trade.get("notional_common") or 0.0)
            row["fees_common"] += float(trade.get("fee_common") or 0.0)
            row["realized_pnl"] += float(trade.get("realized_pnl_common") or 0.0)
        original_payload = original if isinstance(original, dict) else {}
        return {
            "currency": original_payload.get("currency") or cfg.common_quote_currency,
            "window": original_payload.get("window") or "recent_fills",
            "trade_count": len(trades),
            "attributed_trade_count": sum(
                1 for trade in trades if trade.get("source") != "unattributed"
            ),
            "unattributed_trade_count": sum(
                1 for trade in trades if trade.get("source") == "unattributed"
            ),
            "total_realized_pnl": sum(
                row["realized_pnl"] for row in source_rows.values()
            ),
            "total_fees": sum(row["fees_common"] for row in source_rows.values()),
            "total_notional": sum(
                row["notional_common"] for row in source_rows.values()
            ),
            "sources": source_rows,
            "missing_cost_basis": original_payload.get("missing_cost_basis", []),
            "missing_quote_rates": original_payload.get("missing_quote_rates", []),
            "missing_fee_rates": original_payload.get("missing_fee_rates", []),
            "observed_at": original_payload.get("observed_at"),
            "asset_scoped": True,
        }

    def filter_order_activity(activity: Any) -> None:
        if not isinstance(activity, dict):
            return
        activity["accounts"] = filter_accounts(activity.get("accounts"))
        activity["open_orders"] = filter_symbol_rows(activity.get("open_orders"))
        activity["closed_orders"] = filter_symbol_rows(activity.get("closed_orders"))
        activity["recent_trades"] = filter_symbol_rows(
            activity.get("recent_trades"),
            limit=order_activity_limit,
        )
        activity["open_order_count"] = len(activity["open_orders"])
        activity["closed_order_count"] = len(activity["closed_orders"])
        activity["recent_trade_count"] = len(activity["recent_trades"])
        activity["checked_account_count"] = len(activity["accounts"])
        activity["pnl_summary"] = filtered_pnl_summary(
            activity["recent_trades"],
            activity.get("pnl_summary"),
        )
        activity["daily_pnl"] = None
        reconciliation = activity.get("reconciliation")
        if isinstance(reconciliation, dict) and isinstance(
            reconciliation.get("issues"),
            list,
        ):
            issues = [
                issue
                for issue in reconciliation["issues"]
                if isinstance(issue, dict)
                and symbol_in_scope(str(issue.get("symbol") or ""))
            ]
            reconciliation["issues"] = issues
            reconciliation["issue_count"] = len(issues)
            reconciliation["critical_issue_count"] = sum(
                1 for issue in issues if issue.get("level") == "error"
            )
            reconciliation["notice_count"] = sum(
                1 for issue in issues if issue.get("level") == "info"
            )
            reconciliation["status"] = "warning" if issues else "ok"

    def filter_account_balances(balances: Any) -> None:
        if not isinstance(balances, dict):
            return
        balances["accounts"] = filter_accounts(
            balances.get("accounts"),
            include_balances=True,
        )
        if isinstance(balances.get("totals"), list):
            balances["totals"] = [
                row
                for row in balances["totals"]
                if isinstance(row, dict)
                and currency_in_scope(str(row.get("currency") or ""))
            ]
        balances["checked_account_count"] = len(balances["accounts"])
        balances["total_account_count"] = len(balances["accounts"])

    def filter_strategy_section(section: Any) -> None:
        if not isinstance(section, dict):
            return
        section["accounts"] = filter_accounts(section.get("accounts"))
        config = section.get("config") if isinstance(section.get("config"), dict) else {}
        plan = section.get("plan") if isinstance(section.get("plan"), dict) else {}
        symbol = str(config.get("symbol") or plan.get("symbol") or "")
        if symbol and not symbol_in_scope(symbol):
            section.update(
                {
                    "status": "out_of_scope",
                    "plan": None,
                    "config": {},
                    "runtime": {},
                    "market_data": None,
                    "safety": {},
                    "error": "outside user asset scope",
                }
            )
            return
        tasks = section.get("tasks")
        if isinstance(tasks, dict) and isinstance(tasks.get("tasks"), list):
            tasks["tasks"] = [
                task
                for task in tasks["tasks"]
                if isinstance(task, dict)
                and symbol_in_scope(
                    str(
                        (
                            task.get("config")
                            if isinstance(task.get("config"), dict)
                            else {}
                        ).get("symbol")
                        or ""
                    )
                )
            ]
            tasks["task_count"] = len(tasks["tasks"])
            tasks["active_count"] = sum(
                1
                for task in tasks["tasks"]
                if task.get("status")
                not in {"complete", "stopped_by_price", "below_min_order_quote"}
            )

    def filter_strategy_rows(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        filtered_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            symbol_text = str(item.get("symbol") or "")
            if "/" in symbol_text:
                if not symbol_in_scope(symbol_text):
                    continue
            elif "," in symbol_text:
                assets = [
                    asset.strip().upper()
                    for asset in symbol_text.split(",")
                    if asset.strip()
                ]
                assets = [asset for asset in assets if in_scope(asset)]
                if not assets:
                    continue
                item["symbol"] = ",".join(assets)
            filtered_rows.append(item)
        return filtered_rows

    def filter_trading_console(console: Any) -> None:
        if not isinstance(console, dict):
            return
        console["accounts"] = filter_accounts(console.get("accounts"))
        console["strategies"] = filter_strategy_rows(console.get("strategies"))
        console["open_order_count"] = len(
            payload.get("order_activity", {}).get("open_orders", [])
        )

    def filter_readiness(readiness: Any) -> None:
        if not isinstance(readiness, dict):
            return
        readiness["accounts"] = filter_accounts(readiness.get("accounts"))
        readiness["strategies"] = filter_strategy_rows(readiness.get("strategies"))
        summary = readiness.get("summary")
        if isinstance(summary, dict):
            summary["used_accounts"] = len(readiness["accounts"])
            summary["configured_strategies"] = len(readiness["strategies"])

    def filter_strategy_center(center: Any) -> None:
        if not isinstance(center, dict):
            return
        owner = user.email
        strategies = []
        for row in center.get("strategy_instances", []) or []:
            if not isinstance(row, dict):
                continue
            if user.role != "admin" and str(row.get("owner_email") or "").lower() != owner:
                continue
            if not in_scope(str(row.get("asset") or _base_asset_from_symbol(str(row.get("symbol") or "")))):
                continue
            strategies.append(row)
        accounts = []
        for row in center.get("user_api_accounts", []) or []:
            if not isinstance(row, dict):
                continue
            if user.role != "admin" and str(row.get("owner_email") or "").lower() != owner:
                continue
            scope = [
                str(asset or "").strip().upper()
                for asset in row.get("asset_scope", []) or []
            ]
            if scope and not any(in_scope(asset) for asset in scope):
                continue
            accounts.append(row)
        signals = [
            row
            for row in center.get("signals", []) or []
            if isinstance(row, dict) and symbol_in_scope(str(row.get("symbol") or ""))
        ]
        center["strategy_instances"] = strategies
        center["user_api_accounts"] = accounts
        center["signals"] = signals
        summary = center.get("summary")
        if isinstance(summary, dict):
            summary["strategy_count"] = len(strategies)
            summary["enabled_count"] = sum(1 for row in strategies if row.get("enabled"))
            summary["live_count"] = sum(1 for row in strategies if row.get("live_enabled"))
            summary["api_account_count"] = len(accounts)
            summary["recent_signal_count"] = len(signals)
            summary["pnl_quote"] = sum(
                float(row.get("pnl_quote") or 0.0) for row in strategies
            )
            summary["open_order_count"] = sum(
                int(row.get("open_order_count") or 0) for row in strategies
            )

    if isinstance(payload.get("markets"), list):
        payload["markets"] = [
            row
            for row in payload["markets"]
            if in_scope(
                str(row.get("asset") or _base_asset_from_symbol(row.get("symbol", "")))
            )
        ]
    config_payload = payload.get("config")
    if isinstance(config_payload, dict) and isinstance(
        config_payload.get("spot_markets"),
        list,
    ):
        config_payload["spot_markets"] = [
            row
            for row in config_payload["spot_markets"]
            if in_scope(
                str(row.get("asset") or _base_asset_from_symbol(row.get("symbol", "")))
            )
        ]
    for key in ("opportunities", "recent_opportunities"):
        if isinstance(payload.get(key), list):
            payload[key] = [
                row
                for row in payload[key]
                if isinstance(row, dict) and in_scope(_opportunity_asset(row))
            ]
    portfolio = payload.get("portfolio")
    if isinstance(portfolio, dict) and isinstance(portfolio.get("positions"), list):
        portfolio["positions"] = [
            row
            for row in portfolio["positions"]
            if in_scope(str(row.get("asset") or ""))
        ]
        if isinstance(portfolio.get("cash_balances"), dict):
            portfolio["cash_balances"] = {
                currency: value
                for currency, value in portfolio["cash_balances"].items()
                if currency_in_scope(str(currency))
            }
    filter_strategy_section(payload.get("market_maker"))
    filter_strategy_section(payload.get("slow_execution"))
    filter_strategy_section(payload.get("spot_grid"))
    filter_strategy_section(payload.get("dca"))
    filter_strategy_section(payload.get("execution_algo"))
    filter_strategy_section(payload.get("backtest"))
    filter_order_activity(payload.get("order_activity"))
    filter_account_balances(payload.get("account_balances"))
    filter_trading_console(payload.get("trading_console"))
    filter_readiness(payload.get("readiness"))
    filter_strategy_center(payload.get("strategy_center"))
    payload["auth"] = user.public_dict(available_assets=available_assets)
    payload["auth"]["mode"] = "user"
    payload["auth"]["asset_scope"] = sorted(asset_scope)
    return payload
