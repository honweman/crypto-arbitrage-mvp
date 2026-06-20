from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BotConfig, ExchangeConfig, SpotGridConfig
from .exchanges import (
    ExchangeManager,
    limit_order_capability_errors,
    limit_order_features,
)
from .grid_trading import (
    GridFill,
    GridOrder,
    SpotGridPlan,
    build_spot_grid_fill_replacement_plan,
    build_spot_grid_plan,
)
from .market_maker import market_maker_quote_conversion, order_book_market_data
from .models import OrderBookSnapshot, Side
from .order_validation import summarize_order_validations
from .risk import (
    RiskMarketContext,
    RiskOrder,
    current_daily_pnl_quote,
    evaluate_order_batch,
    portfolio_positions_base,
)


@dataclass(frozen=True)
class TrackedGridOrder:
    order_id: str
    client_order_id: str
    side: Side
    level: int
    price: float
    amount: float
    quote_notional: float
    exchange: str
    symbol: str
    created_at: float = 0.0

    @classmethod
    def from_grid_order(
        cls,
        order: GridOrder,
        *,
        order_id: str,
        client_order_id: str,
        exchange: str,
        symbol: str,
        created_at: float | None = None,
    ) -> "TrackedGridOrder":
        return cls(
            order_id=order_id,
            client_order_id=client_order_id,
            side=order.side,
            level=order.level,
            price=order.price,
            amount=order.amount,
            quote_notional=order.quote_notional,
            exchange=exchange,
            symbol=symbol,
            created_at=time.time() if created_at is None else created_at,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TrackedGridOrder":
        side = str(raw.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("tracked grid side must be buy or sell")
        return cls(
            order_id=str(raw.get("order_id") or raw.get("id") or ""),
            client_order_id=str(raw.get("client_order_id") or ""),
            side=side,  # type: ignore[arg-type]
            level=int(raw.get("level") or 0),
            price=float(raw.get("price") or 0.0),
            amount=float(raw.get("amount") or 0.0),
            quote_notional=float(raw.get("quote_notional") or 0.0),
            exchange=str(raw.get("exchange") or ""),
            symbol=str(raw.get("symbol") or ""),
            created_at=float(raw.get("created_at") or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "level": self.level,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "created_at": self.created_at,
        }

    def to_fill(self, *, amount: float | None = None, cost: float | None = None) -> GridFill:
        fill_amount = self.amount if amount is None or amount <= 0 else amount
        fill_cost = self.quote_notional if cost is None or cost <= 0 else cost
        return GridFill(
            side=self.side,
            level=self.level,
            price=self.price,
            amount=fill_amount,
            quote_notional=fill_cost,
        )


def load_runtime_state(path: str | Path) -> dict[str, Any]:
    runtime_path = Path(path)
    try:
        raw = json.loads(runtime_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_runtime_state()
    except (OSError, json.JSONDecodeError):
        return _empty_runtime_state()
    if not isinstance(raw, dict):
        return _empty_runtime_state()
    orders = []
    for item in raw.get("tracked_orders", []):
        if not isinstance(item, dict):
            continue
        try:
            order = TrackedGridOrder.from_dict(item)
        except (TypeError, ValueError):
            continue
        if order.order_id or order.client_order_id:
            orders.append(order.to_dict())
    return {
        "version": 1,
        "updated_at": float(raw.get("updated_at") or 0.0),
        "exchange": str(raw.get("exchange") or ""),
        "symbol": str(raw.get("symbol") or ""),
        "tracked_orders": orders,
        "stats": raw.get("stats") if isinstance(raw.get("stats"), dict) else {},
    }


def save_runtime_state(path: str | Path, state: dict[str, Any]) -> None:
    runtime_path = Path(path)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.time(),
        **state,
    }
    tmp_path = runtime_path.with_suffix(runtime_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, runtime_path)
    try:
        os.chmod(runtime_path, 0o600)
    except OSError:
        pass


def _empty_runtime_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 0.0,
        "exchange": "",
        "symbol": "",
        "tracked_orders": [],
        "stats": {},
    }


def tracked_orders_from_state(state: dict[str, Any]) -> list[TrackedGridOrder]:
    rows: list[TrackedGridOrder] = []
    for item in state.get("tracked_orders", []):
        if not isinstance(item, dict):
            continue
        try:
            order = TrackedGridOrder.from_dict(item)
        except (TypeError, ValueError):
            continue
        if order.order_id or order.client_order_id:
            rows.append(order)
    return rows


def runtime_state_from_tracked(
    orders: list[TrackedGridOrder],
    *,
    exchange: str,
    symbol: str,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": time.time(),
        "exchange": exchange,
        "symbol": symbol,
        "tracked_orders": [order.to_dict() for order in orders],
        "stats": stats or {},
    }


def _find_exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        if exchange.key == key:
            return exchange
    raise ValueError(f"spot grid exchange is not configured: {key}")


def _tracked_key(order: TrackedGridOrder) -> str:
    return order.order_id or order.client_order_id


def _raw_order_id(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("id") or raw.get("order") or raw.get("orderId") or raw.get("uuid") or "")


def _raw_client_order_id(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    for value in (
        raw.get("clientOrderId"),
        raw.get("client_order_id"),
        raw.get("clientOid"),
        info.get("clientOrderId"),
        info.get("client_order_id"),
        info.get("client_oid"),
    ):
        if value:
            return str(value)
    return ""


def _raw_order_status(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("status") or raw.get("state") or "").lower()


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _raw_order_filled(raw: Any) -> float:
    if not isinstance(raw, dict):
        return 0.0
    for key in ("filled", "filled_amount", "executedQty", "executed_amount", "executed_volume"):
        amount = _float_value(raw.get(key))
        if amount > 0:
            return amount
    return 0.0


def _raw_order_cost(raw: Any) -> float:
    if not isinstance(raw, dict):
        return 0.0
    for key in ("cost", "filled_quote", "executed_quote", "cumQuote", "executed_funds"):
        cost = _float_value(raw.get(key))
        if cost > 0:
            return cost
    price = _float_value(raw.get("average") or raw.get("price"))
    filled = _raw_order_filled(raw)
    return price * filled if price > 0 and filled > 0 else 0.0


def _order_identity_set(rows: list[dict[str, Any]]) -> set[str]:
    identities: set[str] = set()
    for row in rows:
        order_id = _raw_order_id(row)
        client_id = _raw_client_order_id(row)
        if order_id:
            identities.add(order_id)
        if client_id:
            identities.add(client_id)
    return identities


def _closed_order_by_identity(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        order_id = _raw_order_id(row)
        client_id = _raw_client_order_id(row)
        if order_id:
            result[order_id] = row
        if client_id:
            result[client_id] = row
    return result


def sync_tracked_grid_orders(
    tracked_orders: list[TrackedGridOrder],
    open_orders: list[dict[str, Any]],
    closed_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    open_identities = _order_identity_set(open_orders)
    closed_by_id = _closed_order_by_identity(closed_orders or [])
    still_open: list[TrackedGridOrder] = []
    confirmed_fills: list[GridFill] = []
    missing_unconfirmed: list[TrackedGridOrder] = []
    for order in tracked_orders:
        identities = {value for value in (order.order_id, order.client_order_id) if value}
        if identities & open_identities:
            still_open.append(order)
            continue
        closed_row = next(
            (closed_by_id[value] for value in identities if value in closed_by_id),
            None,
        )
        if closed_row is None:
            missing_unconfirmed.append(order)
            continue
        filled = _raw_order_filled(closed_row)
        status = _raw_order_status(closed_row)
        if filled > 0 or status in {"closed", "filled", "done"}:
            confirmed_fills.append(
                order.to_fill(amount=filled, cost=_raw_order_cost(closed_row))
            )
        else:
            missing_unconfirmed.append(order)
    return {
        "open_tracked_orders": [order.to_dict() for order in still_open],
        "confirmed_fills": [fill.__dict__ for fill in confirmed_fills],
        "missing_unconfirmed": [order.to_dict() for order in missing_unconfirmed],
        "open_tracked_count": len(still_open),
        "confirmed_fill_count": len(confirmed_fills),
        "missing_unconfirmed_count": len(missing_unconfirmed),
        "tracked_before_count": len(tracked_orders),
        "exchange_open_count": len(open_orders),
    }


def _tracked_from_sync(sync: dict[str, Any], key: str) -> list[TrackedGridOrder]:
    rows: list[TrackedGridOrder] = []
    for item in sync.get(key, []):
        if isinstance(item, dict):
            rows.append(TrackedGridOrder.from_dict(item))
    return rows


def tracked_orders_from_sync(sync: dict[str, Any], key: str) -> list[TrackedGridOrder]:
    return _tracked_from_sync(sync, key)


def _fills_from_sync(sync: dict[str, Any]) -> list[GridFill]:
    fills: list[GridFill] = []
    for item in sync.get("confirmed_fills", []):
        if isinstance(item, dict):
            fills.append(GridFill.from_dict(item))
    return fills


def fills_from_sync(sync: dict[str, Any]) -> list[GridFill]:
    return _fills_from_sync(sync)


async def load_plan_order_book(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    order_book: OrderBookSnapshot | None = None,
) -> OrderBookSnapshot:
    grid_cfg = cfg.spot_grid
    if not grid_cfg.enabled:
        raise ValueError("spot_grid.enabled is false")
    if not grid_cfg.exchange:
        raise ValueError("spot_grid.exchange is required")
    if not grid_cfg.symbol:
        raise ValueError("spot_grid.symbol is required")
    if order_book is not None:
        if order_book.exchange != grid_cfg.exchange or order_book.symbol != grid_cfg.symbol:
            raise ValueError("cached order book does not match spot grid exchange/symbol")
        return order_book
    exchange_cfg = _find_exchange(cfg, grid_cfg.exchange)
    book = await manager.fetch_order_book(
        exchange_cfg,
        grid_cfg.symbol,
        max(cfg.order_book_depth, grid_cfg.grid_count + 1),
    )
    if book is None:
        raise ValueError(f"no order book for {grid_cfg.exchange} {grid_cfg.symbol}")
    return book


def _scaled_market_context(plan: SpotGridPlan, *, quote_rate: float) -> RiskMarketContext:
    return RiskMarketContext(
        exchange=plan.exchange,
        symbol=plan.symbol,
        best_bid=plan.best_bid * quote_rate,
        best_ask=plan.best_ask * quote_rate,
        mid_price=plan.mid_price * quote_rate,
        bid_depth_quote=plan.bid_depth_quote * quote_rate,
        ask_depth_quote=plan.ask_depth_quote * quote_rate,
        max_level_gap_bps=plan.max_level_gap_bps,
        order_book_timestamp_ms=plan.order_book_timestamp_ms,
        order_book_received_at=plan.order_book_received_at,
    )


def _risk_orders(plan: SpotGridPlan, orders: list[GridOrder], *, quote_rate: float) -> list[RiskOrder]:
    return [
        RiskOrder(
            strategy="spot_grid",
            exchange=plan.exchange,
            symbol=plan.symbol,
            side=order.side,
            amount=order.amount,
            price=order.price * quote_rate,
            quote_notional=order.quote_notional * quote_rate,
            distance_bps=order.distance_bps,
        )
        for order in orders
    ]


def _client_order_id(cfg: SpotGridConfig, *, side: Side, level: int, index: int) -> str | None:
    if not cfg.client_order_prefix:
        return None
    timestamp_ms = int(time.time() * 1000)
    return f"{cfg.client_order_prefix}-{side[0]}{level}-{timestamp_ms}-{index}"[:64]


def _validation_error_row(
    exchange_cfg: ExchangeConfig,
    symbol: str,
    order: GridOrder,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "exchange": exchange_cfg.key,
        "symbol": symbol,
        "side": order.side,
        "status": "error",
        "requested_amount": order.amount,
        "requested_price": order.price,
        "amount": None,
        "price": None,
        "cost": order.quote_notional,
        "limits": {},
        "precision": {},
        "errors": [f"{exc.__class__.__name__}: {exc}"],
        "warnings": [],
    }


async def validate_plan_orders(
    cfg: BotConfig,
    manager: ExchangeManager,
    orders: list[GridOrder],
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    exchange_cfg = _find_exchange(cfg, grid_cfg.exchange)
    rows = []
    batch_preparer = getattr(manager, "prepare_limit_orders", None)
    if batch_preparer is not None:
        try:
            rows = await batch_preparer(
                exchange_cfg,
                symbol=grid_cfg.symbol,
                orders=[order.to_dict() for order in orders],
            )
        except Exception as exc:  # noqa: BLE001
            rows = [
                _validation_error_row(exchange_cfg, grid_cfg.symbol, order, exc)
                for order in orders
            ]
    else:
        for order in orders:
            try:
                rows.append(
                    await manager.prepare_limit_order(
                        exchange_cfg,
                        symbol=grid_cfg.symbol,
                        side=order.side,
                        amount=order.amount,
                        price=order.price,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                rows.append(_validation_error_row(exchange_cfg, grid_cfg.symbol, order, exc))
    summary = summarize_order_validations(rows)
    capability_errors = limit_order_capability_errors(
        exchange_cfg,
        post_only=grid_cfg.post_only,
    )
    features = limit_order_features(exchange_cfg)
    warnings = list(summary.get("warnings", []))
    if grid_cfg.client_order_prefix and not features.client_order_id:
        warnings.append(
            f"{exchange_cfg.key} does not support client order ids; grid recovery relies on exchange order ids"
        )
    if capability_errors:
        errors = [*summary.get("errors", []), *capability_errors]
        summary["status"] = "error"
        summary["errors"] = errors
        summary["error_count"] = len(errors)
    if warnings:
        summary["warnings"] = warnings
        summary["warning_count"] = len(warnings)
    summary["exchange_features"] = features.to_dict()
    return summary


def _block_for_validation(payload: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    risk["approved"] = False
    risk["level"] = "blocked"
    risk["reasons"] = [
        *list(risk.get("reasons", [])),
        *[f"order validation: {error}" for error in validation.get("errors", [])],
    ]
    risk["warnings"] = [
        *list(risk.get("warnings", [])),
        *validation.get("warnings", []),
    ]
    payload["risk"] = risk
    payload["status"] = "blocked_by_risk"
    return payload


async def cancel_order_ids(
    cfg: BotConfig,
    manager: ExchangeManager,
    order_ids: list[str],
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    exchange_cfg = _find_exchange(cfg, grid_cfg.exchange)
    canceled = []
    errors = []
    batch_canceler = getattr(manager, "cancel_orders", None)
    if batch_canceler is not None and len(order_ids) > 1:
        try:
            canceled = await batch_canceler(
                exchange_cfg,
                symbol=grid_cfg.symbol,
                order_ids=order_ids,
            )
            return {
                "type": "spot_grid_cancel",
                "strategy": "spot_grid",
                "mode": "live",
                "status": "canceled",
                "exchange": grid_cfg.exchange,
                "symbol": grid_cfg.symbol,
                "order_ids": order_ids,
                "canceled": canceled,
                "canceled_count": len(canceled),
                "errors": [],
                "used_batch_cancel": True,
            }
        except Exception as exc:  # noqa: BLE001
            errors.append({"order_id": "batch", "error": str(exc)})
    for order_id in order_ids:
        try:
            canceled.append(
                await manager.cancel_order(
                    exchange_cfg,
                    symbol=grid_cfg.symbol,
                    order_id=order_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"order_id": order_id, "error": str(exc)})
    return {
        "type": "spot_grid_cancel",
        "strategy": "spot_grid",
        "mode": "live",
        "status": "canceled" if not errors else "cancel_error",
        "exchange": grid_cfg.exchange,
        "symbol": grid_cfg.symbol,
        "order_ids": order_ids,
        "canceled": canceled,
        "canceled_count": len(canceled),
        "errors": errors,
        "used_batch_cancel": False,
    }


async def _confirm_remaining_order_ids(
    manager: ExchangeManager,
    exchange_cfg: ExchangeConfig,
    *,
    symbol: str,
) -> tuple[list[str], str | None]:
    try:
        remaining_open_orders = await manager.fetch_open_orders(exchange_cfg, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        return [], f"{exc.__class__.__name__}: {exc}"
    return [
        _raw_order_id(order)
        for order in remaining_open_orders
        if _raw_order_id(order)
    ], None


async def place_grid_orders(
    cfg: BotConfig,
    manager: ExchangeManager,
    orders: list[GridOrder],
    *,
    replace_order_ids: list[str] | None = None,
    prepared_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    grid_cfg = cfg.spot_grid
    exchange_cfg = _find_exchange(cfg, grid_cfg.exchange)
    replace_order_ids = [order_id for order_id in (replace_order_ids or []) if order_id]
    canceled: list[dict[str, Any]] = []
    cancel_errors: list[dict[str, Any]] = []
    if replace_order_ids:
        cancel_payload = await cancel_order_ids(cfg, manager, replace_order_ids)
        canceled = cancel_payload["canceled"]
        cancel_errors = cancel_payload["errors"]
        remaining_ids, confirmation_error = await _confirm_remaining_order_ids(
            manager,
            exchange_cfg,
            symbol=grid_cfg.symbol,
        )
        remaining_set = set(remaining_ids)
        remaining_tracked_ids = [
            order_id for order_id in replace_order_ids if order_id in remaining_set
        ]
        if confirmation_error:
            cancel_errors.append(
                {
                    "order_id": "open_order_confirmation",
                    "error": confirmation_error,
                }
            )
        if confirmation_error or remaining_tracked_ids:
            return {
                "canceled_count": len(canceled),
                "cancel_errors": cancel_errors,
                "placed_count": 0,
                "placed_order_ids": [],
                "placed_orders": [],
                "cancel_retry_required": True,
                "remaining_open_order_ids": remaining_tracked_ids or remaining_ids,
                "reason": "tracked grid orders must be fully canceled before rebuilding",
            }

    placed: list[dict[str, Any]] = []
    placed_orders: list[dict[str, Any]] = []
    create_errors: list[dict[str, Any]] = []
    prepared_orders = prepared_orders or []
    client_order_ids = [
        _client_order_id(grid_cfg, side=order.side, level=order.level, index=index)
        for index, order in enumerate(orders, start=1)
    ]
    batch_creator = getattr(manager, "create_prepared_limit_orders", None)
    if (
        batch_creator is not None
        and len(orders) > 1
        and len(prepared_orders) == len(orders)
    ):
        try:
            raw_result = await batch_creator(
                exchange_cfg,
                symbol=grid_cfg.symbol,
                sides=[order.side for order in orders],
                prepared_orders=prepared_orders,
                post_only=grid_cfg.post_only,
                client_order_ids=client_order_ids,
            )
            placed = raw_result if isinstance(raw_result, list) else [raw_result]
            placed_orders = _tracked_payloads_from_results(
                orders,
                placed,
                client_order_ids,
                exchange=grid_cfg.exchange,
                symbol=grid_cfg.symbol,
            )
            return {
                "canceled_count": len(canceled),
                "cancel_errors": cancel_errors,
                "placed_count": len(placed_orders),
                "placed_order_ids": [row["order_id"] for row in placed_orders if row["order_id"]],
                "placed_orders": placed_orders,
                "used_batch_create": True,
            }
        except NotImplementedError:
            pass
        except Exception as exc:  # noqa: BLE001
            remaining_ids, confirmation_error = await _confirm_remaining_order_ids(
                manager,
                exchange_cfg,
                symbol=grid_cfg.symbol,
            )
            errors = [{"scope": "batch", "error": f"{exc.__class__.__name__}: {exc}"}]
            if confirmation_error:
                errors.append({"scope": "post_batch_create_open_orders", "error": confirmation_error})
            return {
                "canceled_count": len(canceled),
                "cancel_errors": cancel_errors,
                "placed_count": 0,
                "placed_order_ids": [],
                "placed_orders": [],
                "create_errors": errors,
                "create_result_uncertain": True,
                "remaining_open_order_ids": remaining_ids,
                "manual_intervention_required": bool(remaining_ids or confirmation_error),
                "used_batch_create": True,
            }

    prepared_creator = getattr(manager, "create_prepared_limit_order", None)
    for index, order in enumerate(orders, start=1):
        client_order_id = client_order_ids[index - 1]
        prepared = prepared_orders[index - 1] if index <= len(prepared_orders) else None
        try:
            if prepared is not None and prepared_creator is not None:
                raw = await prepared_creator(
                    exchange_cfg,
                    symbol=grid_cfg.symbol,
                    side=order.side,
                    prepared=prepared,
                    post_only=grid_cfg.post_only,
                    client_order_id=client_order_id,
                )
            else:
                raw = await manager.create_limit_order(
                    exchange_cfg,
                    symbol=grid_cfg.symbol,
                    side=order.side,
                    amount=order.amount,
                    price=order.price,
                    post_only=grid_cfg.post_only,
                    client_order_id=client_order_id,
                )
            placed.append(raw)
        except Exception as exc:  # noqa: BLE001
            create_errors.append(
                {
                    "scope": "order",
                    "index": index,
                    "side": order.side,
                    "level": order.level,
                    "price": order.price,
                    "amount": order.amount,
                    "client_order_id": client_order_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            break
    placed_orders = _tracked_payloads_from_results(
        orders,
        placed,
        client_order_ids,
        exchange=grid_cfg.exchange,
        symbol=grid_cfg.symbol,
    )
    emergency_canceled: list[dict[str, Any]] = []
    emergency_cancel_errors: list[dict[str, Any]] = []
    partial_create = bool(create_errors and placed_orders)
    if partial_create:
        for row in placed_orders:
            order_id = row.get("order_id")
            if not order_id:
                continue
            try:
                emergency_canceled.append(
                    await manager.cancel_order(
                        exchange_cfg,
                        symbol=grid_cfg.symbol,
                        order_id=order_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                emergency_cancel_errors.append(
                    {"order_id": order_id, "error": f"{exc.__class__.__name__}: {exc}"}
                )
    remaining_ids: list[str] = []
    if partial_create:
        remaining_ids, confirmation_error = await _confirm_remaining_order_ids(
            manager,
            exchange_cfg,
            symbol=grid_cfg.symbol,
        )
        if confirmation_error:
            emergency_cancel_errors.append(
                {"order_id": "open_order_confirmation", "error": confirmation_error}
            )
    return {
        "canceled_count": len(canceled) + len(emergency_canceled),
        "cancel_errors": cancel_errors,
        "placed_count": len(placed_orders),
        "placed_order_ids": [row["order_id"] for row in placed_orders if row["order_id"]],
        "placed_orders": placed_orders,
        "create_errors": create_errors,
        "partial_create": partial_create,
        "emergency_cancel": partial_create,
        "emergency_canceled_count": len(emergency_canceled),
        "emergency_cancel_errors": emergency_cancel_errors,
        "remaining_open_order_ids": remaining_ids,
        "manual_intervention_required": bool(emergency_cancel_errors or remaining_ids),
        "used_batch_create": False,
    }


def _tracked_payloads_from_results(
    orders: list[GridOrder],
    results: list[Any],
    client_order_ids: list[str | None],
    *,
    exchange: str,
    symbol: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    created_at = time.time()
    for order, raw, client_order_id in zip(orders, results, client_order_ids):
        order_id = _raw_order_id(raw)
        observed_client_id = _raw_client_order_id(raw) or str(client_order_id or "")
        rows.append(
            TrackedGridOrder.from_grid_order(
                order,
                order_id=order_id,
                client_order_id=observed_client_id,
                exchange=exchange,
                symbol=symbol,
                created_at=created_at,
            ).to_dict()
        )
    return rows


async def run_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    live: bool,
    tracked_orders: list[TrackedGridOrder] | None = None,
    replacement_fills: list[GridFill] | None = None,
    replace_order_ids: list[str] | None = None,
    previous_mid_price: float | None = None,
    last_cancel_at: float | None = None,
    order_book: OrderBookSnapshot | None = None,
) -> dict[str, Any]:
    book = await load_plan_order_book(cfg, manager, order_book=order_book)
    plan = build_spot_grid_plan(book, cfg.spot_grid)
    replacement_plan = (
        build_spot_grid_fill_replacement_plan(cfg.spot_grid, replacement_fills)
        if replacement_fills
        else None
    )
    orders = (
        list(replacement_plan.replacements)
        if replacement_plan is not None
        else list(plan.orders)
    )
    action = "replace_filled_orders" if replacement_plan else "place_grid"
    payload: dict[str, Any] = {
        "type": "spot_grid",
        "strategy": "spot_grid",
        "mode": "live" if live else "dry_run",
        "status": "planned",
        "action": action,
        "plan": plan.to_dict(),
        "orders_to_place": [order.to_dict() for order in orders],
        "market_data": order_book_market_data(book),
    }
    if replacement_plan is not None:
        payload["replacement_plan"] = replacement_plan.to_dict()
    if plan.status != "planned":
        payload["status"] = plan.status
        payload["reason"] = plan.reason
        payload["orders_to_place"] = []
        return payload
    if replacement_plan is not None and replacement_plan.status != "planned":
        payload["status"] = replacement_plan.status
        payload["reason"] = replacement_plan.reason
        payload["orders_to_place"] = []
        return payload
    if not orders:
        payload["status"] = "unchanged"
        payload["reason"] = "no grid orders are due"
        return payload

    conversion = market_maker_quote_conversion(cfg, plan.symbol)
    payload["quote_conversion"] = conversion
    quote_rate = conversion.get("quote_to_common_rate")
    quote_rate_for_risk = float(quote_rate) if quote_rate is not None else 1.0
    exchange_cfg = _find_exchange(cfg, cfg.spot_grid.exchange)
    existing_open_order_count: int | None = None
    open_order_error: str | None = None
    if live and (
        cfg.risk.max_open_orders > 0
        or cfg.risk.max_cancels_per_cycle > 0
        or cfg.risk.min_seconds_between_cancels > 0
    ):
        try:
            existing_open_order_count = len(
                await manager.fetch_open_orders(exchange_cfg, symbol=cfg.spot_grid.symbol)
            )
        except Exception as exc:  # noqa: BLE001
            open_order_error = str(exc)
    replace_order_ids = [order_id for order_id in (replace_order_ids or []) if order_id]
    market = _scaled_market_context(plan, quote_rate=quote_rate_for_risk)
    risk = evaluate_order_batch(
        cfg.risk,
        _risk_orders(plan, orders, quote_rate=quote_rate_for_risk),
        strategy="spot_grid",
        live=live,
        existing_spread_bps=(plan.best_ask - plan.best_bid) / plan.mid_price * 10_000,
        plan_observed_at=plan.observed_at,
        market=market,
        previous_mid_price=previous_mid_price,
        current_positions_base=portfolio_positions_base(cfg.portfolio),
        daily_pnl_quote=current_daily_pnl_quote(cfg),
        existing_open_order_count=existing_open_order_count,
        expected_cancel_count=len(replace_order_ids),
        last_cancel_at=last_cancel_at,
        open_order_error=open_order_error,
        post_only=cfg.spot_grid.post_only,
    )
    payload["risk"] = risk.to_dict()
    payload["risk"]["currency"] = cfg.common_quote_currency
    payload["risk"]["quote_conversion"] = conversion
    if quote_rate is None:
        payload["risk"]["approved"] = False
        payload["risk"]["level"] = "blocked"
        payload["risk"]["reasons"] = [
            *list(payload["risk"].get("reasons", [])),
            (
                f"missing quote rate for {conversion['quote_currency']} -> "
                f"{cfg.common_quote_currency}"
            ),
        ]
    if not live:
        return payload
    if not payload["risk"]["approved"]:
        payload["status"] = "blocked_by_risk"
        return payload
    validation = await validate_plan_orders(cfg, manager, orders)
    payload["order_validation"] = validation
    if validation["status"] != "ok":
        return _block_for_validation(payload, validation)
    execution = await place_grid_orders(
        cfg,
        manager,
        orders,
        replace_order_ids=replace_order_ids,
        prepared_orders=validation.get("orders"),
    )
    payload["execution"] = execution
    if execution.get("cancel_retry_required"):
        payload["status"] = "cancel_retry"
    else:
        payload["status"] = "execution_error" if execution.get("create_errors") else "placed"
    return payload
