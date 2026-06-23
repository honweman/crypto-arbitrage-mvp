from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .config import BotConfig, OptionComboConfig
from .execution_protection import build_multileg_execution_protection
from .models import Opportunity, OrderBookSnapshot
from .strategies.options_arbitrage import find_options_arbitrage_opportunities


def _best_bid_ask(book: OrderBookSnapshot | None) -> tuple[float, float] | None:
    if book is None or not book.bids or not book.asks:
        return None
    bid = float(book.bids[0].price)
    ask = float(book.asks[0].price)
    if bid <= 0 or ask <= 0:
        return None
    return bid, ask


def _mid(book: OrderBookSnapshot | None) -> float | None:
    best = _best_bid_ask(book)
    if best is None:
        return None
    return (best[0] + best[1]) / 2.0


def _days_to_expiry(expiry: str, *, now: float) -> float | None:
    if not expiry:
        return None
    value = expiry.strip()
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            expires_at = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (expires_at.timestamp() - now) / 86_400


def _depth_quote(levels: list[Any]) -> float:
    return sum(
        max(0.0, float(level.price)) * max(0.0, float(level.amount))
        for level in levels
    )


def _liquidity_metrics(book: OrderBookSnapshot | None) -> dict[str, Any]:
    best = _best_bid_ask(book)
    if book is None or best is None:
        return {
            "bid": None,
            "ask": None,
            "mark_price": None,
            "spread_bps": None,
            "bid_depth_quote": 0.0,
            "ask_depth_quote": 0.0,
            "min_depth_quote": 0.0,
            "available": False,
        }
    bid, ask = best
    mark = (bid + ask) / 2.0
    bid_depth = _depth_quote(book.bids)
    ask_depth = _depth_quote(book.asks)
    return {
        "bid": bid,
        "ask": ask,
        "mark_price": mark,
        "spread_bps": (ask - bid) / mark * 10_000.0 if mark > 0 else None,
        "bid_depth_quote": bid_depth,
        "ask_depth_quote": ask_depth,
        "min_depth_quote": min(bid_depth, ask_depth),
        "available": True,
    }


def _book_info(book: OrderBookSnapshot | None) -> dict[str, Any]:
    if book is None:
        return {}
    for name in ("metadata", "info", "ticker"):
        value = getattr(book, name, None)
        if isinstance(value, dict):
            return value
    return {}


