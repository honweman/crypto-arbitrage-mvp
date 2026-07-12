from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable
from dataclasses import replace
from typing import Any, Callable

from .config import ExchangeConfig, MarketMakerConfig, SpotGridConfig, SpotMarketConfig
from .exchanges import ExchangeManager
from .grid_trading import build_spot_grid_plan
from .market_making import build_symmetric_market_maker_plan
from .models import FillEstimate, OrderBookSnapshot
from .orderbook import available_base, estimate_fill, max_base_for_quote
from .strategies.spot_spread import find_converted_spot_spread_opportunities
from .user_account_check import workspace_exchange_config
from .user_paper_store import UserPaperStateConflict, UserPaperTradingStore
from .user_strategies import UserStrategy
from .user_workspace import UserExchangeAccount, UserProject, UserWorkspaceStore


LOGGER = logging.getLogger(__name__)

PAPER_SCAN_SECONDS = 1.0
PAPER_FETCH_TIMEOUT_SECONDS = 15.0
PAPER_ORDER_BOOK_DEPTH = 20
PAPER_MAX_FETCH_CONCURRENCY = 8


def _base_currency(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].strip().upper()


def _quote_currency(symbol: str) -> str:
    if "/" not in str(symbol or ""):
        return ""
    return str(symbol).split("/", 1)[1].split(":", 1)[0].strip().upper()


def _mid_price(book: OrderBookSnapshot) -> float:
    if not book.bids or not book.asks:
        raise ValueError("order book has no bid/ask")
    best_bid = float(book.bids[0].price)
    best_ask = float(book.asks[0].price)
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("order book top bid/ask are invalid")
    return (best_bid + best_ask) / 2


def _normalized_quote_rates(
    quote_rates: dict[str, float],
    common_quote_currency: str,
) -> dict[str, float]:
    rates: dict[str, float] = {}
    for currency, raw_rate in quote_rates.items():
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0:
            rates[str(currency).strip().upper()] = rate
    rates[str(common_quote_currency).strip().upper()] = 1.0
    return rates


def _strategy_interval(strategy: UserStrategy) -> float:
    parameters = strategy.parameters
    if strategy.strategy_type == "market_maker":
        return max(1.0, float(parameters["refresh_seconds"]))
    if strategy.strategy_type in {"auto_buy_sell", "dca"}:
        return max(1.0, float(parameters["interval_seconds"]))
    if strategy.strategy_type == "spot_grid":
        return max(1.0, float(parameters["refresh_seconds"]))
    return max(0.1, float(parameters["scan_interval_seconds"]))


