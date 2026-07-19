from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


USER_STRATEGY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "market_maker": {
        "label": "Market Maker",
        "min_accounts": 1,
        "max_accounts": 1,
        "parameters": {
            "levels": 2,
            "price_band_pct": 1.0,
            "quote_per_level": 1.0,
            "refresh_seconds": 10.0,
            "post_only": True,
        },
    },
    "auto_buy_sell": {
        "label": "Auto Buy/Sell",
        "min_accounts": 1,
        "max_accounts": 1,
        "parameters": {
            "side": "buy",
            "total_quote": 25.0,
            "quote_per_order": 1.0,
            "interval_seconds": 30.0,
            "start_price": 0.0,
            "stop_price": 0.0,
        },
    },
    "spot_grid": {
        "label": "Spot Grid",
        "min_accounts": 1,
        "max_accounts": 1,
        "parameters": {
            "lower_price": 0.0,
            "upper_price": 0.0,
            "grid_count": 10,
            "quote_per_grid": 1.0,
            "spacing": "arithmetic",
            "refresh_seconds": 10.0,
        },
    },
    "dca": {
        "label": "DCA",
        "min_accounts": 1,
        "max_accounts": 1,
        "parameters": {
            "side": "buy",
            "total_quote": 25.0,
            "quote_per_order": 1.0,
            "interval_seconds": 300.0,
            "trigger_price": 0.0,
            "take_profit_pct": 0.0,
        },
    },
    "spot_spread": {
        "label": "Spot Arbitrage",
        "min_accounts": 2,
        "max_accounts": 8,
        "parameters": {
            "min_profit_bps": 15.0,
            "max_cycle_quote": 5.0,
            "scan_interval_seconds": 1.0,
        },
    },
    "contract_arbitrage": {
        "label": "Contract Arbitrage (CEX/DEX)",
        "min_accounts": 2,
        "max_accounts": 8,
        "parameters": {
            "min_basis_bps": 15.0,
            "min_funding_bps": 0.0,
            "max_cycle_quote": 5.0,
            "max_leverage": 1.0,
            "scan_interval_seconds": 1.0,
            "require_dex_leg": True,
        },
    },
}

DEFAULT_USER_STRATEGY_RISK: dict[str, Any] = {
    "max_order_quote": 5.0,
    "max_total_quote": 50.0,
    "max_daily_loss_quote": 10.0,
    "max_open_orders": 50,
    "max_slippage_bps": 50.0,
    "max_order_book_age_seconds": 10.0,
    "paper_fee_bps": 60.0,
}

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SECRET_FIELD_RE = re.compile(
    r"(^|_)(api[_-]?key|secret|password|passphrase|token|private[_-]?key)($|_)",
    re.IGNORECASE,
)


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"user-strategy-{uuid.uuid4().hex[:12]}"


def _clean_id(value: Any, *, label: str, default: str = "") -> str:
    result = str(value or "").strip() or default
    if not result or not ID_RE.fullmatch(result):
        raise ValueError(f"{label} contains unsupported characters")
    return result


def _clean_email(value: Any) -> str:
    result = str(value or "").strip().lower()
    if not result or "@" not in result:
        raise ValueError("strategy owner email is required")
    return result


def _clean_text(value: Any, *, max_length: int = 80) -> str:
    return str(value or "").strip()[:max_length]


