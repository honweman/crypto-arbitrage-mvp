from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from .config import BotConfig, ContractStrategiesConfig, RiskConfig
from .execution_protection import build_multileg_execution_protection


CONTRACT_STRATEGY_IDS = {
    "funding_bot",
    "basis_bot",
    "futures_grid",
    "hedge_rebalancer",
}


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _base_asset(symbol: str) -> str:
    text = str(symbol or "")
    if "/" in text:
        return text.split("/", 1)[0].split(":", 1)[0].upper()
    if "-" in text:
        return text.split("-", 1)[0].upper()
    return text.upper()


def _account_enabled(risk: RiskConfig, exchange: str) -> bool:
    return not exchange or risk.account_enabled.get(exchange, True)


def _strategy_enabled(risk: RiskConfig, strategy_id: str) -> bool:
    return risk.strategy_enabled.get(strategy_id, True)


def _effective_notional(cfg: BotConfig) -> float:
    notional = float(cfg.contract_strategies.notional_quote or 0.0)
    return notional if notional > 0 else float(cfg.notional_quote or 0.0)


def _risk_row(
    cfg: BotConfig,
    *,
    strategy_id: str,
    exchange: str = "",
    extra_reasons: list[str] | None = None,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    reasons = list(extra_reasons or [])
    warnings = list(extra_warnings or [])
    if not cfg.contract_strategies.enabled:
        reasons.append("contract strategies disabled")
    if not _strategy_enabled(cfg.risk, strategy_id):
        reasons.append("strategy disabled by risk")
    if exchange and not _account_enabled(cfg.risk, exchange):
        reasons.append("account disabled by risk")
    if not cfg.risk.enabled:
        warnings.append("risk engine disabled; paper plan only")
    if cfg.contract_strategies.live_enabled:
        warnings.append("live flag is configured but this module is paper-only")
    status = "blocked" if reasons else "warning" if warnings else "ok"
    return {
        "status": status,
        "reasons": reasons,
        "warnings": warnings,
        "controls": {
            "paper_mode_only": True,
            "auto_submit_live_orders": False,
            "live_submit_allowed": False,
        },
    }


def _disabled_risk() -> dict[str, Any]:
    return {
        "status": "disabled",
        "reasons": [],
        "warnings": [],
        "controls": {
            "paper_mode_only": True,
            "auto_submit_live_orders": False,
            "live_submit_allowed": False,
        },
    }


def _status_from_candidate(candidate: bool, risk: dict[str, Any], fallback: str) -> str:
    if risk.get("status") == "blocked":
        return "blocked"
    if candidate:
        return "candidate"
    return fallback


def _row(
    *,
    strategy_id: str,
    strategy: str,
    status: str,
    reason: str,
    market: dict[str, Any],
    signal: dict[str, Any],
    plan: dict[str, Any],
    risk: dict[str, Any],
    warnings: list[str] | None = None,
    observed_at: float | None = None,
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "strategy": strategy,
        "status": status,
        "reason": reason,
        "warnings": list(warnings or []),
        "market": market,
        "signal": signal,
        "plan": {
            "mode": "paper",
            "auto_submit_live_orders": False,
            **plan,
        },
        "risk": risk,
        "observed_at": observed_at,
    }


def _market_from_funding_row(row: dict[str, Any]) -> dict[str, Any]:
    spot = f"{row.get('spot_exchange') or '--'} {row.get('spot_symbol') or '--'}"
    derivative = (
        f"{row.get('derivative_exchange') or '--'} "
        f"{row.get('derivative_symbol') or '--'}"
    )
    return {
        "label": f"{spot} / {derivative}",
        "spot_exchange": row.get("spot_exchange", ""),
        "spot_symbol": row.get("spot_symbol", ""),
        "derivative_exchange": row.get("derivative_exchange", ""),
        "derivative_symbol": row.get("derivative_symbol", ""),
    }


def _funding_bot_rows(
    cfg: BotConfig,
    funding_basis: dict[str, Any],
    *,
    now: float,
) -> list[dict[str, Any]]:
    contract_cfg = cfg.contract_strategies
    if not (contract_cfg.enabled and contract_cfg.funding_bot_enabled):
        return [
            _row(
                strategy_id="funding_bot",
                strategy="Funding Bot",
                status="disabled",
                reason="funding bot disabled",
                market={"label": "--"},
                signal={"primary": "--", "detail": ""},
                plan={"summary": "No paper plan"},
                risk=_disabled_risk(),
                observed_at=now,
            )
        ]

    rows = []
    threshold = abs(float(contract_cfg.funding_min_bps or 0.0))
    for item in funding_basis.get("rows", []) or []:
        if not isinstance(item, dict):
            continue
        funding_bps = _number_or_none(item.get("funding_rate_bps"))
        row_threshold = _number_or_none(
            (item.get("thresholds") or {}).get("min_funding_bps")
        )
        effective_threshold = threshold if threshold > 0 else abs(row_threshold or 0.0)
        threshold_ok = (
            funding_bps is not None and abs(funding_bps) >= effective_threshold
        )
        paper = item.get("paper_execution") if isinstance(item.get("paper_execution"), dict) else {}
        legs = paper.get("suggested_legs") if isinstance(paper.get("suggested_legs"), list) else []
        protection = paper.get("protection") if isinstance(paper.get("protection"), dict) else {}
        risk_reasons = [
            reason
            for reason in protection.get("reasons", []) or []
            if isinstance(reason, str)
        ]
        risk_warnings = [
            warning
            for warning in [
                *list(item.get("warnings", []) or []),
                *list(protection.get("warnings", []) or []),
            ]
            if isinstance(warning, str)
        ]
        risk = _risk_row(
            cfg,
            strategy_id="funding_bot",
            exchange=str(item.get("derivative_exchange") or ""),
            extra_reasons=risk_reasons,
            extra_warnings=risk_warnings,
        )
        candidate = item.get("status") == "candidate" and threshold_ok and bool(legs)
        if funding_bps is None:
            fallback = "watching"
            reason = "funding rate unavailable"
        elif not threshold_ok:
            fallback = "watching"
            reason = (
                f"funding {funding_bps:.4g} bps below "
                f"threshold {effective_threshold:.4g} bps"
            )
        else:
            fallback = str(item.get("status") or "watching")
            reason = str(item.get("reason") or paper.get("reason") or "watching")
        status = _status_from_candidate(candidate, risk, fallback)
        plan_summary = (
            " / ".join(
                f"{leg.get('side')} {leg.get('symbol')} @ {leg.get('exchange')}"
                for leg in legs
                if isinstance(leg, dict)
            )
            if legs
            else "Waiting for entry conditions"
        )
        rows.append(
            _row(
                strategy_id="funding_bot",
                strategy="Funding Bot",
                status=status,
                reason=reason,
                market=_market_from_funding_row(item),
                signal={
                    "primary": "funding",
                    "funding_rate_bps": funding_bps,
                    "basis_bps": item.get("basis_bps"),
                    "estimated_apr_pct": item.get("estimated_apr_pct"),
                    "threshold_bps": effective_threshold,
                    "detail": str(item.get("direction") or ""),
                },
                plan={
                    "summary": plan_summary,
                    "notional_quote": paper.get("notional_quote") or _effective_notional(cfg),
                    "legs": legs,
                    "protection": protection,
                },
                risk=risk,
                warnings=list(item.get("warnings", []) or []),
                observed_at=item.get("observed_at") or now,
            )
        )
    if not rows:
        risk = _risk_row(cfg, strategy_id="funding_bot")
        rows.append(
            _row(
                strategy_id="funding_bot",
                strategy="Funding Bot",
                status="watching" if risk.get("status") != "blocked" else "blocked",
                reason="no funding/basis pair configured",
                market={"label": "strategy center"},
                signal={"primary": "funding", "detail": ""},
                plan={"summary": "Add funding pair in Strategy Center"},
                risk=risk,
                observed_at=now,
            )
        )
    return rows


def _basis_legs(
    item: dict[str, Any],
    *,
    notional_quote: float,
    now: float,
    risk: RiskConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    spot_mid = _number_or_none(item.get("spot_mid"))
    derivative_mid = _number_or_none(item.get("derivative_mid"))
    basis_bps = _number_or_none(item.get("basis_bps"))
    if spot_mid is None or derivative_mid is None or basis_bps is None:
        return [], None
    if spot_mid <= 0 or derivative_mid <= 0:
        return [], None
    quantity = min(notional_quote / spot_mid, notional_quote / derivative_mid)
    if quantity <= 0:
        return [], None
    asset = _base_asset(str(item.get("spot_symbol") or item.get("derivative_symbol") or ""))
    if basis_bps >= 0:
        spot_side = "buy"
        derivative_side = "sell"
    else:
        spot_side = "sell"
        derivative_side = "buy"
    legs = [
        {
            "exchange": item.get("spot_exchange", ""),
            "symbol": item.get("spot_symbol", ""),
            "side": spot_side,
            "type": "spot",
            "quantity_base": quantity,
            "average_price": spot_mid,
            "notional_quote": quantity * spot_mid,
            "hedge_asset": asset,
            "hedge_base_equivalent": quantity,
        },
        {
            "exchange": item.get("derivative_exchange", ""),
            "symbol": item.get("derivative_symbol", ""),
            "side": derivative_side,
            "type": "perp",
            "quantity_base": quantity,
            "average_price": derivative_mid,
            "notional_quote": quantity * derivative_mid,
            "hedge_asset": asset,
            "hedge_base_equivalent": quantity,
        },
    ]
    protection = build_multileg_execution_protection(
        strategy="basis_bot",
        legs=legs,
        risk=risk,
        observed_at=item.get("observed_at") or now,
        now=now,
    )
    return legs, protection


def _basis_bot_rows(
    cfg: BotConfig,
    funding_basis: dict[str, Any],
    *,
    now: float,
) -> list[dict[str, Any]]:
    contract_cfg = cfg.contract_strategies
    if not (contract_cfg.enabled and contract_cfg.basis_bot_enabled):
        return [
            _row(
                strategy_id="basis_bot",
                strategy="Basis Bot",
                status="disabled",
                reason="basis bot disabled",
                market={"label": "--"},
                signal={"primary": "--", "detail": ""},
                plan={"summary": "No paper plan"},
                risk=_disabled_risk(),
                observed_at=now,
            )
        ]

    rows = []
    threshold = abs(float(contract_cfg.basis_entry_bps or cfg.min_basis_bps or 0.0))
    notional = _effective_notional(cfg)
    for item in funding_basis.get("rows", []) or []:
        if not isinstance(item, dict):
            continue
        basis_bps = _number_or_none(item.get("basis_bps"))
        reasons: list[str] = []
        warnings: list[str] = []
        if item.get("spot_mid") is None:
            reasons.append("spot order book unavailable")
        if item.get("derivative_mid") is None:
            reasons.append("perp order book unavailable")
        if basis_bps is None:
            reasons.append("basis unavailable")
        if basis_bps is not None and abs(basis_bps) < threshold:
            reasons.append(
                f"basis {basis_bps:.4g} bps below entry {threshold:.4g} bps"
            )
        if basis_bps is not None and basis_bps < 0:
            warnings.append("negative basis leg may require spot inventory or borrow")
        candidate = not reasons
        legs, protection = (
            _basis_legs(
                item,
                notional_quote=notional,
                now=now,
                risk=cfg.risk,
            )
            if candidate
            else ([], None)
        )
        if protection:
            reasons.extend(str(reason) for reason in protection.get("reasons", []) or [])
            warnings.extend(
                str(warning) for warning in protection.get("warnings", []) or []
            )
        risk = _risk_row(
            cfg,
            strategy_id="basis_bot",
            exchange=str(item.get("derivative_exchange") or ""),
            extra_reasons=reasons if protection else [],
            extra_warnings=warnings,
        )
        candidate = candidate and risk.get("status") != "blocked"
        direction = (
            "positive basis reversion"
            if basis_bps is not None and basis_bps >= 0
            else "negative basis reversion"
            if basis_bps is not None
            else ""
        )
        rows.append(
            _row(
                strategy_id="basis_bot",
                strategy="Basis Bot",
                status=_status_from_candidate(candidate, risk, "watching"),
                reason=(
                    "entry conditions met"
                    if candidate
                    else (reasons or ["waiting for basis entry"])[0]
                ),
                market=_market_from_funding_row(item),
                signal={
                    "primary": "basis",
                    "basis_bps": basis_bps,
                    "funding_rate_bps": item.get("funding_rate_bps"),
                    "threshold_bps": threshold,
                    "exit_bps": contract_cfg.basis_exit_bps,
                    "detail": direction,
                },
                plan={
                    "summary": (
                        " / ".join(
                            f"{leg.get('side')} {leg.get('symbol')} @ {leg.get('exchange')}"
                            for leg in legs
                        )
                        if legs
                        else "Waiting for basis entry"
                    ),
                    "notional_quote": notional,
                    "legs": legs,
                    "protection": protection,
                },
                risk=risk,
                warnings=warnings,
                observed_at=item.get("observed_at") or now,
            )
        )
    if not rows:
        risk = _risk_row(cfg, strategy_id="basis_bot")
        rows.append(
            _row(
                strategy_id="basis_bot",
                strategy="Basis Bot",
                status="watching" if risk.get("status") != "blocked" else "blocked",
                reason="no funding/basis pair configured",
                market={"label": "strategy center"},
                signal={"primary": "basis", "detail": ""},
                plan={"summary": "Add basis pair in Strategy Center"},
                risk=risk,
                observed_at=now,
            )
        )
    return rows


def _selected_derivative_reference(
    cfg: BotConfig,
    funding_basis: dict[str, Any],
) -> dict[str, Any]:
    contract_cfg = cfg.contract_strategies
    target_exchange = contract_cfg.derivative_exchange
    target_symbol = contract_cfg.derivative_symbol
    for item in funding_basis.get("rows", []) or []:
        if not isinstance(item, dict):
            continue
        exchange = str(item.get("derivative_exchange") or "")
        symbol = str(item.get("derivative_symbol") or "")
        if target_exchange and exchange != target_exchange:
            continue
        if target_symbol and symbol != target_symbol:
            continue
        return {
            "exchange": exchange,
            "symbol": symbol,
            "mid": _number_or_none(item.get("derivative_mid")),
            "spot_exchange": item.get("spot_exchange", ""),
            "spot_symbol": item.get("spot_symbol", ""),
            "basis_bps": item.get("basis_bps"),
            "funding_rate_bps": item.get("funding_rate_bps"),
            "observed_at": item.get("observed_at"),
        }
    return {
        "exchange": target_exchange,
        "symbol": target_symbol,
        "mid": None,
        "spot_exchange": contract_cfg.spot_exchange,
        "spot_symbol": contract_cfg.spot_symbol,
        "basis_bps": None,
        "funding_rate_bps": None,
        "observed_at": None,
    }


def _contract_market_label(reference: dict[str, Any]) -> dict[str, Any]:
    exchange = str(reference.get("exchange") or "")
    symbol = str(reference.get("symbol") or "")
    spot_exchange = str(reference.get("spot_exchange") or "")
    spot_symbol = str(reference.get("spot_symbol") or "")
    if spot_exchange or spot_symbol:
        label = f"{spot_exchange or '--'} {spot_symbol or '--'} / {exchange or '--'} {symbol or '--'}"
    else:
        label = f"{exchange or '--'} {symbol or '--'}"
    return {
        "label": label,
        "spot_exchange": spot_exchange,
        "spot_symbol": spot_symbol,
        "derivative_exchange": exchange,
        "derivative_symbol": symbol,
    }


def _futures_grid_rows(
    cfg: BotConfig,
    funding_basis: dict[str, Any],
    *,
    now: float,
) -> list[dict[str, Any]]:
    contract_cfg = cfg.contract_strategies
    if not (contract_cfg.enabled and contract_cfg.futures_grid_enabled):
        return [
            _row(
                strategy_id="futures_grid",
                strategy="Futures Grid",
                status="disabled",
                reason="futures grid disabled",
                market={"label": "--"},
                signal={"primary": "--", "detail": ""},
                plan={"summary": "No paper plan"},
                risk=_disabled_risk(),
                observed_at=now,
            )
        ]

    reference = _selected_derivative_reference(cfg, funding_basis)
    mid = _number_or_none(reference.get("mid"))
    reasons: list[str] = []
    warnings: list[str] = []
    if not reference.get("exchange") or not reference.get("symbol"):
        reasons.append("derivative exchange and symbol are required")
    if mid is None or mid <= 0:
        reasons.append("derivative mid price unavailable")
    levels = max(1, int(contract_cfg.futures_grid_levels or 1))
    band_pct = abs(float(contract_cfg.futures_grid_band_pct or 0.0))
    quote_per_level = float(contract_cfg.futures_grid_quote_per_level or 0.0)
    if band_pct <= 0:
        reasons.append("futures grid band must be positive")
    if quote_per_level <= 0:
        reasons.append("quote per level must be positive")
    max_leverage = max(1.0, float(contract_cfg.futures_grid_max_leverage or 1.0))
    leverage_cap = cfg.risk.max_derivative_leverage
    leverage = (
        min(max_leverage, leverage_cap)
        if leverage_cap and leverage_cap > 0
        else max_leverage
    )
    if leverage != max_leverage:
        warnings.append(
            f"leverage capped from {max_leverage:.4g}x to {leverage:.4g}x by risk"
        )
    if leverage > 1.0:
        warnings.append("futures grid uses leverage above 1x; keep live disabled")

    orders: list[dict[str, Any]] = []
    if not reasons and mid is not None:
        for index in range(1, levels + 1):
            offset = (band_pct / 100.0) * index / levels
            for side, price in (
                ("buy", mid * (1.0 - offset)),
                ("sell", mid * (1.0 + offset)),
            ):
                if price <= 0:
                    continue
                orders.append(
                    {
                        "exchange": reference["exchange"],
                        "symbol": reference["symbol"],
                        "side": side,
                        "type": "limit",
                        "price": price,
                        "quantity_base": quote_per_level / price,
                        "notional_quote": quote_per_level,
                        "leverage": leverage,
                        "post_only": contract_cfg.post_only,
                        "reduce_only": False,
                        "client_order_prefix": contract_cfg.client_order_prefix,
                    }
                )
    risk = _risk_row(
        cfg,
        strategy_id="futures_grid",
        exchange=str(reference.get("exchange") or ""),
        extra_reasons=reasons,
        extra_warnings=warnings,
    )
    candidate = bool(orders) and risk.get("status") != "blocked"
    return [
        _row(
            strategy_id="futures_grid",
            strategy="Futures Grid",
            status=_status_from_candidate(candidate, risk, "watching"),
            reason=(
                "grid levels generated"
                if candidate
                else (reasons or ["waiting for derivative price"])[0]
            ),
            market=_contract_market_label(reference),
            signal={
                "primary": "grid",
                "mid_price": mid,
                "basis_bps": reference.get("basis_bps"),
                "funding_rate_bps": reference.get("funding_rate_bps"),
                "detail": f"{levels} levels each side within {band_pct:.4g}%",
            },
            plan={
                "summary": (
                    f"{len(orders)} paper orders, {quote_per_level:.4g} quote each"
                    if orders
                    else "No grid orders"
                ),
                "orders": orders,
                "order_count": len(orders),
                "quote_per_level": quote_per_level,
                "leverage": leverage,
                "post_only": contract_cfg.post_only,
            },
            risk=risk,
            warnings=warnings,
            observed_at=reference.get("observed_at") or now,
        )
    ]


def _trade_source(trade: dict[str, Any]) -> str:
    attribution = trade.get("attribution") if isinstance(trade.get("attribution"), dict) else {}
    return str(
        trade.get("source")
        or attribution.get("source")
        or attribution.get("strategy")
        or ""
    ).lower()


def _recent_market_maker_delta(
    cfg: BotConfig,
    order_activity: dict[str, Any],
    market_maker: dict[str, Any],
) -> dict[str, Any]:
    target_bases = {
        _base_asset(cfg.market_maker.symbol),
        _base_asset(cfg.contract_strategies.spot_symbol),
        _base_asset(cfg.contract_strategies.derivative_symbol),
    }
    target_bases = {base for base in target_bases if base}
    net_base = 0.0
    notional = 0.0
    trade_count = 0
    for trade in order_activity.get("recent_trades", []) or []:
        if not isinstance(trade, dict):
            continue
        source = _trade_source(trade)
        if source not in {"market_maker", "mm"}:
            continue
        base = _base_asset(str(trade.get("symbol") or ""))
        if target_bases and base and base not in target_bases:
            continue
        side = str(trade.get("side") or "").lower()
        amount = _number_or_none(trade.get("amount")) or 0.0
        if side == "buy":
            net_base += amount
        elif side == "sell":
            net_base -= amount
        else:
            continue
        trade_count += 1
        trade_notional = (
            _number_or_none(trade.get("notional_common"))
            or _number_or_none(trade.get("cost"))
            or 0.0
        )
        notional += trade_notional

    quality = market_maker.get("quality") if isinstance(market_maker.get("quality"), dict) else {}
    if trade_count == 0 and isinstance(quality, dict):
        buy = quality.get("buy") if isinstance(quality.get("buy"), dict) else {}
        sell = quality.get("sell") if isinstance(quality.get("sell"), dict) else {}
        buy_base = _number_or_none(buy.get("base_amount")) or 0.0
        sell_base = _number_or_none(sell.get("base_amount")) or 0.0
        quality_trade_count = int(_number_or_none(quality.get("recent_trade_count")) or 0)
        if quality_trade_count > 0:
            net_base = buy_base - sell_base
            trade_count = quality_trade_count
            notional = _number_or_none(quality.get("total_notional")) or 0.0

    return {
        "net_base": net_base,
        "trade_count": trade_count,
        "notional_quote": notional,
        "target_bases": sorted(target_bases),
    }


def _hedge_rebalancer_rows(
    cfg: BotConfig,
    funding_basis: dict[str, Any],
    market_maker: dict[str, Any],
    order_activity: dict[str, Any],
    *,
    now: float,
) -> list[dict[str, Any]]:
    contract_cfg = cfg.contract_strategies
    if not (contract_cfg.enabled and contract_cfg.hedge_rebalancer_enabled):
        return [
            _row(
                strategy_id="hedge_rebalancer",
                strategy="Hedge Rebalancer",
                status="disabled",
                reason="hedge rebalancer disabled",
                market={"label": "--"},
                signal={"primary": "--", "detail": ""},
                plan={"summary": "No paper plan"},
                risk=_disabled_risk(),
                observed_at=now,
            )
        ]

    reference = _selected_derivative_reference(cfg, funding_basis)
    mid = _number_or_none(reference.get("mid"))
    delta = _recent_market_maker_delta(cfg, order_activity, market_maker)
    net_base = float(delta["net_base"])
    threshold = abs(float(contract_cfg.hedge_threshold_base or 0.0))
    reasons: list[str] = []
    warnings = ["paper hedge only; reduce-only depends on current derivative position"]
    if not reference.get("exchange") or not reference.get("symbol"):
        reasons.append("derivative exchange and symbol are required")
    if mid is None or mid <= 0:
        reasons.append("derivative mid price unavailable")
    if int(delta["trade_count"]) <= 0:
        reasons.append("no recent market maker fills")
    if abs(net_base) <= threshold:
        reasons.append(
            f"net MM delta {net_base:.8g} base within threshold {threshold:.8g}"
        )

    order: dict[str, Any] | None = None
    if not reasons and mid is not None and mid > 0:
        quantity = abs(net_base)
        max_quote = float(contract_cfg.hedge_max_quote or 0.0)
        if max_quote > 0:
            quantity = min(quantity, max_quote / mid)
        order = {
            "exchange": reference["exchange"],
            "symbol": reference["symbol"],
            "side": "sell" if net_base > 0 else "buy",
            "type": "market",
            "quantity_base": quantity,
            "reference_price": mid,
            "notional_quote": quantity * mid,
            "post_only": False,
            "reduce_only": False,
            "client_order_prefix": contract_cfg.client_order_prefix,
        }
    risk = _risk_row(
        cfg,
        strategy_id="hedge_rebalancer",
        exchange=str(reference.get("exchange") or ""),
        extra_reasons=reasons,
        extra_warnings=warnings,
    )
    candidate = order is not None and risk.get("status") != "blocked"
    return [
        _row(
            strategy_id="hedge_rebalancer",
            strategy="Hedge Rebalancer",
            status=_status_from_candidate(candidate, risk, "watching"),
            reason=(
                "hedge delta generated"
                if candidate
                else (reasons or ["waiting for MM fill delta"])[0]
            ),
            market=_contract_market_label(reference),
            signal={
                "primary": "delta",
                "net_mm_delta_base": net_base,
                "trade_count": delta["trade_count"],
                "threshold_base": threshold,
                "detail": ", ".join(delta["target_bases"]),
            },
            plan={
                "summary": (
                    f"{order['side']} {order['quantity_base']:.8g} "
                    f"{reference.get('symbol')} as paper hedge"
                    if order
                    else "No hedge order"
                ),
                "order": order,
                "orders": [order] if order else [],
                "order_count": 1 if order else 0,
            },
            risk=risk,
            warnings=warnings,
            observed_at=now,
        )
    ]


def _summary_for_rows(rows: list[dict[str, Any]], strategy_id: str) -> dict[str, Any]:
    matching = [row for row in rows if row.get("strategy_id") == strategy_id]
    candidate_count = sum(1 for row in matching if row.get("status") == "candidate")
    blocked_count = sum(1 for row in matching if row.get("status") == "blocked")
    disabled_count = sum(1 for row in matching if row.get("status") == "disabled")
    if not matching or disabled_count == len(matching):
        status = "disabled"
    elif blocked_count:
        status = "blocked"
    elif candidate_count:
        status = "candidate"
    else:
        status = "watching"
    return {
        "status": status,
        "row_count": len(matching),
        "candidate_count": candidate_count,
        "blocked_count": blocked_count,
    }


def _overall_status(rows: list[dict[str, Any]], cfg: ContractStrategiesConfig) -> str:
    if not cfg.enabled:
        return "disabled"
    if any(row.get("status") == "blocked" for row in rows):
        return "blocked"
    if any(row.get("status") == "candidate" for row in rows):
        return "candidate"
    if any(row.get("status") == "watching" for row in rows):
        return "watching"
    return "disabled"


def build_contract_strategies_payload(
    cfg: BotConfig,
    *,
    funding_basis: dict[str, Any] | None = None,
    derivatives: dict[str, Any] | None = None,
    market_maker: dict[str, Any] | None = None,
    order_activity: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    checked_at = time.time() if now is None else now
    funding_basis = funding_basis or {}
    derivatives = derivatives or {}
    market_maker = market_maker or {}
    order_activity = order_activity or {}
    rows = [
        *_funding_bot_rows(cfg, funding_basis, now=checked_at),
        *_basis_bot_rows(cfg, funding_basis, now=checked_at),
        *_futures_grid_rows(cfg, funding_basis, now=checked_at),
        *_hedge_rebalancer_rows(
            cfg,
            funding_basis,
            market_maker,
            order_activity,
            now=checked_at,
        ),
    ]
    summaries = {
        strategy_id: _summary_for_rows(rows, strategy_id)
        for strategy_id in sorted(CONTRACT_STRATEGY_IDS)
    }
    return {
        "status": _overall_status(rows, cfg.contract_strategies),
        "mode": "paper",
        "config": asdict(cfg.contract_strategies),
        "summary": summaries,
        "rows": rows,
        "candidate_count": sum(1 for row in rows if row.get("status") == "candidate"),
        "blocked_count": sum(1 for row in rows if row.get("status") == "blocked"),
        "configured_count": sum(
            1 for row in rows if row.get("status") != "disabled"
        ),
        "derivative_status": derivatives.get("status"),
        "execution_controls": {
            "paper_mode_only": True,
            "auto_submit_live_orders": False,
            "live_submit_allowed": False,
            "requires_explicit_live_confirmation": True,
        },
        "last_finished": checked_at,
        "errors": [],
        "warnings": [
            "contract strategy orders are paper plans only until a live executor is added"
        ],
    }