def strategy_paper_fingerprint(
    strategy: UserStrategy,
    project: UserProject,
    accounts: list[UserExchangeAccount],
) -> str:
    payload = {
        "strategy": {
            "id": strategy.id,
            "type": strategy.strategy_type,
            "account_ids": strategy.account_ids,
            "parameters": strategy.parameters,
            "risk": strategy.risk,
        },
        "project": {
            "id": project.id,
            "asset": project.asset,
            "quote_currency": project.quote_currency,
        },
        "accounts": [
            {
                "id": account.id,
                "exchange": account.exchange,
                "market_type": account.market_type,
                "api_variant": account.api_variant,
                "symbol": account.symbol,
            }
            for account in accounts
        ],
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _new_state(
    strategy: UserStrategy,
    project: UserProject,
    *,
    fingerprint: str,
    now: float,
    common_quote_currency: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "strategy_id": strategy.id,
        "owner_email": strategy.owner_email,
        "project_id": project.id,
        "strategy_type": strategy.strategy_type,
        "run_id": f"paper-{uuid.uuid4().hex[:16]}",
        "config_fingerprint": fingerprint,
        "strategy_updated_at": strategy.updated_at,
        "mode": "paper",
        "live_submit_allowed": False,
        "capital_model": "synthetic_from_max_total_quote",
        "status": "starting",
        "reason": "paper simulation is initializing",
        "terminal": False,
        "created_at": now,
        "updated_at": now,
        "last_cycle_at": None,
        "next_due_at": now,
        "common_quote_currency": common_quote_currency,
        "wallets": {},
        "open_orders": [],
        "open_order_count": 0,
        "fill_count": 0,
        "strategy_filled_quote": 0.0,
        "traded_quote_common": 0.0,
        "fees_common": 0.0,
        "realized_pnl_common": 0.0,
        "unrealized_pnl_common": 0.0,
        "total_pnl_common": 0.0,
        "daily_pnl_common": 0.0,
        "initial_equity_common": 0.0,
        "equity_common": 0.0,
        "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "day_start_equity_common": 0.0,
        "position_base": 0.0,
        "last_prices": {},
        "last_event_key": "",
    }


def _validate_book(
    book: OrderBookSnapshot,
    *,
    max_age_seconds: float,
    now: float,
) -> str | None:
    try:
        _mid_price(book)
    except ValueError as exc:
        return str(exc)
    received_age = now - float(book.received_at)
    if received_age < -5.0:
        return "order book received_at is in the future"
    if max_age_seconds > 0 and received_age > max_age_seconds:
        return f"order book is stale ({received_age:.2f}s)"
    if book.timestamp_ms is not None and float(book.timestamp_ms) > 0:
        exchange_age = now - float(book.timestamp_ms) / 1000
        if exchange_age < -30.0:
            return "exchange order book timestamp is in the future"
        if max_age_seconds > 0 and exchange_age > max_age_seconds:
            return f"exchange order book is stale ({exchange_age:.2f}s)"
    return None


def _initialize_wallets(
    state: dict[str, Any],
    strategy: UserStrategy,
    project: UserProject,
    accounts: list[UserExchangeAccount],
    books: dict[str, OrderBookSnapshot],
    quote_rates: dict[str, float],
    *,
    now: float,
) -> None:
    project_rate = quote_rates.get(project.quote_currency)
    if project_rate is None:
        raise ValueError(f"quote rate is missing: {project.quote_currency}")
    capital_common = float(strategy.risk["max_total_quote"]) * project_rate
    if capital_common <= 0:
        raise ValueError("paper capital must be greater than zero")

    directional_side = str(strategy.parameters.get("side") or "")
    fee_multiplier = 1 + float(strategy.risk["paper_fee_bps"]) / 10_000
    wallets: dict[str, dict[str, Any]] = {}
    for account in accounts:
        book = books[account.id]
        mid = _mid_price(book)
        quote = _quote_currency(account.symbol)
        rate = quote_rates.get(quote)
        if rate is None:
            raise ValueError(f"quote rate is missing: {quote}")
        local_capital = capital_common / rate
        if strategy.strategy_type in {"auto_buy_sell", "dca"}:
            initial_quote = (
                local_capital * fee_multiplier if directional_side == "buy" else 0.0
            )
            initial_base = local_capital / mid if directional_side == "sell" else 0.0
        else:
            initial_quote = local_capital * fee_multiplier
            initial_base = local_capital / mid
        wallets[account.id] = {
            "account_id": account.id,
            "exchange": account.exchange,
            "symbol": account.symbol,
            "base_currency": _base_currency(account.symbol),
            "quote_currency": quote,
            "quote_rate": rate,
            "base_balance": initial_base,
            "quote_balance": initial_quote,
            "base_cost_quote": initial_base * mid,
            "initial_base": initial_base,
            "initial_quote": initial_quote,
            "initial_mid_price": mid,
            "average_cost": mid if initial_base > 0 else 0.0,
        }
    state["wallets"] = wallets
    _refresh_valuation(state, books, now=now, initialize=True)


def _refresh_valuation(
    state: dict[str, Any],
    books: dict[str, OrderBookSnapshot],
    *,
    now: float,
    initialize: bool = False,
) -> None:
    equity_common = 0.0
    position_base = 0.0
    last_prices = dict(state.get("last_prices") or {})
    for account_id, wallet in (state.get("wallets") or {}).items():
        book = books.get(account_id)
        if book is not None:
            mid = _mid_price(book)
            last_prices[account_id] = {
                "best_bid": book.bids[0].price,
                "best_ask": book.asks[0].price,
                "mid_price": mid,
                "timestamp_ms": book.timestamp_ms,
                "received_at": book.received_at,
            }
        else:
            mid = float((last_prices.get(account_id) or {}).get("mid_price") or 0.0)
        rate = float(wallet["quote_rate"])
        base_balance = float(wallet["base_balance"])
        quote_balance = float(wallet["quote_balance"])
        equity_common += (quote_balance + base_balance * mid) * rate
        position_base += base_balance
        wallet["average_cost"] = (
            float(wallet["base_cost_quote"]) / base_balance
            if base_balance > 1e-15
            else 0.0
        )
    state["last_prices"] = last_prices
    state["equity_common"] = equity_common
    state["position_base"] = position_base
    if initialize or float(state.get("initial_equity_common") or 0.0) <= 0:
        state["initial_equity_common"] = equity_common
        state["day_start_equity_common"] = equity_common
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if state.get("day") != day:
        state["day"] = day
        state["day_start_equity_common"] = equity_common
    total_pnl = equity_common - float(state.get("initial_equity_common") or 0.0)
    state["total_pnl_common"] = total_pnl
    state["daily_pnl_common"] = equity_common - float(
        state.get("day_start_equity_common") or equity_common
    )
    state["unrealized_pnl_common"] = total_pnl - float(
        state.get("realized_pnl_common") or 0.0
    )
    state["open_order_count"] = len(state.get("open_orders") or [])


def _update_wallet_quote_rates(
    state: dict[str, Any],
    quote_rates: dict[str, float],
) -> None:
    for wallet in (state.get("wallets") or {}).values():
        quote = str(wallet.get("quote_currency") or "").upper()
        rate = quote_rates.get(quote)
        if rate is None:
            raise ValueError(f"quote rate is missing: {quote}")
        wallet["quote_rate"] = rate


def _apply_fill(
    strategy: UserStrategy,
    state: dict[str, Any],
    account: UserExchangeAccount,
    *,
    side: str,
    price: float,
    amount: float,
    fee_bps: float,
    filled_at: float,
    fill_kind: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if side not in {"buy", "sell"} or price <= 0 or amount <= 0:
        return None, "paper fill parameters are invalid"
    wallet = (state.get("wallets") or {}).get(account.id)
    if not isinstance(wallet, dict):
        return None, f"paper wallet is missing: {account.label}"
    gross_quote = price * amount
    fee_quote = gross_quote * fee_bps / 10_000
    realized_quote = 0.0
    epsilon = max(1e-12, gross_quote * 1e-12)
    if side == "buy":
        total_quote = gross_quote + fee_quote
        if float(wallet["quote_balance"]) + epsilon < total_quote:
            return None, f"paper quote balance is insufficient: {account.label}"
        wallet["quote_balance"] = max(0.0, float(wallet["quote_balance"]) - total_quote)
        wallet["base_balance"] = float(wallet["base_balance"]) + amount
        wallet["base_cost_quote"] = float(wallet["base_cost_quote"]) + total_quote
    else:
        base_balance = float(wallet["base_balance"])
        if base_balance + epsilon < amount:
            return None, f"paper base balance is insufficient: {account.label}"
        average_cost = (
            float(wallet["base_cost_quote"]) / base_balance
            if base_balance > 1e-15
            else 0.0
        )
        removed_cost = average_cost * amount
        wallet["base_balance"] = max(0.0, base_balance - amount)
        wallet["base_cost_quote"] = max(
            0.0,
            float(wallet["base_cost_quote"]) - removed_cost,
        )
        wallet["quote_balance"] = (
            float(wallet["quote_balance"]) + gross_quote - fee_quote
        )
        realized_quote = gross_quote - fee_quote - removed_cost
    rate = float(wallet["quote_rate"])
    realized_common = realized_quote * rate
    state["fill_count"] = int(state.get("fill_count") or 0) + 1
    state["traded_quote_common"] = (
        float(state.get("traded_quote_common") or 0.0) + gross_quote * rate
    )
    state["fees_common"] = float(state.get("fees_common") or 0.0) + fee_quote * rate
    state["realized_pnl_common"] = (
        float(state.get("realized_pnl_common") or 0.0) + realized_common
    )
    wallet["average_cost"] = (
        float(wallet["base_cost_quote"]) / float(wallet["base_balance"])
        if float(wallet["base_balance"]) > 1e-15
        else 0.0
    )
    fill_id = f"paper-fill-{uuid.uuid4().hex}"
    return (
        {
            "fill_id": fill_id,
            "strategy_id": strategy.id,
            "run_id": state["run_id"],
            "owner_email": strategy.owner_email,
            "project_id": strategy.project_id,
            "account_id": account.id,
            "exchange": account.exchange,
            "symbol": account.symbol,
            "side": side,
            "price": price,
            "amount": amount,
            "gross_quote": gross_quote,
            "fee_quote": fee_quote,
            "quote_currency": wallet["quote_currency"],
            "quote_rate": rate,
            "gross_common": gross_quote * rate,
            "fee_common": fee_quote * rate,
            "realized_pnl_common": realized_common,
            "fill_kind": fill_kind,
            "filled_at": filled_at,
            "mode": "paper",
            "live_submit_allowed": False,
        },
        None,
    )


def _slippage_bps(book: OrderBookSnapshot, side: str, average_price: float) -> float:
    top = book.asks[0].price if side == "buy" else book.bids[0].price
    if top <= 0:
        return math.inf
    if side == "buy":
        return max(0.0, (average_price - top) / top * 10_000)
    return max(0.0, (top - average_price) / top * 10_000)


def _market_fill_estimate(
    state: dict[str, Any],
    account: UserExchangeAccount,
    book: OrderBookSnapshot,
    *,
    side: str,
    quote_target: float,
    fee_bps: float,
) -> tuple[FillEstimate | None, str | None]:
    wallet = state["wallets"][account.id]
    if side == "buy":
        available_quote = float(wallet["quote_balance"])
        gross_budget = min(quote_target, available_quote / (1 + fee_bps / 10_000))
        quantity = max_base_for_quote(book.asks, gross_budget)
        levels = book.asks
    else:
        top_bid = book.bids[0].price
        desired = quote_target / top_bid
        quantity = min(
            desired,
            float(wallet["base_balance"]),
            available_base(book.bids),
        )
        levels = book.bids
    if quantity <= 1e-15:
        return None, "paper balance or visible order book depth is insufficient"
    fill = estimate_fill(levels, side=side, quantity_base=quantity, fee_bps=fee_bps)
    if fill is None:
        return None, "visible order book depth cannot fill the paper order"
    return fill, None


def _crossed_limit_fills(
    strategy: UserStrategy,
    state: dict[str, Any],
    account: UserExchangeAccount,
    book: OrderBookSnapshot,
    *,
    now: float,
) -> list[dict[str, Any]]:
    fee_bps = float(strategy.risk["paper_fee_bps"])
    buy_liquidity = float(book.asks[0].amount)
    sell_liquidity = float(book.bids[0].amount)
    fills: list[dict[str, Any]] = []
    for order in list(state.get("open_orders") or []):
        if order.get("account_id") != account.id:
            continue
        side = str(order.get("side") or "")
        price = float(order.get("price") or 0.0)
        remaining = float(order.get("amount") or 0.0)
        crossed = (side == "buy" and book.asks[0].price <= price) or (
            side == "sell" and book.bids[0].price >= price
        )
        if not crossed or remaining <= 0:
            continue
        wallet = state["wallets"][account.id]
        if side == "buy":
            balance_capacity = float(wallet["quote_balance"]) / (
                price * (1 + fee_bps / 10_000)
            )
            amount = min(remaining, buy_liquidity, balance_capacity)
            buy_liquidity -= amount
        else:
            amount = min(remaining, sell_liquidity, float(wallet["base_balance"]))
            sell_liquidity -= amount
        if amount <= 1e-15:
            continue
        fill, _ = _apply_fill(
            strategy,
            state,
            account,
            side=side,
            price=price,
            amount=amount,
            fee_bps=fee_bps,
            filled_at=now,
            fill_kind=str(order.get("kind") or "limit"),
        )
        if fill is not None:
            fills.append(fill)
    return fills


def _fund_limit_orders(
    state: dict[str, Any],
    account: UserExchangeAccount,
    orders: list[dict[str, Any]],
    *,
    max_total_quote: float,
    max_open_orders: int,
    fee_bps: float,
    kind: str,
) -> list[dict[str, Any]]:
    wallet = state["wallets"][account.id]
    available_quote = float(wallet["quote_balance"])
    available_base = float(wallet["base_balance"])
    used_quote = 0.0
    used_base = 0.0
    planned_quote = 0.0
    funded: list[dict[str, Any]] = []
    for raw in orders:
        if len(funded) >= max_open_orders:
            break
        side = str(raw.get("side") or "")
        price = float(raw.get("price") or 0.0)
        amount = float(raw.get("amount") or 0.0)
        quote_notional = float(raw.get("quote_notional") or price * amount)
        if price <= 0 or amount <= 0 or quote_notional <= 0:
            continue
        if planned_quote + quote_notional > max_total_quote + 1e-12:
            continue
        if side == "buy":
            reserve = quote_notional * (1 + fee_bps / 10_000)
            if used_quote + reserve > available_quote + 1e-12:
                continue
            used_quote += reserve
        elif side == "sell":
            if used_base + amount > available_base + 1e-12:
                continue
            used_base += amount
        else:
            continue
        planned_quote += quote_notional
        funded.append(
            {
                "paper_order_id": f"paper-order-{uuid.uuid4().hex}",
                "account_id": account.id,
                "exchange": account.exchange,
                "symbol": account.symbol,
                "side": side,
                "price": price,
                "amount": amount,
                "quote_notional": quote_notional,
                "level": int(raw.get("level") or 0),
                "kind": kind,
            }
        )
    return funded


def _simulate_market_maker(
    strategy: UserStrategy,
    state: dict[str, Any],
    account: UserExchangeAccount,
    book: OrderBookSnapshot,
    *,
    now: float,
) -> tuple[list[dict[str, Any]], str, str, str]:
    fills = _crossed_limit_fills(strategy, state, account, book, now=now)
    parameters = strategy.parameters
    config = MarketMakerConfig(
        enabled=True,
        live_enabled=False,
        exchange=account.id,
        symbol=account.symbol,
        levels=int(parameters["levels"]),
        price_band_pct=float(parameters["price_band_pct"]),
        quote_per_level=float(parameters["quote_per_level"]),
        depth_shape="linear",
        poll_seconds=float(parameters["refresh_seconds"]),
        post_only=bool(parameters["post_only"]),
    )
    plan = build_symmetric_market_maker_plan(book, config)
    funded = _fund_limit_orders(
        state,
        account,
        [order.to_dict() for order in plan.orders],
        max_total_quote=float(strategy.risk["max_total_quote"]),
        max_open_orders=int(strategy.risk["max_open_orders"]),
        fee_bps=float(strategy.risk["paper_fee_bps"]),
        kind="market_maker",
    )
    state["open_orders"] = funded
    if not funded:
        return (
            fills,
            "blocked_balance",
            "paper balances cannot fund MM orders",
            "blocked",
        )
    if fills:
        return (
            fills,
            "orders_active",
            f"filled {len(fills)} and rebuilt MM orders",
            "fill",
        )
    return (
        fills,
        "orders_active",
        "MM paper orders rebuilt from current book",
        "orders_rebuilt",
    )


def _simulate_grid(
    strategy: UserStrategy,
    state: dict[str, Any],
    account: UserExchangeAccount,
    book: OrderBookSnapshot,
    *,
    now: float,
) -> tuple[list[dict[str, Any]], str, str, str]:
    fills = _crossed_limit_fills(strategy, state, account, book, now=now)
    parameters = strategy.parameters
    config = SpotGridConfig(
        enabled=True,
        live_enabled=False,
        exchange=account.id,
        symbol=account.symbol,
        lower_price=float(parameters["lower_price"]),
        upper_price=float(parameters["upper_price"]),
        grid_count=int(parameters["grid_count"]),
        spacing=str(parameters["spacing"]),
        quote_per_grid=float(parameters["quote_per_grid"]),
        auto_rebuild=True,
        max_open_orders=int(strategy.risk["max_open_orders"]),
        min_grid_step_bps=0.0,
        post_only=True,
    )
    plan = build_spot_grid_plan(book, config)
    if plan.status != "planned":
        state["open_orders"] = []
        return fills, "waiting", plan.reason, "waiting"
    funded = _fund_limit_orders(
        state,
        account,
        [order.to_dict() for order in plan.orders],
        max_total_quote=float(strategy.risk["max_total_quote"]),
        max_open_orders=int(strategy.risk["max_open_orders"]),
        fee_bps=float(strategy.risk["paper_fee_bps"]),
        kind="spot_grid",
    )
    state["open_orders"] = funded
    if not funded:
        return (
            fills,
            "blocked_balance",
            "paper balances cannot fund grid orders",
            "blocked",
        )
    if fills:
        return (
            fills,
            "orders_active",
            f"filled {len(fills)} and rebuilt grid orders",
            "fill",
        )
    return (
        fills,
        "orders_active",
        "grid paper orders rebuilt from current book",
        "orders_rebuilt",
    )


def _simulate_directional(
    strategy: UserStrategy,
    state: dict[str, Any],
    account: UserExchangeAccount,
    book: OrderBookSnapshot,
    *,
    now: float,
) -> tuple[list[dict[str, Any]], str, str, str]:
    parameters = strategy.parameters
    side = str(parameters["side"])
    price = book.asks[0].price if side == "buy" else book.bids[0].price
    total_quote = float(parameters["total_quote"])
    filled_quote = float(state.get("strategy_filled_quote") or 0.0)
    remaining_quote = max(0.0, total_quote - filled_quote)
    state["target_quote"] = total_quote
    state["remaining_quote"] = remaining_quote
    state["progress_pct"] = min(100.0, filled_quote / total_quote * 100)
    if remaining_quote <= 1e-12:
        state["terminal"] = True
        return [], "complete", "paper quote target is complete", "complete"

    if strategy.strategy_type == "auto_buy_sell":
        stop_price = float(parameters["stop_price"])
        if stop_price > 0 and (
            (side == "buy" and price >= stop_price)
            or (side == "sell" and price <= stop_price)
        ):
            state["terminal"] = True
            return (
                [],
                "stopped_by_price",
                "configured stop price was reached",
                "stopped",
            )
        start_price = float(parameters["start_price"])
        if start_price > 0 and (
            (side == "buy" and price > start_price)
            or (side == "sell" and price < start_price)
        ):
            return [], "waiting", "configured start price is not reached", "waiting"
    else:
        wallet = state["wallets"][account.id]
        average_cost = float(wallet.get("average_cost") or 0.0)
        take_profit_pct = float(parameters["take_profit_pct"])
        if (
            int(state.get("fill_count") or 0) > 0
            and average_cost > 0
            and take_profit_pct > 0
        ):
            take_profit_reached = (
                side == "buy"
                and book.bids[0].price >= average_cost * (1 + take_profit_pct / 100)
            ) or (
                side == "sell"
                and book.asks[0].price <= average_cost * (1 - take_profit_pct / 100)
            )
            if take_profit_reached:
                state["terminal"] = True
                return (
                    [],
                    "complete",
                    "DCA take-profit condition was reached",
                    "complete",
                )
        trigger_price = float(parameters["trigger_price"])
        if trigger_price > 0 and (
            (side == "buy" and price > trigger_price)
            or (side == "sell" and price < trigger_price)
        ):
            return [], "waiting", "DCA trigger price is not reached", "waiting"

    quote_target = min(
        float(parameters["quote_per_order"]),
        remaining_quote,
        float(strategy.risk["max_order_quote"]),
    )
    fee_bps = float(strategy.risk["paper_fee_bps"])
    estimate, error = _market_fill_estimate(
        state,
        account,
        book,
        side=side,
        quote_target=quote_target,
        fee_bps=fee_bps,
    )
    if estimate is None:
        return [], "blocked_balance", error or "paper fill is unavailable", "blocked"
    slippage = _slippage_bps(book, side, estimate.average_price)
    state["last_slippage_bps"] = slippage
    if slippage > float(strategy.risk["max_slippage_bps"]):
        return [], "blocked_slippage", "paper fill exceeds max slippage", "blocked"
    fill, error = _apply_fill(
        strategy,
        state,
        account,
        side=side,
        price=estimate.average_price,
        amount=estimate.quantity_base,
        fee_bps=fee_bps,
        filled_at=now,
        fill_kind=strategy.strategy_type,
    )
    if fill is None:
        return (
            [],
            "blocked_balance",
            error or "paper balance is insufficient",
            "blocked",
        )
    state["strategy_filled_quote"] = filled_quote + estimate.gross_quote
    state["remaining_quote"] = max(
        0.0,
        total_quote - float(state["strategy_filled_quote"]),
    )
    state["progress_pct"] = min(
        100.0,
        float(state["strategy_filled_quote"]) / total_quote * 100,
    )
    if state["remaining_quote"] <= 1e-12:
        state["terminal"] = True
        return [fill], "complete", "paper quote target is complete", "fill"
    return [fill], "running", "paper market order filled", "fill"


def _paper_exchange_config(
    account: UserExchangeAccount,
    *,
    fee_bps: float,
) -> ExchangeConfig:
    return replace(
        workspace_exchange_config(
            exchange=account.exchange,
            market_type=account.market_type,
            api_variant=account.api_variant,
            runtime_key=f"paper:{account.id}",
        ),
        fee_bps=fee_bps,
    )


def _simulate_spot_spread(
    strategy: UserStrategy,
    state: dict[str, Any],
    project: UserProject,
    accounts: list[UserExchangeAccount],
    books: dict[str, OrderBookSnapshot],
    quote_rates: dict[str, float],
    common_quote_currency: str,
    *,
    now: float,
) -> tuple[list[dict[str, Any]], str, str, str, dict[str, Any]]:
    fee_bps = float(strategy.risk["paper_fee_bps"])
    configs = [_paper_exchange_config(account, fee_bps=fee_bps) for account in accounts]
    config_by_account = {account.id: cfg for account, cfg in zip(accounts, configs)}
    account_by_key = {
        config_by_account[account.id].key: account for account in accounts
    }
    markets = [
        SpotMarketConfig(
            asset=project.asset,
            exchange=config_by_account[account.id].key,
            symbol=account.symbol,
            quote_currency=_quote_currency(account.symbol),
        )
        for account in accounts
    ]
    keyed_books = {
        (config_by_account[account.id].key, account.symbol): books[account.id]
        for account in accounts
    }
    project_rate = quote_rates.get(project.quote_currency)
    if project_rate is None:
        return (
            [],
            "blocked_quote_rate",
            "project quote rate is unavailable",
            "blocked",
            {},
        )
    opportunities = find_converted_spot_spread_opportunities(
        keyed_books,
        configs,
        markets,
        notional_quote=float(strategy.parameters["max_cycle_quote"]) * project_rate,
        min_profit_quote=0.0,
        min_profit_bps=float(strategy.parameters["min_profit_bps"]),
        quote_rates=quote_rates,
        common_quote_currency=common_quote_currency,
    )
    if not opportunities:
        return (
            [],
            "waiting",
            "no spread exceeds the configured paper threshold",
            "waiting",
            {},
        )
    opportunity = opportunities[0]
    max_slippage = float(strategy.risk["max_slippage_bps"])
    for leg in opportunity.legs:
        account = account_by_key[leg.exchange]
        book = books[account.id]
        if _slippage_bps(book, leg.side, leg.average_price) > max_slippage:
            return (
                [],
                "blocked_slippage",
                "arbitrage leg exceeds max slippage",
                "blocked",
                {},
            )

    candidate_state = copy.deepcopy(state)
    candidate_fills: list[dict[str, Any]] = []
    for leg in opportunity.legs:
        account = account_by_key[leg.exchange]
        fill, error = _apply_fill(
            strategy,
            candidate_state,
            account,
            side=leg.side,
            price=leg.average_price,
            amount=leg.quantity_base,
            fee_bps=fee_bps,
            filled_at=now,
            fill_kind="spot_spread",
        )
        if fill is None:
            return (
                [],
                "blocked_balance",
                error or "arbitrage paper balance is insufficient",
                "blocked",
                {},
            )
        candidate_fills.append(fill)
    state.clear()
    state.update(candidate_state)
    metrics = {
        "profit_quote": opportunity.profit_quote,
        "profit_bps": opportunity.profit_bps,
        "buy_exchange": opportunity.legs[0].exchange,
        "sell_exchange": opportunity.legs[1].exchange,
    }
    return (
        candidate_fills,
        "running",
        f"paper arbitrage filled at {opportunity.profit_bps:.2f} bps",
        "fill",
        metrics,
    )


def _paper_event(
    strategy: UserStrategy,
    state: dict[str, Any],
    *,
    event_type: str,
    status: str,
    reason: str,
    now: float,
    fills: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    fill_ids = [fill["fill_id"] for fill in fills]
    identity = {
        "run_id": state["run_id"],
        "event_type": event_type,
        "status": status,
        "reason": reason,
        "fill_ids": fill_ids,
    }
    raw = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    event_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    if not fills and event_key == state.get("last_event_key"):
        return None
    state["last_event_key"] = event_key
    event_metrics = {
        "fill_count": len(fills),
        "open_order_count": len(state.get("open_orders") or []),
        "position_base": float(state.get("position_base") or 0.0),
        "total_pnl_common": float(state.get("total_pnl_common") or 0.0),
        "daily_pnl_common": float(state.get("daily_pnl_common") or 0.0),
        "fees_common": float(state.get("fees_common") or 0.0),
    }
    event_metrics.update(metrics or {})
    return {
        "event_key": event_key,
        "strategy_id": strategy.id,
        "run_id": state["run_id"],
        "owner_email": strategy.owner_email,
        "project_id": strategy.project_id,
        "event_type": event_type,
        "status": status,
        "reason": reason,
        "created_at": now,
        "account_ids": list(strategy.account_ids),
        "metrics": event_metrics,
        "mode": "paper",
        "live_submit_allowed": False,
    }


def simulate_user_paper_cycle(
    strategy: UserStrategy,
    project: UserProject,
    accounts: list[UserExchangeAccount],
    books: dict[str, OrderBookSnapshot],
    existing_state: dict[str, Any] | None,
    *,
    quote_rates: dict[str, float],
    common_quote_currency: str,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    cycle_at = time.time() if now is None else float(now)
    rates = _normalized_quote_rates(quote_rates, common_quote_currency)
    fingerprint = strategy_paper_fingerprint(strategy, project, accounts)
    reset = (
        existing_state is None
        or existing_state.get("config_fingerprint") != fingerprint
        or existing_state.get("common_quote_currency")
        != str(common_quote_currency).upper()
        or not existing_state.get("wallets")
    )
    state = (
        _new_state(
            strategy,
            project,
            fingerprint=fingerprint,
            now=cycle_at,
            common_quote_currency=common_quote_currency,
        )
        if reset
        else copy.deepcopy(existing_state)
    )
    state["strategy_updated_at"] = strategy.updated_at
    for account in accounts:
        book = books.get(account.id)
        if book is None:
            raise ValueError(f"order book is unavailable: {account.label}")
        error = _validate_book(
            book,
            max_age_seconds=float(strategy.risk["max_order_book_age_seconds"]),
            now=cycle_at,
        )
        if error:
            raise ValueError(f"{account.label}: {error}")
    if reset:
        _initialize_wallets(
            state,
            strategy,
            project,
            accounts,
            books,
            rates,
            now=cycle_at,
        )
        state["created_at"] = cycle_at
        state["day"] = time.strftime("%Y-%m-%d", time.gmtime(cycle_at))
    _update_wallet_quote_rates(state, rates)
    _refresh_valuation(state, books, now=cycle_at)

    project_rate = rates.get(project.quote_currency)
    if project_rate is None:
        raise ValueError(f"quote rate is missing: {project.quote_currency}")
    daily_loss_common = float(strategy.risk["max_daily_loss_quote"]) * project_rate
    if float(state.get("daily_pnl_common") or 0.0) <= -daily_loss_common:
        state["open_orders"] = []
        state["open_order_count"] = 0
        state.update(
            {
                "status": "blocked_daily_loss",
                "reason": "paper daily loss limit was reached",
                "terminal": True,
                "last_cycle_at": cycle_at,
                "next_due_at": None,
                "updated_at": cycle_at,
            }
        )
        event = _paper_event(
            strategy,
            state,
            event_type="blocked",
            status=state["status"],
            reason=state["reason"],
            now=cycle_at,
            fills=[],
        )
        return state, [], event
    if state.get("terminal"):
        state["last_cycle_at"] = cycle_at
        state["updated_at"] = cycle_at
        return state, [], None

    fills: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    if strategy.strategy_type == "market_maker":
        fills, status, reason, event_type = _simulate_market_maker(
            strategy,
            state,
            accounts[0],
            books[accounts[0].id],
            now=cycle_at,
        )
    elif strategy.strategy_type == "spot_grid":
        fills, status, reason, event_type = _simulate_grid(
            strategy,
            state,
            accounts[0],
            books[accounts[0].id],
            now=cycle_at,
        )
    elif strategy.strategy_type in {"auto_buy_sell", "dca"}:
        fills, status, reason, event_type = _simulate_directional(
            strategy,
            state,
            accounts[0],
            books[accounts[0].id],
            now=cycle_at,
        )
    else:
        fills, status, reason, event_type, metrics = _simulate_spot_spread(
            strategy,
            state,
            project,
            accounts,
            books,
            rates,
            common_quote_currency,
            now=cycle_at,
        )

    _refresh_valuation(state, books, now=cycle_at)
    state.update(
        {
            "strategy_updated_at": strategy.updated_at,
            "status": status,
            "reason": reason,
            "last_cycle_at": cycle_at,
            "next_due_at": (
                None
                if state.get("terminal")
                else cycle_at + _strategy_interval(strategy)
            ),
            "updated_at": cycle_at,
        }
    )
    if float(state.get("daily_pnl_common") or 0.0) <= -daily_loss_common:
        state["status"] = "blocked_daily_loss"
        state["reason"] = "paper daily loss limit was reached"
        state["terminal"] = True
        state["next_due_at"] = None
        state["open_orders"] = []
        state["open_order_count"] = 0
        event_type = "blocked"
    event = _paper_event(
        strategy,
        state,
        event_type=event_type,
        status=state["status"],
        reason=state["reason"],
        now=cycle_at,
        fills=fills,
        metrics=metrics,
    )
    return state, fills, event


def _nonrunning_state(
    strategy: UserStrategy,
    project: UserProject,
    existing_state: dict[str, Any] | None,
    *,
    status: str,
    reason: str,
    common_quote_currency: str,
    now: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = (
        copy.deepcopy(existing_state)
        if existing_state is not None
        else _new_state(
            strategy,
            project,
            fingerprint="pending",
            now=now,
            common_quote_currency=common_quote_currency,
        )
    )
    state.update(
        {
            "status": status,
            "reason": reason,
            "open_orders": [],
            "open_order_count": 0,
            "last_cycle_at": now,
            "next_due_at": now + _strategy_interval(strategy),
            "updated_at": now,
        }
    )
    event = _paper_event(
        strategy,
        state,
        event_type="blocked" if status.startswith("blocked") else status,
        status=status,
        reason=reason,
        now=now,
        fills=[],
    )
    return state, event


class UserPaperTradingService:
    def __init__(
        self,
        workspace_store: UserWorkspaceStore,
        paper_store: UserPaperTradingStore,
        *,
        quote_rates: dict[str, float],
        common_quote_currency: str,
        manager_factory: Callable[..., ExchangeManager] = ExchangeManager,
        order_book_depth: int = PAPER_ORDER_BOOK_DEPTH,
        fetch_timeout_seconds: float = PAPER_FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_store = workspace_store
        self.paper_store = paper_store
        self.quote_rates = dict(quote_rates)
        self.common_quote_currency = str(common_quote_currency).upper()
        self.order_book_depth = max(1, int(order_book_depth))
        self.fetch_timeout_seconds = max(1.0, float(fetch_timeout_seconds))
        self.manager = manager_factory()
        self._run_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.manager.close()

    def update_quote_rates(self, quote_rates: dict[str, float]) -> None:
        self.quote_rates = dict(quote_rates)

    def _persist_current_cycle(
        self,
        strategy: UserStrategy,
        previous_state: dict[str, Any] | None,
        next_state: dict[str, Any],
        *,
        fills: list[dict[str, Any]] | None = None,
        event: dict[str, Any] | None = None,
    ) -> None:
        current = self.workspace_store.get_strategy(strategy.id)
        if (
            current is None
            or current.owner_email != strategy.owner_email
            or not math.isclose(
                float(current.updated_at),
                float(strategy.updated_at),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise UserPaperStateConflict(
                "strategy changed or was deleted during the paper cycle"
            )
        expected_updated_at = (
            None
            if previous_state is None
            else float(previous_state.get("updated_at") or 0.0)
        )
        self.paper_store.persist_cycle(
            strategy,
            next_state,
            fills=fills,
            event=event,
            expected_state_updated_at=expected_updated_at,
        )

    async def _fetch_book(
        self,
        account: UserExchangeAccount,
        semaphore: asyncio.Semaphore,
    ) -> OrderBookSnapshot:
        cfg = _paper_exchange_config(account, fee_bps=0.0)
        async with semaphore:
            book = await asyncio.wait_for(
                self.manager.fetch_order_book(
                    cfg,
                    account.symbol,
                    self.order_book_depth,
                ),
                timeout=self.fetch_timeout_seconds,
            )
        if book is None:
            raise ValueError("order book is unavailable")
        return book

    async def pause_all(self, *, now: float | None = None) -> dict[str, int]:
        paused_at = time.time() if now is None else float(now)
        processed = 0
        conflicts = 0
        strategies = self.workspace_store.list_strategies(
            owner_email="paper@localhost",
            is_admin=True,
        )
        for strategy in strategies:
            if not strategy.enabled:
                continue
            state = self.paper_store.get_state(strategy.id)
            if state is not None and (
                state.get("terminal")
                or (
                    state.get("status") == "program_paused"
                    and not state.get("open_orders")
                )
            ):
                continue
            project = self.workspace_store.get_project(strategy.project_id)
            if project is None:
                continue
            paused, event = _nonrunning_state(
                strategy,
                project,
                state,
                status="program_paused",
                reason="global program switch is paused",
                common_quote_currency=self.common_quote_currency,
                now=paused_at,
            )
            paused["next_due_at"] = paused_at
            try:
                self._persist_current_cycle(
                    strategy,
                    state,
                    paused,
                    event=event,
                )
            except UserPaperStateConflict:
                conflicts += 1
            else:
                processed += 1
        return {"processed": processed, "conflicts": conflicts}

    async def run_once(self, *, now: float | None = None) -> dict[str, int]:
        cycle_at = time.time() if now is None else float(now)
        if self._run_lock.locked():
            return {
                "processed": 0,
                "fetched": 0,
                "errors": 0,
                "conflicts": 0,
                "skipped_locked": 1,
            }
        async with self._run_lock:
            strategies = self.workspace_store.list_strategies(
                owner_email="paper@localhost",
                is_admin=True,
            )
            enabled_by_owner: dict[str, list[UserStrategy]] = {}
            for strategy in strategies:
                if strategy.enabled:
                    enabled_by_owner.setdefault(strategy.owner_email, []).append(
                        strategy
                    )
            owner_risk_blockers: dict[str, str] = {}
            for owner, owner_strategies in enabled_by_owner.items():
                profile = self.workspace_store.risk_profile(owner)
                if not profile.trading_enabled:
                    owner_risk_blockers[owner] = "user risk trading switch is disabled"
                    continue
                if (
                    profile.max_active_strategies > 0
                    and len(owner_strategies) > profile.max_active_strategies
                ):
                    owner_risk_blockers[owner] = (
                        "user max active strategies limit is exceeded"
                    )
                    continue
                exposure = sum(
                    float(item.risk.get("max_total_quote") or 0.0)
                    for item in owner_strategies
                )
                if (
                    profile.max_total_exposure_quote > 0
                    and exposure > profile.max_total_exposure_quote
                ):
                    owner_risk_blockers[owner] = (
                        "user max total exposure quote is exceeded"
                    )
                    continue
                open_order_limit = sum(
                    int(item.risk.get("max_open_orders") or 0)
                    for item in owner_strategies
                )
                if (
                    profile.max_open_orders > 0
                    and open_order_limit > profile.max_open_orders
                ):
                    owner_risk_blockers[owner] = (
                        "user max open orders limit is exceeded"
                    )
                    continue
                if profile.max_daily_loss_quote > 0:
                    paper = self.paper_store.public_payload(
                        owner_email=owner,
                        is_admin=False,
                    )
                    daily_pnl = float(
                        (paper.get("summary") or {}).get("daily_pnl_common") or 0.0
                    )
                    if daily_pnl <= -profile.max_daily_loss_quote:
                        owner_risk_blockers[owner] = "user max daily loss is reached"
            jobs: list[
                tuple[
                    UserStrategy,
                    UserProject,
                    list[UserExchangeAccount],
                    dict[str, Any] | None,
                ]
            ] = []
            processed = 0
            errors = 0
            conflicts = 0
            for strategy in strategies:
                state = self.paper_store.get_state(strategy.id)
                project = self.workspace_store.get_project(strategy.project_id)
                if project is None:
                    continue
                if not strategy.enabled:
                    if state is not None and state.get("terminal"):
                        continue
                    if state is not None and state.get("status") != "paused":
                        paused, event = _nonrunning_state(
                            strategy,
                            project,
                            state,
                            status="paused",
                            reason="paper strategy is paused",
                            common_quote_currency=self.common_quote_currency,
                            now=cycle_at,
                        )
                        try:
                            self._persist_current_cycle(
                                strategy,
                                state,
                                paused,
                                event=event,
                            )
                        except UserPaperStateConflict:
                            conflicts += 1
                        else:
                            processed += 1
                    continue
                user_risk_reason = owner_risk_blockers.get(strategy.owner_email)
                if user_risk_reason:
                    if state is None or state.get("status") != "blocked_user_risk":
                        blocked, event = _nonrunning_state(
                            strategy,
                            project,
                            state,
                            status="blocked_user_risk",
                            reason=user_risk_reason,
                            common_quote_currency=self.common_quote_currency,
                            now=cycle_at,
                        )
                        try:
                            self._persist_current_cycle(
                                strategy,
                                state,
                                blocked,
                                event=event,
                            )
                        except UserPaperStateConflict:
                            conflicts += 1
                        else:
                            processed += 1
                    continue
                next_due = float((state or {}).get("next_due_at") or 0.0)
                configuration_changed = bool(
                    state is not None
                    and float(state.get("strategy_updated_at") or 0.0)
                    != float(strategy.updated_at)
                )
                if next_due > cycle_at and not configuration_changed:
                    continue
                accounts = [
                    account
                    for account_id in strategy.account_ids
                    for account in [self.workspace_store.get_account(account_id)]
                    if account is not None
                ]
                if (
                    state is not None
                    and state.get("terminal")
                    and state.get("config_fingerprint")
                    == strategy_paper_fingerprint(strategy, project, accounts)
                ):
                    continue
                readiness = self.workspace_store.strategy_readiness(strategy)
                if not readiness["ready"]:
                    blocked, event = _nonrunning_state(
                        strategy,
                        project,
                        state,
                        status="blocked_configuration",
                        reason="; ".join(readiness["blockers"][:3]),
                        common_quote_currency=self.common_quote_currency,
                        now=cycle_at,
                    )
                    try:
                        self._persist_current_cycle(
                            strategy,
                            state,
                            blocked,
                            event=event,
                        )
                    except UserPaperStateConflict:
                        conflicts += 1
                    else:
                        processed += 1
                    continue
                jobs.append((strategy, project, accounts, state))

            market_accounts: dict[
                tuple[str, str, str, str],
                UserExchangeAccount,
            ] = {}
            account_market_keys: dict[str, tuple[str, str, str, str]] = {}
            for account in (
                account for _, _, accounts, _ in jobs for account in accounts
            ):
                market_key = (
                    account.exchange,
                    account.market_type,
                    account.api_variant,
                    account.symbol,
                )
                account_market_keys[account.id] = market_key
                market_accounts.setdefault(market_key, account)
            semaphore = asyncio.Semaphore(PAPER_MAX_FETCH_CONCURRENCY)
            market_keys = list(market_accounts)
            results = await asyncio.gather(
                *[
                    self._fetch_book(market_accounts[market_key], semaphore)
                    for market_key in market_keys
                ],
                return_exceptions=True,
            )
            fetched_by_market: dict[
                tuple[str, str, str, str],
                OrderBookSnapshot,
            ] = {}
            errors_by_market: dict[tuple[str, str, str, str], str] = {}
            for market_key, result in zip(market_keys, results):
                if isinstance(result, Exception):
                    errors_by_market[market_key] = (
                        f"{result.__class__.__name__}: {result}"
                    )[:240]
                else:
                    fetched_by_market[market_key] = result
            fetched: dict[str, OrderBookSnapshot] = {}
            fetch_errors: dict[str, str] = {}
            for account_id, market_key in account_market_keys.items():
                if market_key in errors_by_market:
                    fetch_errors[account_id] = errors_by_market[market_key]
                else:
                    fetched[account_id] = fetched_by_market[market_key]

            for strategy, project, accounts, state in jobs:
                account_error = next(
                    (
                        f"{account.label}: {fetch_errors[account.id]}"
                        for account in accounts
                        if account.id in fetch_errors
                    ),
                    "",
                )
                if account_error:
                    blocked, event = _nonrunning_state(
                        strategy,
                        project,
                        state,
                        status="blocked_market_data",
                        reason=account_error,
                        common_quote_currency=self.common_quote_currency,
                        now=cycle_at,
                    )
                    errors += 1
                    try:
                        self._persist_current_cycle(
                            strategy,
                            state,
                            blocked,
                            event=event,
                        )
                    except UserPaperStateConflict:
                        conflicts += 1
                    else:
                        processed += 1
                    continue
                try:
                    next_state, fills, event = simulate_user_paper_cycle(
                        strategy,
                        project,
                        accounts,
                        {account.id: fetched[account.id] for account in accounts},
                        state,
                        quote_rates=self.quote_rates,
                        common_quote_currency=self.common_quote_currency,
                        now=cycle_at,
                    )
                    self._persist_current_cycle(
                        strategy,
                        state,
                        next_state,
                        fills=fills,
                        event=event,
                    )
                except UserPaperStateConflict:
                    conflicts += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    reason = f"{exc.__class__.__name__}: {exc}"[:240]
                    blocked, event = _nonrunning_state(
                        strategy,
                        project,
                        state,
                        status="error",
                        reason=reason,
                        common_quote_currency=self.common_quote_currency,
                        now=cycle_at,
                    )
                    try:
                        self._persist_current_cycle(
                            strategy,
                            state,
                            blocked,
                            event=event,
                        )
                    except UserPaperStateConflict:
                        conflicts += 1
                        continue
                    LOGGER.warning(
                        "user paper cycle failed strategy=%s error=%s",
                        strategy.id,
                        reason,
                    )
                processed += 1
            return {
                "processed": processed,
                "fetched": len(fetched_by_market),
                "errors": errors,
                "conflicts": conflicts,
                "skipped_locked": 0,
            }


async def user_paper_trading_task_loop(
    service: UserPaperTradingService,
    *,
    scan_seconds: float = PAPER_SCAN_SECONDS,
    running_check: Callable[[], Awaitable[bool]] | None = None,
    quote_rates_provider: Callable[[], Awaitable[dict[str, float]]] | None = None,
) -> None:
    await asyncio.sleep(max(0.1, float(scan_seconds)))
    while True:
        started = time.monotonic()
        try:
            running = running_check is None or await running_check()
            if running:
                if quote_rates_provider is not None:
                    service.update_quote_rates(await quote_rates_provider())
                await service.run_once()
            else:
                await service.pause_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("user paper trading loop failed: %s", exc)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(0.05, float(scan_seconds) - elapsed))
