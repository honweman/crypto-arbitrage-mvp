from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from typing import Any, Iterable

from .config import BotConfig, ExchangeConfig
from .models import OrderBookSnapshot
from .risk import RiskOrder, current_daily_pnl_quote, evaluate_order_batch


def _number(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("order") or raw.get("orderId") or "")


def _filled_base(raw: dict[str, Any]) -> float:
    for key in ("filled", "filled_amount", "executedQty", "executed_amount"):
        if key in raw:
            return _number(raw.get(key))
    return 0.0


def _base_currency(symbol: str) -> str:
    return symbol.split("/", 1)[0].upper()


def _quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0].upper()


def _candidate_source_order(
    orders: Iterable[Any],
    fill_status: dict[str, Any],
    *,
    hedge_side: str,
) -> Any | None:
    source_side = "buy" if hedge_side == "sell" else "sell"
    filled_by_key = {
        (
            str(row.get("exchange") or ""),
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
        ): _number(row.get("filled_base"))
        for row in fill_status.get("orders", [])
        if isinstance(row, dict)
    }
    candidates = [order for order in orders if order.leg.side == source_side]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda order: filled_by_key.get(
            (order.exchange.key, order.leg.symbol, order.leg.side),
            0.0,
        ),
    )


async def _current_book(
    manager: Any,
    exchange: ExchangeConfig,
    symbol: str,
    fallback: OrderBookSnapshot | None,
    *,
    depth: int,
) -> OrderBookSnapshot | None:
    fetcher = getattr(manager, "fetch_order_book", None)
    if callable(fetcher):
        try:
            book = await fetcher(exchange, symbol, depth)
            if isinstance(book, OrderBookSnapshot):
                return book
        except Exception:  # noqa: BLE001
            pass
    return fallback


async def _order_fill(
    manager: Any,
    exchange: ExchangeConfig,
    *,
    symbol: str,
    order_id: str,
    submitted: dict[str, Any],
) -> tuple[float, bool]:
    filled = _filled_base(submitted)
    still_open = bool(order_id)
    try:
        open_orders = await manager.fetch_open_orders(exchange, symbol=symbol)
    except Exception:  # noqa: BLE001
        return filled, still_open
    still_open = False
    for row in open_orders or []:
        if not isinstance(row, dict) or _order_id(row) != order_id:
            continue
        still_open = True
        filled = max(filled, _filled_base(row))
    fetch_closed = getattr(manager, "fetch_closed_orders", None)
    if callable(fetch_closed):
        try:
            closed_orders = await fetch_closed(exchange, symbol=symbol, limit=50)
        except Exception:  # noqa: BLE001
            closed_orders = []
        for row in closed_orders or []:
            if isinstance(row, dict) and _order_id(row) == order_id:
                filled = max(filled, _filled_base(row))
    return filled, still_open


