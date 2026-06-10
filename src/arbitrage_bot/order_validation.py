from __future__ import annotations

from typing import Any

from .models import Side


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_limit(
    market: dict[str, Any] | None,
    group: str,
    field: str,
) -> float | None:
    if not isinstance(market, dict):
        return None
    limits = market.get("limits")
    if not isinstance(limits, dict):
        return None
    values = limits.get(group)
    if not isinstance(values, dict):
        return None
    return _number_or_none(values.get(field))


def _market_precision(
    market: dict[str, Any] | None,
    field: str,
) -> Any:
    if not isinstance(market, dict):
        return None
    precision = market.get("precision")
    if not isinstance(precision, dict):
        return None
    return precision.get(field)


def _differs(left: float, right: float) -> bool:
    tolerance = max(abs(left), abs(right), 1.0) * 1e-12
    return abs(left - right) > tolerance


def _limit_error(
    *,
    label: str,
    value: float,
    minimum: float | None,
    maximum: float | None,
) -> str | None:
    if minimum is not None and value < minimum:
        return f"{label} {value:.12g} is below exchange minimum {minimum:.12g}"
    if maximum is not None and value > maximum:
        return f"{label} {value:.12g} exceeds exchange maximum {maximum:.12g}"
    return None


def validate_prepared_limit_order(
    *,
    exchange: str,
    symbol: str,
    side: Side,
    requested_amount: float,
    requested_price: float,
    amount: float,
    price: float,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cost = amount * price
    errors: list[str] = []
    warnings: list[str] = []

    if requested_amount <= 0:
        errors.append("requested amount must be positive")
    if requested_price <= 0:
        errors.append("requested price must be positive")
    if amount <= 0:
        errors.append("precision-adjusted amount must be positive")
    if price <= 0:
        errors.append("precision-adjusted price must be positive")
    if _differs(requested_amount, amount):
        warnings.append(
            f"amount rounded from {requested_amount:.12g} to {amount:.12g}"
        )
    if _differs(requested_price, price):
        warnings.append(f"price rounded from {requested_price:.12g} to {price:.12g}")

    limits_payload: dict[str, Any] = {}
    for group, label, value in (
        ("amount", "amount", amount),
        ("price", "price", price),
        ("cost", "cost", cost),
    ):
        minimum = _market_limit(market, group, "min")
        maximum = _market_limit(market, group, "max")
        limits_payload[group] = {"min": minimum, "max": maximum}
        error = _limit_error(
            label=label,
            value=value,
            minimum=minimum,
            maximum=maximum,
        )
        if error:
            errors.append(error)

    precision_payload = {
        "amount": _market_precision(market, "amount"),
        "price": _market_precision(market, "price"),
    }
    return {
        "exchange": exchange,
        "symbol": symbol,
        "side": side,
        "status": "ok" if not errors else "error",
        "requested_amount": requested_amount,
        "requested_price": requested_price,
        "amount": amount,
        "price": price,
        "cost": cost,
        "limits": limits_payload,
        "precision": precision_payload,
        "errors": errors,
        "warnings": warnings,
    }


def summarize_order_validations(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    errors = [
        f"{row['exchange']} {row['symbol']} {row['side']}: {error}"
        for row in rows
        for error in row.get("errors", [])
    ]
    warnings = [
        f"{row['exchange']} {row['symbol']} {row['side']}: {warning}"
        for row in rows
        for warning in row.get("warnings", [])
    ]
    return {
        "status": "ok" if not errors else "error",
        "order_count": len(rows),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "total_cost": sum(float(row.get("cost") or 0.0) for row in rows),
        "orders": rows,
        "errors": errors,
        "warnings": warnings,
    }
