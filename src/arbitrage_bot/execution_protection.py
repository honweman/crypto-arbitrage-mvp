from __future__ import annotations

import time
from typing import Any

from .config import RiskConfig


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric else None


def _symbol_base(symbol: str) -> str:
    text = str(symbol or "")
    if "/" in text:
        return text.split("/", 1)[0].split(":", 1)[0].upper()
    if "-" in text:
        return text.split("-", 1)[0].upper()
    return text.upper()


def _leg_side(leg: dict[str, Any]) -> str:
    return str(leg.get("side") or "").lower()


def _leg_base_equivalent(leg: dict[str, Any]) -> float:
    explicit = _number_or_none(leg.get("hedge_base_equivalent"))
    if explicit is not None:
        return max(0.0, explicit)
    amount = _number_or_none(
        leg.get("quantity_base")
        if leg.get("quantity_base") is not None
        else leg.get("amount")
    )
    return max(0.0, amount or 0.0)


def _leg_hedge_asset(leg: dict[str, Any]) -> str:
    return str(leg.get("hedge_asset") or _symbol_base(str(leg.get("symbol") or "")))


def _is_option_leg(leg: dict[str, Any]) -> bool:
    leg_type = str(leg.get("type") or "").lower()
    if leg_type == "option":
        return True
    symbol = str(leg.get("symbol") or "")
    return "/" not in symbol and ("-C" in symbol or "-P" in symbol)


def _net_base_by_asset(legs: list[dict[str, Any]]) -> dict[str, float]:
    exposure: dict[str, float] = {}
    for leg in legs:
        side = _leg_side(leg)
        if side not in {"buy", "sell"}:
            continue
        asset = _leg_hedge_asset(leg)
        signed = _leg_base_equivalent(leg) * (1.0 if side == "buy" else -1.0)
        exposure[asset] = exposure.get(asset, 0.0) + signed
    return exposure


def _largest_abs_exposure(exposure: dict[str, float]) -> tuple[str, float]:
    if not exposure:
        return "", 0.0
    asset, value = max(exposure.items(), key=lambda item: abs(item[1]))
    return asset, value


def _scenario_from_filled_legs(
    *,
    name: str,
    filled_legs: list[dict[str, Any]],
    requires_manual_review: bool,
) -> dict[str, Any]:
    exposure = _net_base_by_asset(filled_legs)
    asset, imbalance = _largest_abs_exposure(exposure)
    hedge_side = "sell" if imbalance > 0 else "buy" if imbalance < 0 else ""
    hedge_base = abs(imbalance)
    hedge_required = hedge_base > 1e-12
    status = (
        "manual_review"
        if requires_manual_review and hedge_required
        else "hedge_required"
        if hedge_required
        else "balanced"
    )
    return {
        "name": name,
        "status": status,
        "filled_leg_count": len(filled_legs),
        "hedge_required": hedge_required,
        "hedge_asset": asset,
        "hedge_side": hedge_side,
        "hedge_base": hedge_base,
        "net_base_by_asset": exposure,
        "manual_intervention_required": requires_manual_review and hedge_required,
    }