def _client_order_id(
    prefix: str,
    fill_status: dict[str, Any],
    *,
    attempt: int,
) -> str:
    parent_ids = sorted(
        str(row.get("order_id") or "")
        for row in fill_status.get("orders", [])
        if isinstance(row, dict) and row.get("order_id")
    )
    digest = hashlib.sha256("|".join(parent_ids).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-hedge-{digest}-{attempt}"


async def execute_emergency_hedge(
    cfg: BotConfig,
    manager: Any,
    *,
    orders: list[Any],
    fill_status: dict[str, Any],
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    strategy_id: str,
    client_order_prefix: str,
) -> dict[str, Any]:
    initial_imbalance = float(fill_status.get("imbalance_base") or 0.0)
    hedge_side = "sell" if initial_imbalance > 0 else "buy"
    remaining_base = abs(initial_imbalance)
    result: dict[str, Any] = {
        "enabled": bool(cfg.risk.auto_hedge_live_enabled),
        "status": "not_required" if remaining_base <= 1e-12 else "disabled",
        "strategy": strategy_id,
        "side": hedge_side if remaining_base > 1e-12 else "",
        "requested_base": remaining_base,
        "filled_base": 0.0,
        "remaining_base": remaining_base,
        "max_quote": cfg.risk.max_auto_hedge_quote,
        "attempts": [],
        "started_at": time.time(),
    }
    if remaining_base <= 1e-12:
        result["finished_at"] = time.time()
        return result
    if not cfg.risk.auto_hedge_live_enabled:
        result["reason"] = "risk.auto_hedge_live_enabled is false"
        result["finished_at"] = time.time()
        return result
    if cfg.risk.max_auto_hedge_quote <= 0:
        result["status"] = "blocked"
        result["reason"] = "risk.max_auto_hedge_quote must be positive"
        result["finished_at"] = time.time()
        return result

    source = _candidate_source_order(orders, fill_status, hedge_side=hedge_side)
    if source is None:
        result["status"] = "blocked"
        result["reason"] = "filled source leg could not be identified"
        result["finished_at"] = time.time()
        return result

    exchange = source.exchange
    symbol = source.leg.symbol
    quote = (source.leg.quote_currency or _quote_currency(symbol)).upper()
    quote_rate = _number(quote_rates.get(quote) or source.leg.common_quote_rate)
    if quote_rate <= 0:
        result["status"] = "blocked"
        result["reason"] = f"missing quote rate for {quote}"
        result["finished_at"] = time.time()
        return result
    result.update({"exchange": exchange.key, "symbol": symbol, "quote_currency": quote})

    attempts = max(1, int(cfg.risk.auto_hedge_max_attempts))
    total_filled = 0.0
    quote_used_common = 0.0
    for attempt in range(1, attempts + 1):
        if remaining_base <= 1e-12:
            break
        book = await _current_book(
            manager,
            exchange,
            symbol,
            books.get((exchange.key, symbol)),
            depth=max(5, cfg.order_book_depth),
        )
        levels = (
            book.bids if book and hedge_side == "sell" else book.asks if book else []
        )
        if not levels or levels[0].price <= 0:
            result["attempts"].append(
                {
                    "attempt": attempt,
                    "status": "blocked",
                    "reason": "order book unavailable",
                }
            )
            break

        top_price = float(levels[0].price)
        slip_fraction = max(0.0, cfg.risk.auto_hedge_slippage_bps) / 10_000
        raw_price = (
            top_price * (1 - slip_fraction)
            if hedge_side == "sell"
            else top_price * (1 + slip_fraction)
        )
        remaining_quote_budget = max(
            0.0,
            cfg.risk.max_auto_hedge_quote - quote_used_common,
        )
        amount = min(
            remaining_base,
            remaining_quote_budget / (raw_price * quote_rate),
        )
        attempt_row: dict[str, Any] = {
            "attempt": attempt,
            "side": hedge_side,
            "requested_base": amount,
            "reference_price": top_price,
            "limit_price": raw_price,
        }
        submission_attempted = False
        if amount <= 1e-12:
            attempt_row.update(
                {"status": "blocked", "reason": "hedge quote cap exhausted"}
            )
            result["attempts"].append(attempt_row)
            break

        risk_order = RiskOrder(
            strategy=strategy_id,
            exchange=exchange.key,
            symbol=symbol,
            side=hedge_side,
            amount=amount,
            price=raw_price,
            quote_notional=amount * raw_price * quote_rate,
            distance_bps=max(0.0, cfg.risk.auto_hedge_slippage_bps),
            slippage_bps=max(0.0, cfg.risk.auto_hedge_slippage_bps),
        )
        hedge_risk = replace(cfg.risk, require_post_only=False)
        try:
            open_count = len(await manager.fetch_open_orders(exchange, symbol=symbol))
        except Exception as exc:  # noqa: BLE001
            attempt_row.update(
                {"status": "blocked", "reason": f"open order check failed: {exc}"}
            )
            result["attempts"].append(attempt_row)
            break
        decision = evaluate_order_batch(
            hedge_risk,
            [risk_order],
            strategy=strategy_id,
            live=True,
            daily_pnl_quote=current_daily_pnl_quote(cfg),
            existing_open_order_count=open_count,
            post_only=False,
        )
        attempt_row["risk"] = decision.to_dict()
        if not decision.approved:
            attempt_row.update(
                {"status": "blocked", "reason": "; ".join(decision.reasons)}
            )
            result["attempts"].append(attempt_row)
            break

        try:
            balance = await manager.fetch_balance(exchange)
            currency = quote if hedge_side == "buy" else _base_currency(symbol)
            required = amount * raw_price if hedge_side == "buy" else amount
            free = _number((balance.get(currency) or {}).get("free"))
            if free + 1e-12 < required:
                raise ValueError(
                    f"{currency} free {free:.8f} is below hedge requirement {required:.8f}"
                )
            prepared = await manager.prepare_limit_order(
                exchange,
                symbol=symbol,
                side=hedge_side,
                amount=amount,
                price=raw_price,
            )
            if prepared.get("errors"):
                raise ValueError("; ".join(str(error) for error in prepared["errors"]))
            submission_attempted = True
            submitted = await manager.create_prepared_limit_order(
                exchange,
                symbol=symbol,
                side=hedge_side,
                prepared=prepared,
                post_only=False,
                client_order_id=_client_order_id(
                    client_order_prefix,
                    fill_status,
                    attempt=attempt,
                ),
            )
            order_id = _order_id(submitted)
            attempt_row["order_id"] = order_id
            if cfg.risk.auto_hedge_order_ttl_seconds > 0:
                await asyncio.sleep(cfg.risk.auto_hedge_order_ttl_seconds)
            filled, still_open = await _order_fill(
                manager,
                exchange,
                symbol=symbol,
                order_id=order_id,
                submitted=submitted,
            )
            if still_open and order_id:
                await manager.cancel_order(exchange, symbol=symbol, order_id=order_id)
                filled_after_cancel, _ = await _order_fill(
                    manager,
                    exchange,
                    symbol=symbol,
                    order_id=order_id,
                    submitted=submitted,
                )
                filled = max(filled, filled_after_cancel)
            filled = min(amount, filled)
            total_filled += filled
            remaining_base = max(0.0, remaining_base - filled)
            quote_used_common += filled * raw_price * quote_rate
            attempt_row.update(
                {
                    "status": "filled" if filled + 1e-12 >= amount else "partial",
                    "filled_base": filled,
                    "remaining_base": remaining_base,
                }
            )
        except Exception as exc:  # noqa: BLE001
            attempt_row.update(
                {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
            )
        result["attempts"].append(attempt_row)
        if submission_attempted and attempt_row.get("status") == "error":
            break

    result.update(
        {
            "filled_base": total_filled,
            "remaining_base": remaining_base,
            "filled_quote": quote_used_common,
            "status": (
                "completed"
                if remaining_base <= 1e-12
                else "partial"
                if total_filled > 0
                else "failed"
            ),
            "finished_at": time.time(),
        }
    )
    return result


def apply_emergency_hedge_to_fill_status(
    fill_status: dict[str, Any],
    hedge: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(fill_status)
    initial_imbalance = float(fill_status.get("imbalance_base") or 0.0)
    filled = _number(hedge.get("filled_base"))
    buy_filled = _number(fill_status.get("buy_filled_base"))
    sell_filled = _number(fill_status.get("sell_filled_base"))
    if hedge.get("side") == "sell":
        sell_filled += filled
    elif hedge.get("side") == "buy":
        buy_filled += filled
    imbalance = buy_filled - sell_filled
    required = abs(imbalance) > 1e-12
    updated.update(
        {
            "initial_imbalance_base": initial_imbalance,
            "buy_filled_base": buy_filled,
            "sell_filled_base": sell_filled,
            "imbalance_base": imbalance,
            "hedge_required": required,
            "hedge_side": "sell" if imbalance > 0 else "buy" if imbalance < 0 else "",
            "hedge_base": abs(imbalance),
            "status": "hedge_required" if required else "balanced_after_auto_hedge",
            "auto_hedge": hedge,
        }
    )
    return updated
