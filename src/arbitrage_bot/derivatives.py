from __future__ import annotations

from typing import Any

from .config import ExchangeConfig, RiskConfig


STABLE_MARGIN_CURRENCIES = {"USD", "USDC", "USDT", "BUSD", "FDUSD"}


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _nested_number(raw: dict[str, Any], *keys: str) -> float | None:
    current: Any = raw
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _number_or_none(current)


def _position_side(raw: dict[str, Any], contracts: float | None) -> str:
    side = str(raw.get("side") or "").lower()
    if side in {"long", "short"}:
        return side
    if side in {"buy", "bid"}:
        return "long"
    if side in {"sell", "ask"}:
        return "short"
    if contracts is not None:
        if contracts > 0:
            return "long"
        if contracts < 0:
            return "short"
    return ""


def _liquidation_buffer_pct(
    *,
    side: str,
    mark_price: float | None,
    liquidation_price: float | None,
) -> float | None:
    if mark_price is None or liquidation_price is None:
        return None
    if mark_price <= 0 or liquidation_price <= 0:
        return None
    if side == "long":
        return (mark_price - liquidation_price) / mark_price * 100
    if side == "short":
        return (liquidation_price - mark_price) / mark_price * 100
    return abs(mark_price - liquidation_price) / mark_price * 100


def _position_status(row: dict[str, Any], risk: RiskConfig) -> tuple[str, list[str]]:
    reasons: list[str] = []
    leverage = row.get("leverage")
    if (
        risk.max_derivative_leverage > 0
        and leverage is not None
        and float(leverage) > risk.max_derivative_leverage
    ):
        reasons.append(
            f"leverage {float(leverage):.4g} > {risk.max_derivative_leverage:.4g}"
        )
    buffer_pct = row.get("liquidation_buffer_pct")
    if (
        risk.min_liquidation_buffer_pct > 0
        and buffer_pct is not None
        and float(buffer_pct) < risk.min_liquidation_buffer_pct
    ):
        reasons.append(
            "liquidation buffer "
            f"{float(buffer_pct):.4g}% < {risk.min_liquidation_buffer_pct:.4g}%"
        )
    return ("blocked" if reasons else "ok", reasons)


def normalize_derivative_position(
    exchange: ExchangeConfig,
    raw: dict[str, Any],
    *,
    risk: RiskConfig,
) -> dict[str, Any] | None:
    symbol = str(raw.get("symbol") or raw.get("future") or "").strip()
    contracts = _number_or_none(raw.get("contracts"))
    if contracts is None:
        contracts = _nested_number(raw, "info", "positionAmt")
    contract_size = _number_or_none(raw.get("contractSize")) or 1.0
    mark_price = (
        _number_or_none(raw.get("markPrice"))
        or _number_or_none(raw.get("mark_price"))
        or _nested_number(raw, "info", "markPrice")
    )
    entry_price = (
        _number_or_none(raw.get("entryPrice"))
        or _number_or_none(raw.get("entry_price"))
        or _nested_number(raw, "info", "entryPrice")
    )
    notional = (
        _number_or_none(raw.get("notional"))
        or _number_or_none(raw.get("notionalValue"))
        or _nested_number(raw, "info", "notional")
        or _nested_number(raw, "info", "positionValue")
    )
    if notional is None and contracts is not None and mark_price is not None:
        notional = contracts * contract_size * mark_price

    leverage = (
        _number_or_none(raw.get("leverage"))
        or _nested_number(raw, "info", "leverage")
    )
    liquidation_price = (
        _number_or_none(raw.get("liquidationPrice"))
        or _number_or_none(raw.get("liquidation_price"))
        or _nested_number(raw, "info", "liquidationPrice")
    )
    side = _position_side(raw, contracts)
    base_amount = abs(contracts or 0.0) * contract_size
    notional_abs = abs(notional or 0.0)
    if not symbol and notional_abs <= 0 and base_amount <= 0:
        return None
    if notional_abs <= 0 and base_amount <= 0:
        return None

    row: dict[str, Any] = {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "symbol": symbol,
        "side": side,
        "contracts": contracts,
        "contract_size": contract_size,
        "base_amount": base_amount,
        "notional_quote": notional_abs,
        "entry_price": entry_price,
        "mark_price": mark_price,
        "liquidation_price": liquidation_price,
        "liquidation_buffer_pct": _liquidation_buffer_pct(
            side=side,
            mark_price=mark_price,
            liquidation_price=liquidation_price,
        ),
        "leverage": leverage,
        "margin_mode": str(raw.get("marginMode") or raw.get("margin_mode") or ""),
        "initial_margin": (
            _number_or_none(raw.get("initialMargin"))
            or _nested_number(raw, "info", "initialMargin")
        ),
        "maintenance_margin": (
            _number_or_none(raw.get("maintenanceMargin"))
            or _nested_number(raw, "info", "maintMargin")
        ),
        "margin_ratio": (
            _number_or_none(raw.get("marginRatio"))
            or _nested_number(raw, "info", "marginRatio")
        ),
        "unrealized_pnl": (
            _number_or_none(raw.get("unrealizedPnl"))
            or _number_or_none(raw.get("unrealizedProfit"))
            or _nested_number(raw, "info", "unRealizedProfit")
        ),
        "timestamp": _number_or_none(raw.get("timestamp")),
    }
    status, reasons = _position_status(row, risk)
    row["status"] = status
    row["risk_reasons"] = reasons
    return row