def build_multileg_execution_protection(
    *,
    strategy: str,
    legs: list[dict[str, Any]],
    risk: RiskConfig,
    observed_at: float | None,
    now: float | None = None,
    paper_mode: bool = True,
) -> dict[str, Any]:
    evaluated_at = time.time() if now is None else now
    plan_age_seconds = (
        max(0.0, evaluated_at - observed_at)
        if isinstance(observed_at, (int, float)) and observed_at > 0
        else None
    )
    slippages = [
        value
        for value in (_number_or_none(leg.get("slippage_bps")) for leg in legs)
        if value is not None
    ]
    max_slippage = max(slippages) if slippages else None
    has_buy = any(_leg_side(leg) == "buy" for leg in legs)
    has_sell = any(_leg_side(leg) == "sell" for leg in legs)
    requires_manual_review = any(_is_option_leg(leg) for leg in legs)
    reasons: list[str] = []
    warnings: list[str] = []

    if len(legs) < 2:
        reasons.append("multi-leg strategy needs at least two legs")
    if not has_buy or not has_sell:
        warnings.append("legs do not include both buy and sell exposure")
    if max_slippage is None:
        warnings.append("slippage is not estimated for every leg")
    elif risk.max_slippage_bps > 0 and max_slippage > risk.max_slippage_bps:
        reasons.append(
            f"max slippage {max_slippage:.4g} bps exceeds limit {risk.max_slippage_bps:.4g} bps"
        )
    if plan_age_seconds is None:
        warnings.append("opportunity age is unavailable")
    elif risk.max_plan_age_seconds > 0 and plan_age_seconds > risk.max_plan_age_seconds:
        reasons.append(
            f"plan age {plan_age_seconds:.4g}s exceeds limit {risk.max_plan_age_seconds:.4g}s"
        )
    if requires_manual_review:
        warnings.append("option legs require assignment, expiry, and margin controls")

    net_exposure = _net_base_by_asset(legs)
    _, all_fill_imbalance = _largest_abs_exposure(net_exposure)
    if abs(all_fill_imbalance) > 1e-12:
        warnings.append("all-leg fill would leave residual base exposure")

    status = "blocked" if reasons else "warning" if warnings else "ok"
    first_leg = legs[:1]
    half_first_leg: list[dict[str, Any]] = []
    if first_leg:
        leg = dict(first_leg[0])
        amount = _leg_base_equivalent(leg)
        if amount > 0:
            leg["hedge_base_equivalent"] = amount / 2.0
            leg["quantity_base"] = amount / 2.0
        half_first_leg = [leg]

    playbooks = [
        {
            "event": "one_leg_create_failed",
            "action": "cancel_successful_orders_then_sync_fills",
            "next": "hedge_residual_if_any_and_pause_strategy",
            "auto_submit_live_orders": False,
        },
        {
            "event": "partial_fill_detected",
            "action": "cancel_unfilled_remainders_then_hedge_residual",
            "next": "pause_strategy_until_reconciled",
            "auto_submit_live_orders": False,
        },
        {
            "event": "slippage_exceeded",
            "action": "abandon_plan_and_rescan",
            "next": "do_not_submit_orders",
            "auto_submit_live_orders": False,
        },
        {
            "event": "stale_data",
            "action": "abandon_plan_and_refresh_order_books",
            "next": "do_not_submit_orders",
            "auto_submit_live_orders": False,
        },
    ]
    return {
        "status": status,
        "strategy": strategy,
        "paper_mode": paper_mode,
        "live_submit_allowed": False,
        "would_submit_if_live": status == "ok",
        "leg_count": len(legs),
        "has_buy_and_sell": has_buy and has_sell,
        "requires_manual_review": requires_manual_review,
        "max_slippage_bps": max_slippage,
        "max_allowed_slippage_bps": risk.max_slippage_bps,
        "plan_age_seconds": plan_age_seconds,
        "max_plan_age_seconds": risk.max_plan_age_seconds,
        "net_base_by_asset": net_exposure,
        "reasons": reasons,
        "warnings": warnings,
        "playbooks": playbooks,
        "paper_failure_scenarios": [
            _scenario_from_filled_legs(
                name="all_legs_fill",
                filled_legs=legs,
                requires_manual_review=requires_manual_review,
            ),
            _scenario_from_filled_legs(
                name="first_leg_only",
                filled_legs=first_leg,
                requires_manual_review=requires_manual_review,
            ),
            _scenario_from_filled_legs(
                name="first_leg_half_fill",
                filled_legs=half_first_leg,
                requires_manual_review=requires_manual_review,
            ),
        ],
        "evaluated_at": evaluated_at,
    }
