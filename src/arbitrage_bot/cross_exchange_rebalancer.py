from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .config import (
    BotConfig,
    CrossExchangeRebalanceConfig,
    ExchangeConfig,
)
from .exchanges import ExchangeManager
from .models import Opportunity, OpportunityLeg, OrderBookSnapshot
from .orderbook import available_base, estimate_fill, max_base_for_quote
from .spot_arbitrage_executor import run_spot_arbitrage_execution_cycle


STRATEGY_ID = "cross_exchange_rebalance"
EVENT_TYPE = "cross_exchange_rebalance_execution"


def _base_currency(symbol: str) -> str:
    return str(symbol or "").split("/", 1)[0].upper()


def _quote_currency(symbol: str) -> str:
    if "/" not in str(symbol or ""):
        return ""
    return str(symbol).split("/", 1)[1].split(":", 1)[0].upper()


def _exchange(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in cfg.spot_exchanges:
        if exchange.key == key:
            return exchange
    raise ValueError(f"spot exchange account is not configured: {key}")


def _quote_rate(
    quote_rates: dict[str, float],
    currency: str,
) -> float:
    rate = quote_rates.get(currency.upper())
    if rate is None or float(rate) <= 0:
        raise ValueError(f"missing quote rate for {currency} to common quote")
    return float(rate)


@dataclass(frozen=True)
class CrossExchangeRebalancePlan:
    base_asset: str
    common_quote_currency: str
    buy_exchange: str
    buy_symbol: str
    buy_quote_currency: str
    buy_quote_rate: float
    sell_exchange: str
    sell_symbol: str
    sell_quote_currency: str
    sell_quote_rate: float
    quantity_base: float
    buy_average_price: float
    buy_fee_quote: float
    buy_cost_local: float
    buy_cost_common: float
    sell_average_price: float
    sell_fee_quote: float
    sell_proceeds_local: float
    sell_proceeds_common: float
    expected_cost_common: float
    expected_cost_bps: float
    target_quote_common: float
    completed_quote_common: float
    remaining_quote_common: float
    cycle_quote_common: float
    expected_progress_quote_common: float
    observed_at: float
    buy_book_received_at: float
    sell_book_received_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def opportunity(self) -> Opportunity:
        return Opportunity(
            strategy=STRATEGY_ID,
            profit_quote=-self.expected_cost_common,
            profit_bps=-self.expected_cost_bps,
            observed_at=self.observed_at,
            legs=[
                OpportunityLeg(
                    exchange=self.buy_exchange,
                    symbol=self.buy_symbol,
                    side="buy",
                    quantity_base=self.quantity_base,
                    average_price=self.buy_average_price,
                    fee_quote=self.buy_fee_quote,
                    quote_currency=self.buy_quote_currency,
                    gross_quote=self.buy_cost_local - self.buy_fee_quote,
                    net_quote=self.buy_cost_local,
                    common_quote_rate=self.buy_quote_rate,
                ),
                OpportunityLeg(
                    exchange=self.sell_exchange,
                    symbol=self.sell_symbol,
                    side="sell",
                    quantity_base=self.quantity_base,
                    average_price=self.sell_average_price,
                    fee_quote=self.sell_fee_quote,
                    quote_currency=self.sell_quote_currency,
                    gross_quote=self.sell_proceeds_local + self.sell_fee_quote,
                    net_quote=self.sell_proceeds_local,
                    common_quote_rate=self.sell_quote_rate,
                ),
            ],
            metadata={
                "asset": self.base_asset,
                "common_quote_currency": self.common_quote_currency,
                "purpose": "synthetic_cross_exchange_inventory_transfer",
                "cash_source_exchange": self.buy_exchange,
                "cash_destination_exchange": self.sell_exchange,
                "buy_common_quote": self.buy_cost_common,
                "sell_common_quote": self.sell_proceeds_common,
                "expected_cost_common": self.expected_cost_common,
                "expected_cost_bps": self.expected_cost_bps,
                "requires_prefunded_inventory": True,
                "requires_fx_reconciliation": True,
            },
        )


def build_cross_exchange_rebalance_plan(
    cfg: BotConfig,
    *,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    completed_quote_common: float = 0.0,
) -> CrossExchangeRebalancePlan:
    rebalance = cfg.cross_exchange_rebalance
    if not rebalance.enabled:
        raise ValueError("cross_exchange_rebalance.enabled is false")
    if rebalance.buy_exchange == rebalance.sell_exchange:
        raise ValueError("buy and sell exchange accounts must be different")
    buy_base = _base_currency(rebalance.buy_symbol)
    sell_base = _base_currency(rebalance.sell_symbol)
    if not buy_base or buy_base != sell_base:
        raise ValueError("buy and sell symbols must use the same base asset")
    buy_exchange = _exchange(cfg, rebalance.buy_exchange)
    sell_exchange = _exchange(cfg, rebalance.sell_exchange)
    buy_book = books.get((rebalance.buy_exchange, rebalance.buy_symbol))
    sell_book = books.get((rebalance.sell_exchange, rebalance.sell_symbol))
    if buy_book is None or not buy_book.asks:
        raise ValueError(
            f"buy order book is unavailable: {rebalance.buy_exchange} "
            f"{rebalance.buy_symbol}"
        )
    if sell_book is None or not sell_book.bids:
        raise ValueError(
            f"sell order book is unavailable: {rebalance.sell_exchange} "
            f"{rebalance.sell_symbol}"
        )

    target = max(0.0, float(rebalance.total_quote_common))
    completed = max(0.0, float(completed_quote_common))
    remaining = max(0.0, target - completed)
    if target <= 0:
        raise ValueError("total_quote_common must be positive")
    if remaining <= max(target * 1e-12, 1e-9):
        raise ValueError("rebalance target is complete")
    cycle_quote = min(
        remaining,
        max(0.0, float(rebalance.quote_per_cycle_common)),
    )
    if cycle_quote <= 0:
        raise ValueError("quote_per_cycle_common must be positive")

    buy_quote = _quote_currency(rebalance.buy_symbol)
    sell_quote = _quote_currency(rebalance.sell_symbol)
    buy_rate = _quote_rate(quote_rates, buy_quote)
    sell_rate = _quote_rate(quote_rates, sell_quote)
    buy_fee_multiplier = 1.0 + max(0.0, buy_exchange.fee_bps) / 10_000
    sell_fee_multiplier = 1.0 - max(0.0, sell_exchange.fee_bps) / 10_000
    if sell_fee_multiplier <= 0:
        raise ValueError("sell exchange fee must be below 10000 bps")

    buy_gross_budget_local = cycle_quote / buy_rate / buy_fee_multiplier
    sell_gross_target_local = cycle_quote / sell_rate / sell_fee_multiplier
    quantity_base = min(
        max_base_for_quote(buy_book.asks, buy_gross_budget_local),
        max_base_for_quote(sell_book.bids, sell_gross_target_local),
        available_base(sell_book.bids),
    )
    if quantity_base <= 0:
        raise ValueError("order book depth cannot satisfy the rebalance cycle")

    buy_fill = estimate_fill(
        buy_book.asks,
        side="buy",
        quantity_base=quantity_base,
        fee_bps=buy_exchange.fee_bps,
    )
    sell_fill = estimate_fill(
        sell_book.bids,
        side="sell",
        quantity_base=quantity_base,
        fee_bps=sell_exchange.fee_bps,
    )
    if buy_fill is None or sell_fill is None:
        raise ValueError("order book depth changed while building the rebalance plan")

    buy_common = buy_fill.net_quote * buy_rate
    sell_common = sell_fill.net_quote * sell_rate
    expected_cost = buy_common - sell_common
    expected_cost_bps = expected_cost / buy_common * 10_000 if buy_common > 0 else 0.0
    return CrossExchangeRebalancePlan(
        base_asset=buy_base,
        common_quote_currency=cfg.common_quote_currency,
        buy_exchange=rebalance.buy_exchange,
        buy_symbol=rebalance.buy_symbol,
        buy_quote_currency=buy_quote,
        buy_quote_rate=buy_rate,
        sell_exchange=rebalance.sell_exchange,
        sell_symbol=rebalance.sell_symbol,
        sell_quote_currency=sell_quote,
        sell_quote_rate=sell_rate,
        quantity_base=quantity_base,
        buy_average_price=buy_fill.average_price,
        buy_fee_quote=buy_fill.fee_quote,
        buy_cost_local=buy_fill.net_quote,
        buy_cost_common=buy_common,
        sell_average_price=sell_fill.average_price,
        sell_fee_quote=sell_fill.fee_quote,
        sell_proceeds_local=sell_fill.net_quote,
        sell_proceeds_common=sell_common,
        expected_cost_common=expected_cost,
        expected_cost_bps=expected_cost_bps,
        target_quote_common=target,
        completed_quote_common=completed,
        remaining_quote_common=remaining,
        cycle_quote_common=cycle_quote,
        expected_progress_quote_common=min(remaining, buy_common),
        observed_at=min(buy_book.received_at, sell_book.received_at),
        buy_book_received_at=buy_book.received_at,
        sell_book_received_at=sell_book.received_at,
    )


def _plan_for_aligned_quantity(
    cfg: BotConfig,
    plan: CrossExchangeRebalancePlan,
    *,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quantity_base: float,
) -> CrossExchangeRebalancePlan:
    buy_book = books.get((plan.buy_exchange, plan.buy_symbol))
    sell_book = books.get((plan.sell_exchange, plan.sell_symbol))
    if buy_book is None or sell_book is None:
        raise ValueError("order books are unavailable for precision alignment")
    buy_fill = estimate_fill(
        buy_book.asks,
        side="buy",
        quantity_base=quantity_base,
        fee_bps=_exchange(cfg, plan.buy_exchange).fee_bps,
    )
    sell_fill = estimate_fill(
        sell_book.bids,
        side="sell",
        quantity_base=quantity_base,
        fee_bps=_exchange(cfg, plan.sell_exchange).fee_bps,
    )
    if buy_fill is None or sell_fill is None:
        raise ValueError("aligned quantity is unavailable in the current order books")
    buy_common = buy_fill.net_quote * plan.buy_quote_rate
    sell_common = sell_fill.net_quote * plan.sell_quote_rate
    expected_cost = buy_common - sell_common
    expected_cost_bps = expected_cost / buy_common * 10_000 if buy_common > 0 else 0.0
    return replace(
        plan,
        quantity_base=quantity_base,
        buy_average_price=buy_fill.average_price,
        buy_fee_quote=buy_fill.fee_quote,
        buy_cost_local=buy_fill.net_quote,
        buy_cost_common=buy_common,
        sell_average_price=sell_fill.average_price,
        sell_fee_quote=sell_fill.fee_quote,
        sell_proceeds_local=sell_fill.net_quote,
        sell_proceeds_common=sell_common,
        expected_cost_common=expected_cost,
        expected_cost_bps=expected_cost_bps,
        expected_progress_quote_common=min(
            plan.remaining_quote_common,
            plan.expected_progress_quote_common,
        ),
    )


async def _align_plan_quantity(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: CrossExchangeRebalancePlan,
    *,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> tuple[CrossExchangeRebalancePlan, dict[str, Any]]:
    buy_exchange = _exchange(cfg, plan.buy_exchange)
    sell_exchange = _exchange(cfg, plan.sell_exchange)
    requested = plan.quantity_base
    candidate = requested
    attempts = []
    final_validations: list[dict[str, Any]] = []
    for attempt in range(1, 5):
        candidate_plan = _plan_for_aligned_quantity(
            cfg,
            plan,
            books=books,
            quantity_base=candidate,
        )
        buy_validation, sell_validation = await asyncio.gather(
            manager.prepare_limit_order(
                buy_exchange,
                symbol=plan.buy_symbol,
                side="buy",
                amount=candidate,
                price=candidate_plan.buy_average_price,
            ),
            manager.prepare_limit_order(
                sell_exchange,
                symbol=plan.sell_symbol,
                side="sell",
                amount=candidate,
                price=candidate_plan.sell_average_price,
            ),
        )
        final_validations = [buy_validation, sell_validation]
        errors = [
            f"{row.get('exchange')} {row.get('symbol')}: {error}"
            for row in final_validations
            for error in row.get("errors", [])
        ]
        if errors:
            raise ValueError("; ".join(errors))
        prepared_amounts = [
            max(0.0, float(row.get("amount") or 0.0))
            for row in final_validations
        ]
        aligned = min(candidate, *prepared_amounts)
        attempts.append(
            {
                "attempt": attempt,
                "requested_quantity_base": candidate,
                "prepared_quantities_base": prepared_amounts,
                "aligned_quantity_base": aligned,
            }
        )
        if aligned <= 0:
            raise ValueError("precision-aligned base quantity is zero")
        tolerance = max(abs(candidate), abs(aligned), 1.0) * 1e-12
        if (
            abs(prepared_amounts[0] - prepared_amounts[1]) <= tolerance
            and abs(candidate - aligned) <= tolerance
        ):
            aligned_plan = _plan_for_aligned_quantity(
                cfg,
                plan,
                books=books,
                quantity_base=aligned,
            )
            return aligned_plan, {
                "status": "aligned",
                "requested_quantity_base": requested,
                "aligned_quantity_base": aligned,
                "reduction_base": max(0.0, requested - aligned),
                "attempt_count": attempt,
                "attempts": attempts,
                "validations": final_validations,
            }
        candidate = aligned
    raise ValueError("could not align both exchange base quantities after 4 attempts")


def _raw_order_side(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    return str(raw.get("side") or info.get("side") or "").lower()


def _raw_order_id(raw: dict[str, Any]) -> str:
    return str(raw.get("id") or raw.get("order") or "")


async def _conflicting_open_order_guard(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: CrossExchangeRebalancePlan,
) -> dict[str, Any]:
    if not cfg.cross_exchange_rebalance.block_conflicting_open_orders:
        return {"enabled": False, "blocked": False, "orders": [], "errors": []}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for exchange_key, symbol, conflicting_side in (
        (plan.buy_exchange, plan.buy_symbol, "sell"),
        (plan.sell_exchange, plan.sell_symbol, "buy"),
    ):
        try:
            open_orders = await manager.fetch_open_orders(
                _exchange(cfg, exchange_key),
                symbol=symbol,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{exchange_key} {symbol}: {exc.__class__.__name__}: {exc}")
            continue
        for raw in open_orders:
            if not isinstance(raw, dict):
                continue
            side = _raw_order_side(raw)
            if side and side != conflicting_side:
                continue
            rows.append(
                {
                    "exchange": exchange_key,
                    "symbol": symbol,
                    "side": side or "unknown",
                    "order_id": _raw_order_id(raw),
                }
            )
    reasons = []
    if errors:
        reasons.append("could not verify existing open orders on both routes")
    if rows:
        reasons.append(
            f"found {len(rows)} opposite-side open order(s); self-trade risk"
        )
    return {
        "enabled": True,
        "blocked": bool(reasons),
        "orders": rows,
        "errors": errors,
        "reasons": reasons,
    }


def _balance_free(balance: dict[str, Any], currency: str) -> float:
    row = balance.get(currency.upper())
    if not isinstance(row, dict):
        return 0.0
    try:
        return max(0.0, float(row.get("free") or 0.0))
    except (TypeError, ValueError):
        return 0.0


async def _balance_guard(
    cfg: BotConfig,
    manager: ExchangeManager,
    plan: CrossExchangeRebalancePlan,
) -> dict[str, Any]:
    buy_balance = await manager.fetch_balance(_exchange(cfg, plan.buy_exchange))
    sell_balance = await manager.fetch_balance(_exchange(cfg, plan.sell_exchange))
    buy_free = _balance_free(buy_balance, plan.buy_quote_currency)
    sell_free = _balance_free(sell_balance, plan.base_asset)
    buy_required = plan.buy_cost_local + cfg.cross_exchange_rebalance.buy_quote_reserve
    sell_required = plan.quantity_base + cfg.cross_exchange_rebalance.sell_base_reserve
    reasons = []
    if buy_free + 1e-12 < buy_required:
        reasons.append(
            f"{plan.buy_exchange} {plan.buy_quote_currency} free "
            f"{buy_free:.12g} is below required plus reserve {buy_required:.12g}"
        )
    if sell_free + 1e-12 < sell_required:
        reasons.append(
            f"{plan.sell_exchange} {plan.base_asset} free {sell_free:.12g} "
            f"is below required plus reserve {sell_required:.12g}"
        )
    return {
        "approved": not reasons,
        "reasons": reasons,
        "buy": {
            "exchange": plan.buy_exchange,
            "currency": plan.buy_quote_currency,
            "free": buy_free,
            "order_required": plan.buy_cost_local,
            "reserve": cfg.cross_exchange_rebalance.buy_quote_reserve,
            "required_with_reserve": buy_required,
        },
        "sell": {
            "exchange": plan.sell_exchange,
            "currency": plan.base_asset,
            "free": sell_free,
            "order_required": plan.quantity_base,
            "reserve": cfg.cross_exchange_rebalance.sell_base_reserve,
            "required_with_reserve": sell_required,
        },
    }


def _risk_config_for_rebalance(cfg: BotConfig) -> BotConfig:
    rebalance = cfg.cross_exchange_rebalance
    existing = dict(cfg.risk.strategy_overrides.get(STRATEGY_ID, {}))
    if rebalance.max_slippage_bps > 0:
        existing["max_slippage_bps"] = rebalance.max_slippage_bps
    overrides = dict(cfg.risk.strategy_overrides)
    overrides[STRATEGY_ID] = existing
    return replace(cfg, risk=replace(cfg.risk, strategy_overrides=overrides))


def _execution_progress(
    plan: CrossExchangeRebalancePlan,
    execution: dict[str, Any],
) -> dict[str, Any]:
    fill_status = (
        execution.get("fill_status")
        if isinstance(execution.get("fill_status"), dict)
        else {}
    )
    buy_filled = max(0.0, float(fill_status.get("buy_filled_base") or 0.0))
    sell_filled = max(0.0, float(fill_status.get("sell_filled_base") or 0.0))
    matched_base = min(buy_filled, sell_filled)
    fill_ratio = (
        min(1.0, matched_base / plan.quantity_base)
        if plan.quantity_base > 0
        else 0.0
    )
    sell_quote_local = 0.0
    for row in fill_status.get("orders", []):
        if isinstance(row, dict) and row.get("side") == "sell":
            sell_quote_local += max(0.0, float(row.get("filled_quote") or 0.0))
    if sell_quote_local <= 0 and matched_base > 0:
        sell_quote_local = matched_base * plan.sell_average_price
    destination_quote_common = min(
        plan.remaining_quote_common,
        sell_quote_local * plan.sell_quote_rate,
    )
    source_quote_local = plan.buy_cost_local * fill_ratio
    progress_quote = min(
        plan.remaining_quote_common,
        plan.expected_progress_quote_common * fill_ratio,
    )
    balanced = not bool(fill_status.get("hedge_required"))
    if not balanced:
        progress_quote = 0.0
        matched_base = 0.0
        source_quote_local = 0.0
        destination_quote_common = 0.0
    return {
        "balanced": balanced,
        "matched_base": matched_base,
        "fill_ratio": fill_ratio if balanced else 0.0,
        "source_quote_local": source_quote_local,
        "source_progress_quote_common": progress_quote,
        "destination_quote_local": sell_quote_local if balanced else 0.0,
        "destination_quote_common": destination_quote_common,
        "progress_quote_common": progress_quote,
        "hedge_required": bool(fill_status.get("hedge_required")),
        "hedge_side": fill_status.get("hedge_side"),
        "hedge_base": fill_status.get("hedge_base"),
    }


async def run_cross_exchange_rebalance_cycle(
    cfg: BotConfig,
    manager: ExchangeManager,
    *,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    completed_quote_common: float = 0.0,
    live: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": EVENT_TYPE,
        "strategy": STRATEGY_ID,
        "mode": "live" if live else "dry_run",
        "status": "planned",
        "config": asdict(cfg.cross_exchange_rebalance),
        "observed_at": time.time(),
    }
    if not cfg.cross_exchange_rebalance.enabled:
        payload["status"] = "disabled"
        return payload
    target = max(0.0, cfg.cross_exchange_rebalance.total_quote_common)
    if completed_quote_common >= target > 0:
        payload.update(
            {
                "status": "complete",
                "progress": {
                    "target_quote_common": target,
                    "completed_quote_common": completed_quote_common,
                    "remaining_quote_common": 0.0,
                    "progress_pct": 100.0,
                },
            }
        )
        return payload
    try:
        plan = build_cross_exchange_rebalance_plan(
            cfg,
            books=books,
            quote_rates=quote_rates,
            completed_quote_common=completed_quote_common,
        )
    except ValueError as exc:
        payload["status"] = "blocked_by_plan"
        payload["errors"] = [str(exc)]
        return payload

    payload["plan"] = plan.to_dict()
    payload["progress"] = {
        "target_quote_common": plan.target_quote_common,
        "completed_quote_common": plan.completed_quote_common,
        "remaining_quote_common": plan.remaining_quote_common,
        "progress_pct": min(
            100.0,
            plan.completed_quote_common / plan.target_quote_common * 100,
        ),
        "expected_cycle_quote_common": plan.expected_progress_quote_common,
    }
    max_cost = max(0.0, cfg.cross_exchange_rebalance.max_cost_bps)
    if max_cost > 0 and plan.expected_cost_bps > max_cost:
        payload["status"] = "waiting_for_cost"
        payload["risk"] = {
            "approved": False,
            "level": "blocked",
            "reasons": [
                f"expected rebalance cost {plan.expected_cost_bps:.2f} bps "
                f"exceeds max_cost_bps {max_cost:.2f}"
            ],
        }
        return payload

    opportunity = plan.opportunity()
    payload["opportunity"] = opportunity.to_dict()
    payload["paper_execution"] = {
        "status": "estimated",
        "orders": [leg.to_dict() for leg in opportunity.legs],
        "expected_cost_common": plan.expected_cost_common,
        "expected_cost_bps": plan.expected_cost_bps,
        "expected_progress_quote_common": plan.expected_progress_quote_common,
    }
    if not live:
        return payload

    if not cfg.cross_exchange_rebalance.live_enabled:
        payload["status"] = "dry_run"
        return payload
    if not cfg.risk.strategy_enabled.get(STRATEGY_ID, False):
        payload["status"] = "blocked_by_risk"
        payload["risk"] = {
            "approved": False,
            "level": "blocked",
            "reasons": [f"risk.strategy_enabled.{STRATEGY_ID} is not explicitly true"],
        }
        return payload

    try:
        plan, precision_alignment = await _align_plan_quantity(
            cfg,
            manager,
            plan,
            books=books,
        )
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "blocked_by_validation"
        payload["order_validation"] = {
            "status": "error",
            "errors": [f"{exc.__class__.__name__}: {exc}"],
        }
        return payload
    payload["precision_alignment"] = precision_alignment
    payload["plan"] = plan.to_dict()
    payload["progress"]["expected_cycle_quote_common"] = (
        plan.expected_progress_quote_common
    )
    if max_cost > 0 and plan.expected_cost_bps > max_cost:
        payload["status"] = "waiting_for_cost"
        payload["risk"] = {
            "approved": False,
            "level": "blocked",
            "reasons": [
                f"precision-aligned rebalance cost {plan.expected_cost_bps:.2f} bps "
                f"exceeds max_cost_bps {max_cost:.2f}"
            ],
        }
        return payload
    opportunity = plan.opportunity()
    payload["opportunity"] = opportunity.to_dict()
    payload["paper_execution"] = {
        "status": "estimated",
        "orders": [leg.to_dict() for leg in opportunity.legs],
        "expected_cost_common": plan.expected_cost_common,
        "expected_cost_bps": plan.expected_cost_bps,
        "expected_progress_quote_common": plan.expected_progress_quote_common,
    }

    conflict_guard = await _conflicting_open_order_guard(cfg, manager, plan)
    payload["conflict_guard"] = conflict_guard
    if conflict_guard["blocked"]:
        payload["status"] = "blocked_by_conflict"
        payload["risk"] = {
            "approved": False,
            "level": "blocked",
            "reasons": [
                *conflict_guard.get("reasons", []),
                *conflict_guard.get("errors", []),
            ],
        }
        return payload

    try:
        balance_guard = await _balance_guard(cfg, manager, plan)
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "blocked_by_balance"
        payload["balance_guard"] = {
            "approved": False,
            "reasons": [f"{exc.__class__.__name__}: {exc}"],
        }
        return payload
    payload["balance_guard"] = balance_guard
    if not balance_guard["approved"]:
        payload["status"] = "blocked_by_balance"
        return payload

    execution_payload = await run_spot_arbitrage_execution_cycle(
        _risk_config_for_rebalance(cfg),
        manager,
        opportunities=[opportunity],
        books=books,
        quote_rates=quote_rates,
        live=True,
        order_ttl_seconds=cfg.cross_exchange_rebalance.order_ttl_seconds,
        strategy_id=STRATEGY_ID,
        event_type=EVENT_TYPE,
        client_order_prefix=cfg.cross_exchange_rebalance.client_order_prefix,
        execution_mode="buy_then_sell",
    )
    for key in (
        "risk",
        "order_validation",
        "execution",
        "protection",
        "timing",
        "paper_vs_live",
        "errors",
    ):
        if key in execution_payload:
            payload[key] = execution_payload[key]
    payload["status"] = execution_payload.get("status", "execution_error")
    execution = (
        payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    )
    progress = _execution_progress(plan, execution)
    payload["execution_progress"] = progress
    halt_required = bool(
        progress["hedge_required"]
        or execution.get("manual_intervention_required")
        or (
            cfg.cross_exchange_rebalance.halt_on_error
            and payload["status"]
            in {
                "execution_error",
                "blocked_by_validation",
                "hedge_required",
            }
        )
    )
    payload["halt_required"] = halt_required
    if progress["hedge_required"]:
        payload["status"] = "hedge_required"
    elif progress["progress_quote_common"] > 0:
        payload["status"] = "progress"
    elif payload["status"] == "placed":
        payload["status"] = "no_fill"
    return payload


def rebalance_config_fingerprint(
    cfg: CrossExchangeRebalanceConfig,
    *,
    common_quote_currency: str,
) -> str:
    identity = {
        "buy_exchange": cfg.buy_exchange,
        "buy_symbol": cfg.buy_symbol,
        "sell_exchange": cfg.sell_exchange,
        "sell_symbol": cfg.sell_symbol,
        "common_quote_currency": common_quote_currency.upper(),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def new_rebalance_runtime(
    cfg: CrossExchangeRebalanceConfig,
    *,
    common_quote_currency: str,
) -> dict[str, Any]:
    return {
        "version": 2,
        "config_fingerprint": rebalance_config_fingerprint(
            cfg,
            common_quote_currency=common_quote_currency,
        ),
        "status": "disabled" if not cfg.enabled else "starting",
        "halted": False,
        "halt_reason": None,
        "completed_quote_common": 0.0,
        "completed_destination_quote_common": 0.0,
        "completed_base": 0.0,
        "cycle_count": 0,
        "live_cycle_count": 0,
        "last_payload": None,
        "updated_at": time.time(),
    }


def load_rebalance_runtime(
    path: str | Path,
    cfg: CrossExchangeRebalanceConfig,
    *,
    common_quote_currency: str,
) -> dict[str, Any]:
    fresh = new_rebalance_runtime(
        cfg,
        common_quote_currency=common_quote_currency,
    )
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return fresh
    if not isinstance(raw, dict):
        return fresh
    if raw.get("version") != fresh["version"]:
        return fresh
    if raw.get("config_fingerprint") != fresh["config_fingerprint"]:
        return fresh
    for field in (
        "completed_quote_common",
        "completed_destination_quote_common",
        "completed_base",
    ):
        try:
            fresh[field] = max(0.0, float(raw.get(field) or 0.0))
        except (TypeError, ValueError):
            fresh[field] = 0.0
    for field in ("cycle_count", "live_cycle_count"):
        try:
            fresh[field] = max(0, int(raw.get(field) or 0))
        except (TypeError, ValueError):
            fresh[field] = 0
    fresh["status"] = str(raw.get("status") or fresh["status"])
    fresh["halted"] = bool(raw.get("halted", False))
    fresh["halt_reason"] = raw.get("halt_reason")
    fresh["last_payload"] = (
        raw.get("last_payload") if isinstance(raw.get("last_payload"), dict) else None
    )
    fresh["updated_at"] = float(raw.get("updated_at") or fresh["updated_at"])
    return fresh


def save_rebalance_runtime(path: str | Path, runtime: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(runtime, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def apply_rebalance_cycle_to_runtime(
    runtime: dict[str, Any],
    payload: dict[str, Any],
    cfg: CrossExchangeRebalanceConfig,
) -> dict[str, Any]:
    updated = dict(runtime)
    updated["cycle_count"] = int(updated.get("cycle_count") or 0) + 1
    if payload.get("mode") == "live":
        updated["live_cycle_count"] = int(updated.get("live_cycle_count") or 0) + 1
    execution_progress = (
        payload.get("execution_progress")
        if isinstance(payload.get("execution_progress"), dict)
        else {}
    )
    progress_quote = max(
        0.0,
        float(execution_progress.get("progress_quote_common") or 0.0),
    )
    progress_base = max(0.0, float(execution_progress.get("matched_base") or 0.0))
    destination_quote = max(
        0.0,
        float(execution_progress.get("destination_quote_common") or 0.0),
    )
    updated["completed_quote_common"] = min(
        max(0.0, cfg.total_quote_common),
        max(0.0, float(updated.get("completed_quote_common") or 0.0)) + progress_quote,
    )
    updated["completed_base"] = (
        max(0.0, float(updated.get("completed_base") or 0.0)) + progress_base
    )
    updated["completed_destination_quote_common"] = (
        max(
            0.0,
            float(updated.get("completed_destination_quote_common") or 0.0),
        )
        + destination_quote
    )
    if payload.get("halt_required"):
        updated["halted"] = True
        updated["halt_reason"] = payload.get("status") or "execution protection"
    remaining = max(
        0.0,
        cfg.total_quote_common - updated["completed_quote_common"],
    )
    status = str(payload.get("status") or "unknown")
    if remaining <= max(cfg.total_quote_common * 1e-12, 1e-9):
        status = "complete"
    if updated.get("halted"):
        status = "halted"
    updated["status"] = status
    updated["remaining_quote_common"] = remaining
    updated["progress_pct"] = (
        min(100.0, updated["completed_quote_common"] / cfg.total_quote_common * 100)
        if cfg.total_quote_common > 0
        else 0.0
    )
    updated["last_payload"] = payload
    updated["updated_at"] = time.time()
    return updated