def _balance_field(balance: dict[str, Any], currency: str, field: str) -> float | None:
    nested = balance.get(currency)
    if isinstance(nested, dict):
        value = _number_or_none(nested.get(field))
        if value is not None:
            return value
    by_field = balance.get(field)
    if isinstance(by_field, dict):
        return _number_or_none(by_field.get(currency))
    return None


def derivative_account_summary(
    balance: dict[str, Any],
    positions: list[dict[str, Any]],
    *,
    currencies: set[str],
    risk: RiskConfig,
) -> dict[str, Any]:
    currencies = {currency.upper() for currency in currencies if currency}
    currencies.update(STABLE_MARGIN_CURRENCIES)
    free = 0.0
    used = 0.0
    total = 0.0
    for currency in sorted(currencies):
        free += float(_balance_field(balance, currency, "free") or 0.0)
        used += float(_balance_field(balance, currency, "used") or 0.0)
        total += float(_balance_field(balance, currency, "total") or 0.0)

    notional = sum(float(row.get("notional_quote") or 0.0) for row in positions)
    unrealized_pnl = sum(float(row.get("unrealized_pnl") or 0.0) for row in positions)
    leverages = [
        float(row["leverage"])
        for row in positions
        if row.get("leverage") is not None
    ]
    buffers = [
        float(row["liquidation_buffer_pct"])
        for row in positions
        if row.get("liquidation_buffer_pct") is not None
    ]
    margin_usage_pct = used / total * 100 if total > 0 else None
    reasons: list[str] = []
    if (
        risk.max_margin_usage_pct > 0
        and margin_usage_pct is not None
        and margin_usage_pct > risk.max_margin_usage_pct
    ):
        reasons.append(
            f"margin usage {margin_usage_pct:.4g}% > {risk.max_margin_usage_pct:.4g}%"
        )
    for row in positions:
        reasons.extend(str(reason) for reason in row.get("risk_reasons", []))

    return {
        "free_margin_quote": free,
        "used_margin_quote": used,
        "equity_quote": total,
        "margin_usage_pct": margin_usage_pct,
        "position_notional_quote": notional,
        "unrealized_pnl_quote": unrealized_pnl,
        "max_leverage": max(leverages) if leverages else None,
        "min_liquidation_buffer_pct": min(buffers) if buffers else None,
        "position_count": len(positions),
        "status": "blocked" if reasons else "ok",
        "risk_reasons": sorted(set(reasons)),
    }
