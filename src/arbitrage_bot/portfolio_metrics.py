from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .config import AssetPosition, BotConfig
from .models import OrderBookSnapshot
from .pnl import build_portfolio_pnl
from .risk import portfolio_positions_base


PNL_SOURCE_LABELS = {
    "market_maker": "Market Maker",
    "arbitrage": "Arbitrage",
    "auto_buy_sell": "Auto Buy/Sell",
    "manual": "Manual",
    "unattributed": "Unattributed",
}


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    quote = quote.partition(":")[0]
    return base.upper(), quote.upper()


def _source_for_strategy(strategy: str, event_type: str = "") -> str:
    key = (strategy or event_type or "").lower()
    if key == "market_maker":
        return "market_maker"
    if key in {"slow_execution", "auto_buy_sell", "slow_execution_cancel"}:
        return "auto_buy_sell"
    if key in {"arbitrage", "spot_spread", "spot-spread", "cash_and_carry"}:
        return "arbitrage"
    if key.startswith("manual"):
        return "manual"
    return "unattributed"


def _pnl_source_row(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "label": PNL_SOURCE_LABELS.get(source, source),
        "trade_count": 0,
        "notional_common": 0.0,
        "fees_common": 0.0,
        "realized_pnl": 0.0,
    }


def _attribution_keys(
    exchange: str,
    symbol: str,
    order_id: str,
) -> list[str]:
    if not order_id:
        return []
    keys = []
    if exchange and symbol:
        keys.append(f"{exchange}|{symbol}|{order_id}")
    keys.append(order_id)
    return keys


def build_order_attribution_map(entries: Iterable[Any]) -> dict[str, dict[str, Any]]:
    attribution: dict[str, dict[str, Any]] = {}
    for entry in entries:
        source = _source_for_strategy(
            getattr(entry, "strategy", ""),
            getattr(entry, "event_type", ""),
        )
        row = {
            "source": source,
            "source_label": PNL_SOURCE_LABELS.get(source, source),
            "strategy": getattr(entry, "strategy", ""),
            "event_type": getattr(entry, "event_type", ""),
            "event_id": getattr(entry, "event_id", ""),
            "mode": getattr(entry, "mode", ""),
            "logged_at": getattr(entry, "logged_at", None),
        }
        exchange = getattr(entry, "exchange", "")
        symbol = getattr(entry, "symbol", "")
        for order_id in getattr(entry, "placed_order_ids", []) or []:
            for key in _attribution_keys(exchange, symbol, str(order_id)):
                attribution.setdefault(key, row)
    return attribution