def _info_number(info: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = info.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return None


def _chain_row(
    combo: OptionComboConfig,
    *,
    option_type: str,
    symbol: str,
    book: OrderBookSnapshot | None,
    now: float,
    cfg: BotConfig,
) -> dict[str, Any]:
    metrics = _liquidity_metrics(book)
    info = _book_info(book)
    days = _days_to_expiry(combo.expiry, now=now)
    reasons: list[str] = []
    if not metrics["available"]:
        reasons.append("order book unavailable")
    spread_bps = metrics["spread_bps"]
    if (
        cfg.options_arbitrage.max_option_spread_bps > 0
        and spread_bps is not None
        and spread_bps > cfg.options_arbitrage.max_option_spread_bps
    ):
        reasons.append(
            "spread "
            f"{spread_bps:.4g} bps > {cfg.options_arbitrage.max_option_spread_bps:.4g} bps"
        )
    min_depth = float(metrics["min_depth_quote"] or 0.0)
    if (
        cfg.options_arbitrage.min_option_depth_quote > 0
        and min_depth < cfg.options_arbitrage.min_option_depth_quote
    ):
        reasons.append(
            "depth "
            f"{min_depth:.4g} < {cfg.options_arbitrage.min_option_depth_quote:.4g}"
        )
    if (
        cfg.options_arbitrage.min_days_to_expiry_open > 0
        and days is not None
        and days < cfg.options_arbitrage.min_days_to_expiry_open
    ):
        reasons.append(
            "expiry "
            f"{days:.4g}d < {cfg.options_arbitrage.min_days_to_expiry_open:.4g}d"
        )
    status = "blocked" if reasons else "ok" if metrics["available"] else "unavailable"
    return {
        "underlying": combo.underlying,
        "exchange": combo.option_exchange,
        "symbol": symbol,
        "option_type": option_type,
        "expiry": combo.expiry,
        "days_to_expiry": days,
        "strike": combo.strike,
        "contract_size": combo.contract_size,
        "quote_currency": combo.quote_currency,
        "bid": metrics["bid"],
        "ask": metrics["ask"],
        "mark_price": (
            _info_number(info, "markPrice", "mark_price", "mark")
            or metrics["mark_price"]
        ),
        "iv": _info_number(info, "iv", "impliedVolatility", "implied_volatility"),
        "volume": _info_number(info, "volume", "baseVolume", "contractsVolume"),
        "open_interest": _info_number(info, "openInterest", "open_interest"),
        "delta": _info_number(info, "delta"),
        "gamma": _info_number(info, "gamma"),
        "vega": _info_number(info, "vega"),
        "theta": _info_number(info, "theta"),
        "spread_bps": spread_bps,
        "bid_depth_quote": metrics["bid_depth_quote"],
        "ask_depth_quote": metrics["ask_depth_quote"],
        "min_depth_quote": metrics["min_depth_quote"],
        "passes_liquidity": not reasons and metrics["available"],
        "status": status,
        "reasons": reasons,
    }


def _combo_key(combo: OptionComboConfig) -> tuple[str, str, str, float, str]:
    return (
        combo.underlying,
        combo.call_symbol,
        combo.put_symbol,
        combo.strike,
        combo.expiry,
    )


def _opportunity_combo_key(opportunity: Opportunity) -> tuple[str, str, str, float, str]:
    metadata = opportunity.metadata
    call_symbol = ""
    put_symbol = ""
    for leg in opportunity.legs:
        side_symbol = leg.symbol
        if "-C" in side_symbol or side_symbol.endswith("C"):
            call_symbol = side_symbol
        elif "-P" in side_symbol or side_symbol.endswith("P"):
            put_symbol = side_symbol
    return (
        str(metadata.get("underlying") or ""),
        call_symbol,
        put_symbol,
        float(metadata.get("strike") or 0.0),
        str(metadata.get("expiry") or ""),
    )


def _paper_leg_type(symbol: str) -> str:
    return "spot" if "/" in str(symbol or "") else "option"


def _paper_state(
    opportunity: Opportunity | None,
    reason: str,
    *,
    cfg: BotConfig,
    block_reasons: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    block_reasons = [item for item in block_reasons or [] if item]
    warnings = [item for item in warnings or [] if item]
    if opportunity is None:
        payload: dict[str, Any] = {
            "mode": "paper",
            "state": (
                "blocked"
                if block_reasons
                else "waiting"
                if reason == "edge below thresholds"
                else "blocked"
            ),
            "live_enabled": False,
            "suggested_legs": [],
            "reason": reason,
        }
        if block_reasons or warnings:
            payload["protection"] = {
                "status": "blocked" if block_reasons else "warning",
                "strategy": "options_arbitrage",
                "paper_mode": True,
                "live_submit_allowed": False,
                "would_submit_if_live": False,
                "leg_count": 0,
                "has_buy_and_sell": False,
                "requires_manual_review": bool(warnings),
                "max_slippage_bps": None,
                "plan_age_seconds": None,
                "reasons": block_reasons,
                "warnings": warnings,
                "playbooks": [
                    {
                        "event": "option_preflight_block",
                        "action": "do_not_generate_live_orders",
                        "next": "refresh_chain_or_adjust_risk_limits",
                        "auto_submit_live_orders": False,
                    }
                ],
                "paper_failure_scenarios": [],
                "evaluated_at": time.time(),
            }
        return payload
    underlying = str(opportunity.metadata.get("underlying") or "")
    legs = [
        {
            "exchange": leg.exchange,
            "symbol": leg.symbol,
            "side": leg.side,
            "type": _paper_leg_type(leg.symbol),
            "quantity_base": leg.quantity_base,
            "average_price": leg.average_price,
            "notional_quote": abs(float(leg.net_quote or leg.gross_quote or 0.0)),
            "hedge_asset": underlying or _symbol_base_from_leg(leg.symbol),
            "hedge_base_equivalent": (
                float(opportunity.metadata.get("contract_size") or 1.0)
                * leg.quantity_base
                if _paper_leg_type(leg.symbol) == "option"
                else leg.quantity_base
            ),
        }
        for leg in opportunity.legs
    ]
    protection = build_multileg_execution_protection(
        strategy="options_arbitrage",
        legs=legs,
        risk=cfg.risk,
        observed_at=opportunity.observed_at,
        now=opportunity.observed_at,
    )
    if block_reasons:
        protection["reasons"] = [
            *list(protection.get("reasons", []) or []),
            *block_reasons,
        ]
        protection["status"] = "blocked"
        protection["would_submit_if_live"] = False
    if warnings:
        protection["warnings"] = [
            *list(protection.get("warnings", []) or []),
            *warnings,
        ]
        if protection.get("status") == "ok":
            protection["status"] = "warning"
    return {
        "mode": "paper",
        "state": "would_open",
        "live_enabled": False,
        "suggested_legs": legs,
        "order_ticket": {
            "mode": "paper",
            "auto_submit_live_orders": False,
            "requires_final_confirmation": True,
            "order_count": len(legs),
            "orders": [
                {
                    "exchange": leg["exchange"],
                    "symbol": leg["symbol"],
                    "side": leg["side"],
                    "type": leg["type"],
                    "quantity_base": leg["quantity_base"],
                    "estimated_price": leg.get("average_price"),
                    "notional_quote": leg.get("notional_quote"),
                }
                for leg in legs
            ],
        },
        "protection": protection,
        "reason": "entry conditions met",
    }


def _symbol_base_from_leg(symbol: str) -> str:
    if "/" in symbol:
        return symbol.split("/", 1)[0].split(":", 1)[0].upper()
    if "-" in symbol:
        return symbol.split("-", 1)[0].upper()
    return symbol.upper()


def _option_chain_payload(
    cfg: BotConfig,
    option_books: dict[tuple[str, str], OrderBookSnapshot],
    *,
    now: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for combo in cfg.option_combos:
        rows.append(
            _chain_row(
                combo,
                option_type="call",
                symbol=combo.call_symbol,
                book=option_books.get((combo.option_exchange, combo.call_symbol)),
                now=now,
                cfg=cfg,
            )
        )
        rows.append(
            _chain_row(
                combo,
                option_type="put",
                symbol=combo.put_symbol,
                book=option_books.get((combo.option_exchange, combo.put_symbol)),
                now=now,
                cfg=cfg,
            )
        )
    return rows


def _option_chain_by_symbol(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(row.get("exchange") or ""), str(row.get("symbol") or "")): row
        for row in rows
    }


def _row_preflight_reasons(
    combo: OptionComboConfig,
    chain_by_symbol: dict[tuple[str, str], dict[str, Any]],
    *,
    cfg: BotConfig,
    now: float,
) -> tuple[list[str], list[str]]:
    block_reasons: list[str] = []
    warnings: list[str] = []
    call = chain_by_symbol.get((combo.option_exchange, combo.call_symbol), {})
    put = chain_by_symbol.get((combo.option_exchange, combo.put_symbol), {})
    for label, row in (("call", call), ("put", put)):
        if row.get("status") == "blocked":
            messages = row.get("reasons") or ["option liquidity check failed"]
            block_reasons.append(f"{label}: {messages[0]}")
        elif row.get("status") == "unavailable":
            block_reasons.append(f"{label}: order book unavailable")
    days = _days_to_expiry(combo.expiry, now=now)
    if (
        cfg.options_arbitrage.expiry_reminder_days > 0
        and days is not None
        and days < cfg.options_arbitrage.expiry_reminder_days
    ):
        warnings.append(
            f"expiry reminder: {days:.4g}d < {cfg.options_arbitrage.expiry_reminder_days:.4g}d"
        )
    return block_reasons, warnings


def _strategy_candidate(
    *,
    strategy_type: str,
    label: str,
    edge_quote: float,
    capital_quote: float,
    legs: list[dict[str, Any]],
    metadata: dict[str, Any],
    cfg: BotConfig,
) -> dict[str, Any] | None:
    edge_bps = edge_quote / capital_quote * 10_000.0 if capital_quote > 0 else 0.0
    if (
        edge_quote < cfg.options_arbitrage.min_edge_quote
        or edge_bps < cfg.options_arbitrage.min_edge_bps
    ):
        return None
    return {
        "strategy_type": strategy_type,
        "label": label,
        "edge_quote": edge_quote,
        "edge_bps": edge_bps,
        "capital_quote": capital_quote,
        "legs": legs,
        "metadata": metadata,
        "mode": "paper",
        "auto_submit_live_orders": False,
        "requires_final_confirmation": True,
    }


def _enhanced_strategy_candidates(
    cfg: BotConfig,
    chain_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str, str], dict[float, dict[str, dict[str, Any]]]] = (
        defaultdict(lambda: defaultdict(dict))
    )
    for row in chain_rows:
        if row.get("status") != "ok":
            continue
        key = (
            str(row.get("underlying") or ""),
            str(row.get("exchange") or ""),
            str(row.get("expiry") or ""),
            str(row.get("quote_currency") or ""),
        )
        by_group[key][float(row.get("strike") or 0.0)][str(row.get("option_type"))] = row

    candidates: list[dict[str, Any]] = []
    for (underlying, exchange, expiry, quote_currency), strikes in by_group.items():
        sorted_strikes = sorted(strikes)
        for lower, upper in zip(sorted_strikes, sorted_strikes[1:]):
            lower_pair = strikes[lower]
            upper_pair = strikes[upper]
            if not all(
                item in lower_pair and item in upper_pair for item in ("call", "put")
            ):
                continue
            width = upper - lower
            lower_call = lower_pair["call"]
            lower_put = lower_pair["put"]
            upper_call = upper_pair["call"]
            upper_put = upper_pair["put"]
            long_box_debit = (
                float(lower_call["ask"])
                - float(upper_call["bid"])
                + float(upper_put["ask"])
                - float(lower_put["bid"])
            )
            long_box_edge = width - long_box_debit
            candidate = _strategy_candidate(
                strategy_type="box_spread",
                label=f"{underlying} {expiry} box {lower:g}-{upper:g}",
                edge_quote=long_box_edge,
                capital_quote=max(long_box_debit, width, 1e-12),
                legs=[
                    {"side": "buy", "symbol": lower_call["symbol"], "exchange": exchange},
                    {"side": "sell", "symbol": upper_call["symbol"], "exchange": exchange},
                    {"side": "buy", "symbol": upper_put["symbol"], "exchange": exchange},
                    {"side": "sell", "symbol": lower_put["symbol"], "exchange": exchange},
                ],
                metadata={
                    "underlying": underlying,
                    "expiry": expiry,
                    "lower_strike": lower,
                    "upper_strike": upper,
                    "box_width": width,
                    "box_debit": long_box_debit,
                    "quote_currency": quote_currency,
                },
                cfg=cfg,
            )
            if candidate:
                candidates.append(candidate)
            reverse_box_credit = (
                float(lower_call["bid"])
                - float(upper_call["ask"])
                + float(upper_put["bid"])
                - float(lower_put["ask"])
            )
            reverse_candidate = _strategy_candidate(
                strategy_type="reverse_box",
                label=f"{underlying} {expiry} reverse box {lower:g}-{upper:g}",
                edge_quote=reverse_box_credit - width,
                capital_quote=max(width, 1e-12),
                legs=[
                    {"side": "sell", "symbol": lower_call["symbol"], "exchange": exchange},
                    {"side": "buy", "symbol": upper_call["symbol"], "exchange": exchange},
                    {"side": "sell", "symbol": upper_put["symbol"], "exchange": exchange},
                    {"side": "buy", "symbol": lower_put["symbol"], "exchange": exchange},
                ],
                metadata={
                    "underlying": underlying,
                    "expiry": expiry,
                    "lower_strike": lower,
                    "upper_strike": upper,
                    "box_width": width,
                    "box_credit": reverse_box_credit,
                    "quote_currency": quote_currency,
                },
                cfg=cfg,
            )
            if reverse_candidate:
                candidates.append(reverse_candidate)
            vertical_debit = float(lower_call["ask"]) - float(upper_call["bid"])
            vertical_candidate = _strategy_candidate(
                strategy_type="vertical_spread",
                label=f"{underlying} {expiry} bull call {lower:g}-{upper:g}",
                edge_quote=width - vertical_debit,
                capital_quote=max(vertical_debit, 1e-12),
                legs=[
                    {"side": "buy", "symbol": lower_call["symbol"], "exchange": exchange},
                    {"side": "sell", "symbol": upper_call["symbol"], "exchange": exchange},
                ],
                metadata={
                    "underlying": underlying,
                    "expiry": expiry,
                    "lower_strike": lower,
                    "upper_strike": upper,
                    "max_loss_quote": vertical_debit,
                    "max_profit_quote": width - vertical_debit,
                    "break_even": lower + vertical_debit,
                    "relative_value_not_risk_free": True,
                },
                cfg=cfg,
            )
            if vertical_candidate:
                candidates.append(vertical_candidate)

    by_calendar: dict[tuple[str, str, str, str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in chain_rows:
        if row.get("status") == "ok":
            by_calendar[
                (
                    str(row.get("underlying") or ""),
                    str(row.get("exchange") or ""),
                    str(row.get("option_type") or ""),
                    str(row.get("quote_currency") or ""),
                    float(row.get("strike") or 0.0),
                )
            ].append(row)
    for rows in by_calendar.values():
        rows.sort(key=lambda item: float(item.get("days_to_expiry") or 0.0))
        for near, far in zip(rows, rows[1:]):
            near_mark = near.get("mark_price")
            far_mark = far.get("mark_price")
            if near_mark is None or far_mark is None:
                continue
            edge = float(far_mark) - float(near_mark)
            candidate = _strategy_candidate(
                strategy_type="calendar_spread",
                label=(
                    f"{near.get('underlying')} {near.get('option_type')} "
                    f"K={float(near.get('strike') or 0.0):g} "
                    f"{near.get('expiry')}->{far.get('expiry')}"
                ),
                edge_quote=edge,
                capital_quote=max(float(far_mark), 1e-12),
                legs=[
                    {"side": "buy", "symbol": far["symbol"], "exchange": far["exchange"]},
                    {"side": "sell", "symbol": near["symbol"], "exchange": near["exchange"]},
                ],
                metadata={
                    "near_expiry": near.get("expiry"),
                    "far_expiry": far.get("expiry"),
                    "strike": near.get("strike"),
                    "relative_value_not_risk_free": True,
                },
                cfg=cfg,
            )
            if candidate:
                candidates.append(candidate)

    iv_rows = [row for row in chain_rows if row.get("iv") is not None]
    if iv_rows:
        ivs = sorted(float(row["iv"]) for row in iv_rows)
        median_iv = ivs[len(ivs) // 2]
        for row in iv_rows:
            diff = float(row["iv"]) - median_iv
            candidate = _strategy_candidate(
                strategy_type="iv_anomaly",
                label=f"{row.get('symbol')} IV anomaly",
                edge_quote=abs(diff),
                capital_quote=max(abs(median_iv), 1e-12),
                legs=[
                    {
                        "side": "sell" if diff > 0 else "buy",
                        "symbol": row["symbol"],
                        "exchange": row["exchange"],
                    }
                ],
                metadata={"iv": row.get("iv"), "median_iv": median_iv, "iv_diff": diff},
                cfg=cfg,
            )
            if candidate:
                candidates.append(candidate)

    candidates.sort(key=lambda item: float(item.get("edge_bps") or 0.0), reverse=True)
    return candidates[:20]


def _options_risk_payload(
    cfg: BotConfig,
    rows: list[dict[str, Any]],
    chain_rows: list[dict[str, Any]],
    enhanced_candidates: list[dict[str, Any]],
    *,
    now: float,
) -> dict[str, Any]:
    greek_keys = ("delta", "gamma", "vega", "theta")
    greek_rows = [row for row in chain_rows if any(row.get(key) is not None for key in greek_keys)]
    greek_totals = {
        key: (
            sum(float(row.get(key) or 0.0) for row in greek_rows)
            if greek_rows
            else None
        )
        for key in greek_keys
    }
    expiry_map: dict[str, dict[str, Any]] = {}
    for row in chain_rows:
        expiry = str(row.get("expiry") or "")
        if not expiry:
            continue
        bucket = expiry_map.setdefault(
            expiry,
            {
                "expiry": expiry,
                "option_count": 0,
                "blocked_count": 0,
                "min_days_to_expiry": row.get("days_to_expiry"),
            },
        )
        bucket["option_count"] += 1
        if row.get("status") == "blocked":
            bucket["blocked_count"] += 1
        days = row.get("days_to_expiry")
        if days is not None:
            current = bucket.get("min_days_to_expiry")
            bucket["min_days_to_expiry"] = (
                days if current is None else min(float(current), float(days))
            )
    expiry_concentration = sorted(
        expiry_map.values(),
        key=lambda item: float(item.get("min_days_to_expiry") or 1e18),
    )
    expiry_reminders = [
        item
        for item in expiry_concentration
        if cfg.options_arbitrage.expiry_reminder_days > 0
        and item.get("min_days_to_expiry") is not None
        and float(item["min_days_to_expiry"]) < cfg.options_arbitrage.expiry_reminder_days
    ]
    payoff_candidates: list[dict[str, Any]] = []
    for row in rows:
        opportunity = row.get("opportunity") or {}
        if isinstance(opportunity, dict) and opportunity:
            payoff_candidates.append(
                {
                    "label": f"{row.get('underlying')} K={row.get('strike')} {row.get('expiry')}",
                    "strategy_type": opportunity.get("metadata", {}).get("direction"),
                    "max_profit_quote": opportunity.get("profit_quote"),
                    "max_loss_quote": 0.0,
                    "break_even": row.get("synthetic_forward_mid"),
                }
            )
    for candidate in enhanced_candidates:
        metadata = candidate.get("metadata") or {}
        payoff_candidates.append(
            {
                "label": candidate.get("label"),
                "strategy_type": candidate.get("strategy_type"),
                "max_profit_quote": metadata.get("max_profit_quote", candidate.get("edge_quote")),
                "max_loss_quote": metadata.get("max_loss_quote", 0.0),
                "break_even": metadata.get("break_even"),
            }
        )
    blocked_new_open_count = sum(1 for row in rows if row.get("status") == "blocked")
    if not cfg.options_arbitrage.enabled:
        status = "disabled"
    elif blocked_new_open_count:
        status = "blocked"
    elif expiry_reminders:
        status = "warning"
    else:
        status = "ok" if chain_rows else "disabled"
    return {
        "status": status,
        "total_delta": greek_totals["delta"],
        "total_gamma": greek_totals["gamma"],
        "total_vega": greek_totals["vega"],
        "total_theta": greek_totals["theta"],
        "greeks_available_count": len(greek_rows),
        "chain_option_count": len(chain_rows),
        "expiry_concentration": expiry_concentration,
        "expiry_reminders": expiry_reminders,
        "blocked_new_open_count": blocked_new_open_count,
        "max_loss_quote": (
            max((float(item.get("max_loss_quote") or 0.0) for item in payoff_candidates), default=None)
            if payoff_candidates
            else None
        ),
        "max_profit_quote": (
            max((float(item.get("max_profit_quote") or 0.0) for item in payoff_candidates), default=None)
            if payoff_candidates
            else None
        ),
        "break_even_points": [
            item
            for item in payoff_candidates
            if item.get("break_even") is not None
        ][:12],
        "controls": {
            "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
            "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
            "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
            "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
            "paper_mode_only": True,
            "auto_submit_live_orders": False,
        },
        "updated_at": now,
    }


def options_arbitrage_payload(
    cfg: BotConfig,
    *,
    spot_books: dict[tuple[str, str], OrderBookSnapshot],
    option_books: dict[tuple[str, str], OrderBookSnapshot],
    now: float | None = None,
) -> dict[str, Any]:
    observed_at = time.time() if now is None else now
    chain_rows = _option_chain_payload(cfg, option_books, now=observed_at)
    chain_by_symbol = _option_chain_by_symbol(chain_rows)
    if not cfg.option_combos:
        risk_payload = _options_risk_payload(
            cfg,
            [],
            [],
            [],
            now=observed_at,
        )
        return {
            "status": "disabled",
            "mode": "paper",
            "rows": [],
            "option_chain": [],
            "strategy_candidates": [],
            "risk": risk_payload,
            "execution_controls": risk_payload["controls"],
            "opportunities": [],
            "candidate_count": 0,
            "parity_candidate_count": 0,
            "enhanced_candidate_count": 0,
            "configured_count": 0,
            "checked_count": 0,
            "last_finished": observed_at,
            "errors": [],
            "warnings": [],
        }

    opportunities = (
        find_options_arbitrage_opportunities(
            spot_books=spot_books,
            option_books=option_books,
            spot_exchanges=cfg.spot_exchanges,
            option_exchanges=cfg.derivative_exchanges,
            combos=cfg.option_combos,
            cfg=cfg.options_arbitrage,
        )
        if cfg.options_arbitrage.enabled
        else []
    )
    enhanced_candidates = (
        _enhanced_strategy_candidates(cfg, chain_rows)
        if cfg.options_arbitrage.enabled
        else []
    )
    best_by_combo: dict[tuple[str, str, str, float, str], Opportunity] = {}
    for opportunity in opportunities:
        key = _opportunity_combo_key(opportunity)
        current = best_by_combo.get(key)
        if current is None or opportunity.profit_bps > current.profit_bps:
            best_by_combo[key] = opportunity

    rows: list[dict[str, Any]] = []
    for combo in cfg.option_combos:
        spot_book = spot_books.get((combo.spot_exchange, combo.spot_symbol))
        call_book = option_books.get((combo.option_exchange, combo.call_symbol))
        put_book = option_books.get((combo.option_exchange, combo.put_symbol))
        spot_mid = _mid(spot_book)
        call_mid = _mid(call_book)
        put_mid = _mid(put_book)
        synthetic_forward_mid = (
            call_mid - put_mid + combo.strike
            if call_mid is not None and put_mid is not None
            else None
        )
        parity_gap_bps = (
            (synthetic_forward_mid - spot_mid) / spot_mid * 10_000.0
            if synthetic_forward_mid is not None and spot_mid
            else None
        )
        reasons: list[str] = []
        if not cfg.options_arbitrage.enabled:
            reasons.append("options arbitrage disabled")
        else:
            if spot_mid is None:
                reasons.append("spot order book unavailable")
            if call_mid is None:
                reasons.append("call order book unavailable")
            if put_mid is None:
                reasons.append("put order book unavailable")
        block_reasons, protection_warnings = _row_preflight_reasons(
            combo,
            chain_by_symbol,
            cfg=cfg,
            now=observed_at,
        )
        opportunity = best_by_combo.get(_combo_key(combo))
        if block_reasons and opportunity is not None:
            opportunity = None
        if opportunity is None and not reasons and cfg.options_arbitrage.enabled:
            reasons.append(block_reasons[0] if block_reasons else "edge below thresholds")
        if opportunity is not None:
            status = "candidate"
            reason = "entry conditions met"
        elif reasons == ["options arbitrage disabled"]:
            status = "disabled"
            reason = reasons[0]
        elif block_reasons:
            status = "blocked"
            reason = block_reasons[0]
        else:
            status = "watching"
            reason = reasons[0] if reasons else "waiting for entry conditions"
        rows.append(
            {
                "underlying": combo.underlying,
                "spot_exchange": combo.spot_exchange,
                "spot_symbol": combo.spot_symbol,
                "option_exchange": combo.option_exchange,
                "call_symbol": combo.call_symbol,
                "put_symbol": combo.put_symbol,
                "strike": combo.strike,
                "expiry": combo.expiry,
                "contract_size": combo.contract_size,
                "quote_currency": combo.quote_currency,
                "spot_mid": spot_mid,
                "call_mid": call_mid,
                "put_mid": put_mid,
                "synthetic_forward_mid": synthetic_forward_mid,
                "parity_gap_bps": parity_gap_bps,
                "days_to_expiry": _days_to_expiry(combo.expiry, now=observed_at),
                "status": status,
                "reason": reason,
                "reasons": reasons,
                "preflight_reasons": block_reasons,
                "warnings": protection_warnings,
                "opportunity": opportunity.to_dict() if opportunity else None,
                "paper_execution": _paper_state(
                    opportunity,
                    reason,
                    cfg=cfg,
                    block_reasons=block_reasons,
                    warnings=protection_warnings,
                ),
                "observed_at": observed_at,
            }
        )

    status = "disabled"
    if cfg.options_arbitrage.enabled:
        if any(row.get("status") == "blocked" for row in rows):
            status = "blocked"
        elif opportunities or enhanced_candidates:
            status = "candidate"
        else:
            status = "watching"
    risk_payload = _options_risk_payload(
        cfg,
        rows,
        chain_rows,
        enhanced_candidates,
        now=observed_at,
    )
    return {
        "status": status,
        "mode": "paper",
        "rows": rows,
        "option_chain": chain_rows,
        "strategy_candidates": enhanced_candidates,
        "risk": risk_payload,
        "execution_controls": risk_payload["controls"],
        "opportunities": [item.to_dict() for item in opportunities[:10]],
        "candidate_count": len(opportunities) + len(enhanced_candidates),
        "parity_candidate_count": len(opportunities),
        "enhanced_candidate_count": len(enhanced_candidates),
        "configured_count": len(cfg.option_combos),
        "checked_count": sum(
            1
            for row in rows
            if row.get("spot_mid") is not None
            and row.get("call_mid") is not None
            and row.get("put_mid") is not None
        ),
        "thresholds": {
            "notional_quote": cfg.options_arbitrage.notional_quote,
            "min_edge_quote": cfg.options_arbitrage.min_edge_quote,
            "min_edge_bps": cfg.options_arbitrage.min_edge_bps,
            "max_contracts": cfg.options_arbitrage.max_contracts,
            "max_days_to_expiry": cfg.options_arbitrage.max_days_to_expiry,
            "min_option_depth_quote": cfg.options_arbitrage.min_option_depth_quote,
            "max_option_spread_bps": cfg.options_arbitrage.max_option_spread_bps,
            "min_days_to_expiry_open": cfg.options_arbitrage.min_days_to_expiry_open,
            "expiry_reminder_days": cfg.options_arbitrage.expiry_reminder_days,
        },
        "last_finished": observed_at,
        "errors": [],
        "warnings": [
            "paper mode only; live option exercise, assignment, and margin controls are not enabled"
        ]
        if cfg.options_arbitrage.enabled
        else [],
    }