def _reject_secret_values(value: Any, *, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}" if path else key_text
            if SECRET_FIELD_RE.search(key_text):
                raise ValueError(f"{key_path} must not contain credential values")
            _reject_secret_values(item, path=key_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_values(item, path=f"{path}[{index}]")


def _finite_float(
    value: Any,
    *,
    label: str,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    if result < minimum:
        raise ValueError(f"{label} must be >= {minimum:g}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{label} must be <= {maximum:g}")
    return result


def _bounded_int(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    result = _finite_float(
        value,
        label=label,
        minimum=float(minimum),
        maximum=float(maximum),
    )
    if not result.is_integer():
        raise ValueError(f"{label} must be an integer")
    return int(result)


def _strict_bool(value: Any, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _normalized_account_ids(value: Any) -> list[str]:
    if value is None:
        rows = []
    elif isinstance(value, list):
        rows = value
    else:
        raise ValueError("strategy account_ids must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for item in rows:
        account_id = _clean_id(item, label="strategy account id")
        if account_id in seen:
            continue
        result.append(account_id)
        seen.add(account_id)
    return result


def _clean_parameters(strategy_type: str, value: Any) -> dict[str, Any]:
    if value is None:
        raw = {}
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        raise ValueError("strategy parameters must be an object")
    _reject_secret_values(raw, path="parameters")
    defaults = USER_STRATEGY_DEFINITIONS[strategy_type]["parameters"]
    unknown = sorted(set(raw).difference(defaults))
    if unknown:
        raise ValueError("unsupported strategy parameters: " + ", ".join(unknown))
    merged = {**defaults, **raw}

    if strategy_type == "market_maker":
        return {
            "levels": _bounded_int(
                merged["levels"], label="levels", minimum=1, maximum=50
            ),
            "price_band_pct": _finite_float(
                merged["price_band_pct"],
                label="price_band_pct",
                minimum=0.01,
                maximum=50.0,
            ),
            "quote_per_level": _finite_float(
                merged["quote_per_level"],
                label="quote_per_level",
                minimum=0.00000001,
            ),
            "refresh_seconds": _finite_float(
                merged["refresh_seconds"],
                label="refresh_seconds",
                minimum=1.0,
                maximum=3600.0,
            ),
            "post_only": _strict_bool(
                merged["post_only"], label="post_only", default=True
            ),
        }
    if strategy_type == "auto_buy_sell":
        side = str(merged["side"] or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return {
            "side": side,
            "total_quote": _finite_float(
                merged["total_quote"], label="total_quote", minimum=0.00000001
            ),
            "quote_per_order": _finite_float(
                merged["quote_per_order"],
                label="quote_per_order",
                minimum=0.00000001,
            ),
            "interval_seconds": _finite_float(
                merged["interval_seconds"],
                label="interval_seconds",
                minimum=1.0,
                maximum=86400.0,
            ),
            "start_price": _finite_float(
                merged["start_price"], label="start_price"
            ),
            "stop_price": _finite_float(
                merged["stop_price"], label="stop_price"
            ),
        }
    if strategy_type == "spot_grid":
        spacing = str(merged["spacing"] or "").strip().lower()
        if spacing not in {"arithmetic", "geometric"}:
            raise ValueError("spacing must be arithmetic or geometric")
        return {
            "lower_price": _finite_float(
                merged["lower_price"], label="lower_price"
            ),
            "upper_price": _finite_float(
                merged["upper_price"], label="upper_price"
            ),
            "grid_count": _bounded_int(
                merged["grid_count"], label="grid_count", minimum=2, maximum=200
            ),
            "quote_per_grid": _finite_float(
                merged["quote_per_grid"],
                label="quote_per_grid",
                minimum=0.00000001,
            ),
            "spacing": spacing,
            "refresh_seconds": _finite_float(
                merged["refresh_seconds"],
                label="refresh_seconds",
                minimum=1.0,
                maximum=3600.0,
            ),
        }
    if strategy_type == "dca":
        side = str(merged["side"] or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return {
            "side": side,
            "total_quote": _finite_float(
                merged["total_quote"], label="total_quote", minimum=0.00000001
            ),
            "quote_per_order": _finite_float(
                merged["quote_per_order"],
                label="quote_per_order",
                minimum=0.00000001,
            ),
            "interval_seconds": _finite_float(
                merged["interval_seconds"],
                label="interval_seconds",
                minimum=1.0,
                maximum=2_592_000.0,
            ),
            "trigger_price": _finite_float(
                merged["trigger_price"], label="trigger_price"
            ),
            "take_profit_pct": _finite_float(
                merged["take_profit_pct"],
                label="take_profit_pct",
                maximum=1000.0,
            ),
        }
    if strategy_type == "contract_arbitrage":
        return {
            "min_basis_bps": _finite_float(
                merged["min_basis_bps"],
                label="min_basis_bps",
                maximum=10_000.0,
            ),
            "min_funding_bps": _finite_float(
                merged["min_funding_bps"],
                label="min_funding_bps",
                maximum=1_000.0,
            ),
            "max_cycle_quote": _finite_float(
                merged["max_cycle_quote"],
                label="max_cycle_quote",
                minimum=0.00000001,
            ),
            "max_leverage": _finite_float(
                merged["max_leverage"],
                label="max_leverage",
                minimum=1.0,
                maximum=20.0,
            ),
            "scan_interval_seconds": _finite_float(
                merged["scan_interval_seconds"],
                label="scan_interval_seconds",
                minimum=0.1,
                maximum=3600.0,
            ),
            "require_dex_leg": _strict_bool(
                merged["require_dex_leg"],
                label="require_dex_leg",
                default=True,
            ),
        }
    return {
        "min_profit_bps": _finite_float(
            merged["min_profit_bps"],
            label="min_profit_bps",
            maximum=10_000.0,
        ),
        "max_cycle_quote": _finite_float(
            merged["max_cycle_quote"],
            label="max_cycle_quote",
            minimum=0.00000001,
        ),
        "scan_interval_seconds": _finite_float(
            merged["scan_interval_seconds"],
            label="scan_interval_seconds",
            minimum=0.1,
            maximum=3600.0,
        ),
    }


def _clean_risk(value: Any) -> dict[str, Any]:
    if value is None:
        raw = {}
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        raise ValueError("strategy risk must be an object")
    _reject_secret_values(raw, path="risk")
    unknown = sorted(set(raw).difference(DEFAULT_USER_STRATEGY_RISK))
    if unknown:
        raise ValueError("unsupported strategy risk fields: " + ", ".join(unknown))
    merged = {**DEFAULT_USER_STRATEGY_RISK, **raw}
    result = {
        "max_order_quote": _finite_float(
            merged["max_order_quote"], label="max_order_quote"
        ),
        "max_total_quote": _finite_float(
            merged["max_total_quote"], label="max_total_quote"
        ),
        "max_daily_loss_quote": _finite_float(
            merged["max_daily_loss_quote"], label="max_daily_loss_quote"
        ),
        "max_open_orders": _bounded_int(
            merged["max_open_orders"],
            label="max_open_orders",
            minimum=0,
            maximum=10_000,
        ),
        "max_slippage_bps": _finite_float(
            merged["max_slippage_bps"],
            label="max_slippage_bps",
            maximum=10_000.0,
        ),
        "max_order_book_age_seconds": _finite_float(
            merged["max_order_book_age_seconds"],
            label="max_order_book_age_seconds",
            maximum=3600.0,
        ),
        "paper_fee_bps": _finite_float(
            merged["paper_fee_bps"],
            label="paper_fee_bps",
            maximum=1_000.0,
        ),
    }
    if result["max_total_quote"] < result["max_order_quote"]:
        raise ValueError("max_total_quote must be >= max_order_quote")
    return result


@dataclass(frozen=True)
class UserStrategy:
    id: str
    owner_email: str
    project_id: str
    name: str
    strategy_type: str
    account_ids: list[str] = field(default_factory=list)
    enabled: bool = False
    mode: str = "paper"
    parameters: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserStrategy":
        if not isinstance(raw, dict):
            raise ValueError("user strategy must be an object")
        _reject_secret_values(raw)
        if _strict_bool(
            raw.get("live_enabled"),
            label="live_enabled",
            default=False,
        ):
            raise ValueError("user strategies are paper-only; live_enabled is not allowed")
        mode = str(raw.get("mode") or "paper").strip().lower()
        if mode != "paper":
            raise ValueError("user strategies currently support paper mode only")
        strategy_type = str(
            raw.get("strategy_type") or raw.get("type") or ""
        ).strip().lower()
        definition = USER_STRATEGY_DEFINITIONS.get(strategy_type)
        if definition is None:
            raise ValueError(f"unsupported user strategy type: {strategy_type}")
        account_ids = _normalized_account_ids(raw.get("account_ids"))
        if len(account_ids) > int(definition["max_accounts"]):
            raise ValueError(
                f"{strategy_type} supports at most {definition['max_accounts']} accounts"
            )
        now = _now()
        return cls(
            id=_clean_id(raw.get("id"), label="strategy id", default=_new_id()),
            owner_email=_clean_email(raw.get("owner_email")),
            project_id=_clean_id(raw.get("project_id"), label="strategy project id"),
            name=_clean_text(raw.get("name") or definition["label"]),
            strategy_type=strategy_type,
            account_ids=account_ids,
            enabled=_strict_bool(raw.get("enabled"), label="enabled", default=False),
            mode=mode,
            parameters=_clean_parameters(strategy_type, raw.get("parameters")),
            risk=_clean_risk(raw.get("risk")),
            created_at=float(raw.get("created_at") or now),
            updated_at=float(raw.get("updated_at") or now),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_email": self.owner_email,
            "project_id": self.project_id,
            "name": self.name,
            "strategy_type": self.strategy_type,
            "account_ids": list(self.account_ids),
            "enabled": self.enabled,
            "mode": self.mode,
            "live_enabled": False,
            "parameters": dict(self.parameters),
            "risk": dict(self.risk),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def strategy_parameter_blockers(strategy: UserStrategy) -> list[str]:
    parameters = strategy.parameters
    risk = strategy.risk
    blockers: list[str] = []
    if risk["max_order_quote"] <= 0:
        blockers.append("max order quote must be greater than zero")
    if risk["max_total_quote"] <= 0:
        blockers.append("max total quote must be greater than zero")
    if risk["max_daily_loss_quote"] <= 0:
        blockers.append("max daily loss must be greater than zero")
    if risk["max_open_orders"] <= 0:
        blockers.append("max open orders must be greater than zero")
    if risk["max_slippage_bps"] <= 0:
        blockers.append("max slippage must be greater than zero")
    if risk["max_order_book_age_seconds"] <= 0:
        blockers.append("max order book age must be greater than zero")

    if strategy.strategy_type == "market_maker":
        order_quote = parameters["quote_per_level"]
        total_quote = parameters["levels"] * 2 * order_quote
        planned_orders = parameters["levels"] * 2
    elif strategy.strategy_type in {"auto_buy_sell", "dca"}:
        order_quote = parameters["quote_per_order"]
        total_quote = parameters["total_quote"]
        planned_orders = 1
        if order_quote > total_quote:
            blockers.append("quote per order exceeds total quote target")
        if strategy.strategy_type == "auto_buy_sell":
            start = parameters["start_price"]
            stop = parameters["stop_price"]
            if start > 0 and stop > 0:
                if parameters["side"] == "buy" and stop <= start:
                    blockers.append("buy stop price must be above start price")
                if parameters["side"] == "sell" and stop >= start:
                    blockers.append("sell stop price must be below start price")
    elif strategy.strategy_type == "spot_grid":
        order_quote = parameters["quote_per_grid"]
        total_quote = (parameters["grid_count"] + 1) * order_quote
        planned_orders = parameters["grid_count"] + 1
        if parameters["lower_price"] <= 0 or parameters["upper_price"] <= 0:
            blockers.append("grid lower and upper prices are required")
        elif parameters["upper_price"] <= parameters["lower_price"]:
            blockers.append("grid upper price must be above lower price")
    elif strategy.strategy_type == "contract_arbitrage":
        order_quote = parameters["max_cycle_quote"]
        total_quote = parameters["max_cycle_quote"] * 2
        planned_orders = 2
        if parameters["max_leverage"] > 3:
            blockers.append("contract arbitrage leverage above 3x is not allowed")
    else:
        order_quote = parameters["max_cycle_quote"]
        total_quote = parameters["max_cycle_quote"]
        planned_orders = 2

    if order_quote > risk["max_order_quote"]:
        blockers.append("strategy order size exceeds max order quote")
    if total_quote > risk["max_total_quote"]:
        blockers.append("strategy budget exceeds max total quote")
    if planned_orders > risk["max_open_orders"]:
        blockers.append("strategy planned orders exceed max open orders")
    return blockers


def user_strategy_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": strategy_type,
            "label": definition["label"],
            "mode": "paper",
            "min_accounts": definition["min_accounts"],
            "max_accounts": definition["max_accounts"],
            "default_parameters": json.loads(
                json.dumps(definition["parameters"], ensure_ascii=True)
            ),
            "default_risk": dict(DEFAULT_USER_STRATEGY_RISK),
        }
        for strategy_type, definition in USER_STRATEGY_DEFINITIONS.items()
    ]