def _trade_attribution(
    trade: dict[str, Any],
    attribution: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for key in _attribution_keys(
        str(trade.get("exchange") or ""),
        str(trade.get("symbol") or ""),
        str(trade.get("order_id") or ""),
    ):
        if key in attribution:
            return attribution[key]
    return None


def _configured_average_entry_prices(cfg: BotConfig) -> dict[str, float]:
    prices: dict[str, float] = {}
    if cfg.portfolio.asset:
        prices[cfg.portfolio.asset.upper()] = cfg.portfolio.average_entry_price
    for position in cfg.portfolio.positions:
        prices[position.asset.upper()] = position.average_entry_price
    return prices


def _configured_position_assets(cfg: BotConfig) -> set[str]:
    assets = {market.asset.upper() for market in cfg.spot_markets}
    if cfg.portfolio.asset:
        assets.add(cfg.portfolio.asset.upper())
    assets.update(position.asset.upper() for position in cfg.portfolio.positions)
    return assets


def _mark_prices_by_asset(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
) -> dict[str, float]:
    marks: dict[str, list[float]] = {}
    for market in cfg.spot_markets:
        book = books.get((market.exchange, market.symbol))
        rate = quote_rates.get(market.quote_currency)
        if book is None or rate is None or not book.bids or not book.asks:
            continue
        bid = book.bids[0].price
        ask = book.asks[0].price
        if bid <= 0 or ask <= 0 or bid >= ask:
            continue
        marks.setdefault(market.asset.upper(), []).append((bid + ask) / 2 * rate)
    return {
        asset: sum(values) / len(values)
        for asset, values in marks.items()
        if values
    }


def _fee_common_value(
    fee: dict[str, Any] | None,
    *,
    quote_rates: dict[str, float],
    mark_prices: dict[str, float],
) -> tuple[float | None, str | None]:
    if not fee:
        return 0.0, None
    cost = _number_or_none(fee.get("cost"))
    if cost is None:
        return 0.0, None
    currency = str(fee.get("currency") or "").upper()
    if not currency:
        return cost, None
    rate = quote_rates.get(currency)
    if rate is not None:
        return cost * rate, None
    mark = mark_prices.get(currency)
    if mark is not None:
        return cost * mark, None
    return None, currency


def enrich_recent_trades_with_pnl(
    cfg: BotConfig,
    trades: Iterable[dict[str, Any]],
    *,
    quote_rates: dict[str, float],
    books: dict[tuple[str, str], OrderBookSnapshot],
    attribution: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    attribution = attribution or {}
    average_prices = _configured_average_entry_prices(cfg)
    mark_prices = _mark_prices_by_asset(cfg, books, quote_rates)
    source_rows = {source: _pnl_source_row(source) for source in PNL_SOURCE_LABELS}
    missing_cost_basis: set[str] = set()
    missing_quote_rates: set[str] = set()
    missing_fee_rates: set[str] = set()
    enriched: list[dict[str, Any]] = []

    for trade in trades:
        row = dict(trade)
        match = _trade_attribution(row, attribution)
        source = match["source"] if match is not None else "unattributed"
        base, quote = _symbol_base_quote(str(row.get("symbol") or ""))
        side = str(row.get("side") or "").lower()
        price = _number_or_none(row.get("price"))
        amount = _number_or_none(row.get("amount"))
        cost = _number_or_none(row.get("cost"))
        if cost is None and price is not None and amount is not None:
            cost = price * amount
            row["cost"] = cost

        quote_rate = quote_rates.get(quote) if quote else None
        if quote and quote_rate is None:
            missing_quote_rates.add(quote)
        notional_common = (
            cost * quote_rate
            if cost is not None and quote_rate is not None
            else None
        )
        fee_common, missing_fee_currency = _fee_common_value(
            row.get("fee"),
            quote_rates=quote_rates,
            mark_prices=mark_prices,
        )
        if missing_fee_currency:
            missing_fee_rates.add(missing_fee_currency)

        realized_pnl: float | None = None
        fee_for_pnl = fee_common or 0.0
        if (
            side == "sell"
            and price is not None
            and amount is not None
            and quote_rate is not None
        ):
            average_entry = average_prices.get(base, 0.0)
            if average_entry > 0:
                realized_pnl = (
                    price * quote_rate - average_entry
                ) * amount - fee_for_pnl
            else:
                missing_cost_basis.add(base or row.get("symbol") or "")
                realized_pnl = -fee_for_pnl
        elif fee_common is not None:
            realized_pnl = -fee_common

        source_row = source_rows.setdefault(source, _pnl_source_row(source))
        source_row["trade_count"] += 1
        if notional_common is not None:
            source_row["notional_common"] += notional_common
        if fee_common is not None:
            source_row["fees_common"] += fee_common
        if realized_pnl is not None:
            source_row["realized_pnl"] += realized_pnl

        row.update(
            {
                "source": source,
                "source_label": PNL_SOURCE_LABELS.get(source, source),
                "attribution": match,
                "base_currency": base,
                "quote_currency": quote,
                "notional_common": notional_common,
                "fee_common": fee_common,
                "realized_pnl_common": realized_pnl,
            }
        )
        enriched.append(row)

    active_sources = {
        source: row
        for source, row in source_rows.items()
        if row["trade_count"] > 0 or abs(row["realized_pnl"]) >= 1e-12
    }
    total_realized = sum(row["realized_pnl"] for row in active_sources.values())
    total_fees = sum(row["fees_common"] for row in active_sources.values())
    total_notional = sum(row["notional_common"] for row in active_sources.values())
    summary = {
        "currency": cfg.common_quote_currency,
        "window": "recent_fills",
        "trade_count": len(enriched),
        "attributed_trade_count": sum(
            1 for row in enriched if row["source"] != "unattributed"
        ),
        "unattributed_trade_count": sum(
            1 for row in enriched if row["source"] == "unattributed"
        ),
        "total_realized_pnl": total_realized,
        "total_fees": total_fees,
        "total_notional": total_notional,
        "sources": active_sources,
        "missing_cost_basis": sorted(item for item in missing_cost_basis if item),
        "missing_quote_rates": sorted(missing_quote_rates),
        "missing_fee_rates": sorted(missing_fee_rates),
        "observed_at": time.time(),
    }
    return enriched, summary


def _base_currency_from_symbol(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[0].upper()


def _portfolio_position_for_symbol(
    portfolio_payload: dict[str, Any] | None,
    symbol: str,
    *,
    cfg: BotConfig | None = None,
) -> float | None:
    base = _base_currency_from_symbol(symbol)
    if not base:
        return None
    payload = portfolio_payload or {}
    positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    for position in positions:
        if not isinstance(position, dict):
            continue
        if str(position.get("asset") or "").upper() == base:
            value = _number_or_none(position.get("position_base"))
            if value is not None:
                return value
    if str(payload.get("asset") or "").upper() == base:
        value = _number_or_none(payload.get("position_base"))
        if value is not None:
            return value
    if cfg is not None:
        return portfolio_positions_base(cfg.portfolio).get(base)
    return None


def _account_balance_totals_by_currency(
    account_balances: dict[str, Any],
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in account_balances.get("totals", []):
        currency = str(row.get("currency", "")).upper()
        if not currency:
            continue
        value = row.get("total")
        if value is not None:
            totals[currency] = float(value)
    return totals


def _apply_order_activity_pnl(
    payload: dict[str, Any],
    order_activity: dict[str, Any] | None,
) -> dict[str, Any]:
    sources = {
        str(source): float(value or 0.0)
        for source, value in (payload.get("sources") or {}).items()
    }
    for source in (
        "market_maker",
        "arbitrage",
        "auto_buy_sell",
        "manual",
        "unattributed",
        "price_move",
    ):
        sources.setdefault(source, 0.0)
    payload["sources"] = sources

    summary = (order_activity or {}).get("daily_pnl")
    if not isinstance(summary, dict) or not summary.get("enabled"):
        summary = (order_activity or {}).get("pnl_summary")
    if not isinstance(summary, dict):
        return payload

    for source, row in (summary.get("sources") or {}).items():
        if not isinstance(row, dict):
            continue
        realized_pnl = _number_or_none(row.get("realized_pnl"))
        if realized_pnl is None:
            continue
        source_key = str(source)
        sources[source_key] = sources.get(source_key, 0.0) + realized_pnl

    payload["sources"] = sources
    payload["total_pnl"] = sum(sources.values())
    payload["fill_pnl_summary"] = summary
    payload["fill_pnl_window"] = summary.get("window") or "daily"
    payload["fill_pnl_day"] = summary.get("day")
    payload["fill_pnl_observed_at"] = summary.get("observed_at") or summary.get(
        "updated_at"
    )
    return payload


def build_synced_portfolio_pnl(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    account_balances: dict[str, Any],
    order_activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if int(account_balances.get("checked_account_count", 0) or 0) <= 0:
        payload = build_portfolio_pnl(cfg, books, quote_rates)
        payload["balance_source"] = "configured"
        return _apply_order_activity_pnl(payload, order_activity)

    totals_by_currency = _account_balance_totals_by_currency(account_balances)
    position_assets = _configured_position_assets(cfg)
    average_prices = _configured_average_entry_prices(cfg)
    positions = [
        AssetPosition(
            asset=asset,
            position_base=totals_by_currency.get(asset, 0.0),
            average_entry_price=average_prices.get(asset, 0.0),
        )
        for asset in sorted(position_assets)
    ]
    cash_balances = {
        currency: amount
        for currency, amount in sorted(totals_by_currency.items())
        if currency not in position_assets
    }
    live_portfolio = replace(
        cfg.portfolio,
        enabled=True,
        positions=positions,
        cash_balances=cash_balances,
    )
    payload = build_portfolio_pnl(
        replace(cfg, portfolio=live_portfolio),
        books,
        quote_rates,
    )
    payload["balance_source"] = "live_accounts"
    payload["balance_status"] = account_balances.get("status")
    payload["balance_observed_at"] = account_balances.get("last_finished")
    return _apply_order_activity_pnl(payload, order_activity)


def build_market_maker_quality_payload(
    order_activity: dict[str, Any] | None,
    market_maker: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    activity = order_activity or {}
    maker = market_maker or {}
    daily_pnl = (
        activity.get("daily_pnl")
        if isinstance(activity.get("daily_pnl"), dict)
        else {}
    )
    daily_sources = (
        daily_pnl.get("sources")
        if isinstance(daily_pnl.get("sources"), dict)
        else {}
    )
    daily_source = (
        daily_sources.get("market_maker")
        if isinstance(daily_sources.get("market_maker"), dict)
        else {}
    )
    recent_trades = [
        trade
        for trade in (activity.get("recent_trades") or [])
        if isinstance(trade, dict) and trade.get("source") == "market_maker"
    ]
    by_side = {
        "buy": {"trade_count": 0, "base_amount": 0.0, "quote_notional": 0.0},
        "sell": {"trade_count": 0, "base_amount": 0.0, "quote_notional": 0.0},
    }
    total_notional = 0.0
    total_fees = 0.0
    total_pnl = 0.0
    for trade in recent_trades:
        side = str(trade.get("side") or "").lower()
        if side not in by_side:
            continue
        amount = _number_or_none(trade.get("amount")) or 0.0
        notional = _number_or_none(trade.get("notional_common"))
        if notional is None:
            notional = _number_or_none(trade.get("cost")) or 0.0
        fee = _number_or_none(trade.get("fee_common")) or 0.0
        pnl = _number_or_none(trade.get("realized_pnl_common")) or 0.0
        by_side[side]["trade_count"] += 1
        by_side[side]["base_amount"] += amount
        by_side[side]["quote_notional"] += notional
        total_notional += notional
        total_fees += fee
        total_pnl += pnl

    for side in by_side.values():
        base_amount = side["base_amount"]
        side["average_price"] = (
            side["quote_notional"] / base_amount
            if base_amount > 0
            else None
        )
    buy_avg = by_side["buy"]["average_price"]
    sell_avg = by_side["sell"]["average_price"]
    plan = maker.get("plan") if isinstance(maker.get("plan"), dict) else {}
    if not plan and isinstance((maker.get("runtime") or {}).get("last_plan"), dict):
        plan = maker["runtime"]["last_plan"]
    mid_price = _number_or_none(plan.get("mid_price")) if isinstance(plan, dict) else None
    if buy_avg is not None and sell_avg is not None:
        spread_mid = mid_price or (buy_avg + sell_avg) / 2
        realized_spread_bps = (
            (sell_avg - buy_avg) / spread_mid * 10_000
            if spread_mid and spread_mid > 0
            else None
        )
    else:
        realized_spread_bps = None

    inventory = {
        "base": _number_or_none(plan.get("inventory_base")) if isinstance(plan, dict) else None,
        "target_base": _number_or_none(plan.get("inventory_target_base")) if isinstance(plan, dict) else None,
        "deviation_base": _number_or_none(plan.get("inventory_deviation_base")) if isinstance(plan, dict) else None,
        "buy_multiplier": _number_or_none(plan.get("inventory_buy_multiplier")) if isinstance(plan, dict) else None,
        "sell_multiplier": _number_or_none(plan.get("inventory_sell_multiplier")) if isinstance(plan, dict) else None,
        "active": bool(plan.get("inventory_control_active")) if isinstance(plan, dict) else False,
    }
    if inventory["base"] is None and isinstance(portfolio, dict):
        inventory["base"] = _portfolio_position_for_symbol(
            portfolio,
            str(plan.get("symbol") or ""),
        )
    recent_trade_count = len(recent_trades)
    daily = {
        "enabled": bool(daily_pnl.get("enabled")),
        "day": daily_pnl.get("day"),
        "currency": daily_pnl.get("currency"),
        "trade_count": int(_number_or_none(daily_source.get("trade_count")) or 0),
        "total_notional": _number_or_none(daily_source.get("notional_common")) or 0.0,
        "total_fees": _number_or_none(daily_source.get("fees_common")) or 0.0,
        "realized_pnl": _number_or_none(daily_source.get("realized_pnl")) or 0.0,
        "updated_at": daily_pnl.get("updated_at"),
    }
    use_daily_fallback = recent_trade_count == 0 and daily["trade_count"] > 0
    return {
        "window": "daily_pnl" if use_daily_fallback else "recent_fills",
        "recent_trade_count": recent_trade_count,
        "trade_count": daily["trade_count"] if use_daily_fallback else recent_trade_count,
        "buy": by_side["buy"],
        "sell": by_side["sell"],
        "total_notional": daily["total_notional"] if use_daily_fallback else total_notional,
        "total_fees": daily["total_fees"] if use_daily_fallback else total_fees,
        "realized_pnl": daily["realized_pnl"] if use_daily_fallback else total_pnl,
        "realized_spread_bps": realized_spread_bps,
        "daily": daily,
        "inventory": inventory,
        "observed_at": time.time(),
    }
