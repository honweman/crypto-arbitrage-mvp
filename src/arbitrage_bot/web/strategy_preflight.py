from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from ..config import BotConfig
from ..risk import current_daily_pnl_quote, risk_config_for_strategy


PREFLIGHT_TOKEN_TTL_SECONDS = 45.0


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def candidate_hash(strategy_id: str, candidate: dict[str, Any]) -> str:
    payload = {"strategy_id": strategy_id, "candidate": candidate}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _base_quote(symbol: str) -> tuple[str, str]:
    base, _, quote = str(symbol or "").partition("/")
    return base.upper(), quote.partition(":")[0].upper()


def _check(
    check_id: str,
    label: str,
    status: str,
    detail: str,
    *,
    scope: str = "",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "scope": scope,
        "blocking": status == "blocked",
    }


def _account_row(payload: dict[str, Any], exchange: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in payload.get("accounts", [])
            if isinstance(row, dict) and row.get("exchange") == exchange
        ),
        None,
    )


def _market_limit(
    account: dict[str, Any] | None,
    symbol: str,
) -> dict[str, Any] | None:
    if not isinstance(account, dict):
        return None
    return next(
        (
            row
            for row in account.get("markets", [])
            if isinstance(row, dict) and row.get("symbol") == symbol
        ),
        None,
    )


def _balance_free(account: dict[str, Any] | None, currency: str) -> float | None:
    if not isinstance(account, dict):
        return None
    balance = account.get("balance")
    if not isinstance(balance, dict) or not balance.get("checked"):
        return None
    for row in balance.get("currencies", []):
        if isinstance(row, dict) and row.get("currency") == currency:
            return _number(row.get("free"))
    return 0.0


def _market_row(
    state_payload: dict[str, Any], exchange: str, symbol: str
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in state_payload.get("markets", [])
            if isinstance(row, dict)
            and row.get("exchange") == exchange
            and row.get("symbol") == symbol
        ),
        None,
    )


def _order_strategy(row: dict[str, Any]) -> str:
    attribution = row.get("attribution")
    if isinstance(attribution, dict) and attribution.get("strategy"):
        return str(attribution["strategy"])
    client_id = str(row.get("client_order_id") or "").lower()
    if "crypto-arb-mm" in client_id:
        return "market_maker"
    if "crypto-arb-slow" in client_id or "auto" in client_id:
        return "slow_execution"
    if "crypto-arb-rebalance" in client_id:
        return "cross_exchange_rebalance"
    if "crypto-arb-spot" in client_id:
        return "spot_spread"
    return "unattributed"


def _candidate_routes(
    cfg: BotConfig,
    *,
    strategy_id: str,
    candidate: dict[str, Any],
    state_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, float | int]]:
    routes: list[dict[str, Any]] = []
    summary: dict[str, float | int] = {
        "planned_order_count": 0,
        "max_order_common": 0.0,
        "cycle_quote_common": 0.0,
    }
    quote_rates = state_payload.get("quote_rates") or cfg.quote_rates

    def add_route(
        exchange: str,
        symbol: str,
        side: str,
        *,
        order_quote_local: float,
        required_quote_local: float = 0.0,
        required_base: float = 0.0,
    ) -> None:
        _, quote = _base_quote(symbol)
        rate = _number(quote_rates.get(quote)) or 0.0
        routes.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "side": side,
                "order_quote_local": max(0.0, order_quote_local),
                "required_quote_local": max(0.0, required_quote_local),
                "required_base": max(0.0, required_base),
                "quote_currency": quote,
                "quote_rate": rate,
            }
        )
        summary["max_order_common"] = max(
            float(summary["max_order_common"]),
            max(0.0, order_quote_local) * rate,
        )

    if strategy_id == "market_maker":
        exchange = str(candidate.get("exchange") or "")
        symbol = str(candidate.get("symbol") or "")
        levels = max(0, int(candidate.get("levels") or 0))
        per_level = max(0.0, float(candidate.get("quote_per_level") or 0.0))
        market = _market_row(state_payload, exchange, symbol) or {}
        bid = _number(market.get("bid")) or 0.0
        required_base = levels * per_level / bid if bid > 0 else 0.0
        add_route(
            exchange,
            symbol,
            "buy",
            order_quote_local=per_level,
            required_quote_local=levels * per_level,
        )
        add_route(
            exchange,
            symbol,
            "sell",
            order_quote_local=per_level,
            required_base=required_base,
        )
        summary["planned_order_count"] = levels * 2
        summary["cycle_quote_common"] = sum(
            levels * per_level * float(route["quote_rate"]) for route in routes
        )
    elif strategy_id == "slow_execution":
        exchange = str(candidate.get("exchange") or "")
        symbol = str(candidate.get("symbol") or "")
        side = str(candidate.get("side") or "").lower()
        market = _market_row(state_payload, exchange, symbol) or {}
        price = _number(market.get("ask" if side == "buy" else "bid")) or 0.0
        slice_quote = max(0.0, float(candidate.get("slice_quote") or 0.0))
        slice_base = max(
            0.0,
            float(
                candidate.get("slice_base_max") or candidate.get("slice_base") or 0.0
            ),
        )
        order_quote = slice_quote or slice_base * price
        add_route(
            exchange,
            symbol,
            side,
            order_quote_local=order_quote,
            required_quote_local=order_quote if side == "buy" else 0.0,
            required_base=slice_base if side == "sell" else 0.0,
        )
        summary["planned_order_count"] = 1
        summary["cycle_quote_common"] = order_quote * float(routes[0]["quote_rate"])
    elif strategy_id == "cross_exchange_rebalance":
        cycle_common = max(
            0.0,
            float(candidate.get("quote_per_cycle_common") or 0.0),
        )
        buy_exchange = str(candidate.get("buy_exchange") or "")
        buy_symbol = str(candidate.get("buy_symbol") or "")
        sell_exchange = str(candidate.get("sell_exchange") or "")
        sell_symbol = str(candidate.get("sell_symbol") or "")
        buy_market = _market_row(state_payload, buy_exchange, buy_symbol) or {}
        buy_price = _number(buy_market.get("ask")) or 0.0
        _, buy_quote = _base_quote(buy_symbol)
        buy_rate = _number(quote_rates.get(buy_quote)) or 0.0
        buy_quote_local = cycle_common / buy_rate if buy_rate > 0 else 0.0
        base_amount = buy_quote_local / buy_price if buy_price > 0 else 0.0
        sell_market = _market_row(state_payload, sell_exchange, sell_symbol) or {}
        sell_price = _number(sell_market.get("bid")) or 0.0
        add_route(
            buy_exchange,
            buy_symbol,
            "buy",
            order_quote_local=buy_quote_local,
            required_quote_local=buy_quote_local,
        )
        add_route(
            sell_exchange,
            sell_symbol,
            "sell",
            order_quote_local=base_amount * sell_price,
            required_base=base_amount,
        )
        summary["planned_order_count"] = 2
        summary["cycle_quote_common"] = cycle_common * 2
    elif strategy_id == "spot_spread":
        order_common = max(
            0.0, float(candidate.get("notional_quote") or cfg.notional_quote)
        )
        for market in cfg.spot_markets:
            row = _market_row(state_payload, market.exchange, market.symbol) or {}
            price = _number(row.get("ask")) or 0.0
            rate = _number(quote_rates.get(market.quote_currency)) or 0.0
            local_quote = order_common / rate if rate > 0 else 0.0
            add_route(
                market.exchange,
                market.symbol,
                "both",
                order_quote_local=local_quote,
                required_quote_local=local_quote,
                required_base=local_quote / price if price > 0 else 0.0,
            )
        summary["planned_order_count"] = 2
        summary["cycle_quote_common"] = order_common * 2
    else:
        raise ValueError(f"preflight is not supported for strategy: {strategy_id}")
    return routes, summary


def build_strategy_preflight(
    cfg: BotConfig,
    *,
    strategy_id: str,
    candidate: dict[str, Any],
    state_payload: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    risk = risk_config_for_strategy(cfg.risk, strategy_id)
    routes, summary = _candidate_routes(
        cfg,
        strategy_id=strategy_id,
        candidate=candidate,
        state_payload=state_payload,
    )
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "global_live",
            "Global live trading",
            "passed" if risk.allow_live_trading and risk.trading_enabled else "blocked",
            (
                "Live trading and the global trading switch are enabled"
                if risk.allow_live_trading and risk.trading_enabled
                else "Enable allow_live_trading and trading_enabled"
            ),
        )
    )
    strategy_allowed = bool(risk.strategy_enabled.get(strategy_id, False))
    checks.append(
        _check(
            "strategy_risk",
            "Strategy risk switch",
            "passed" if strategy_allowed else "blocked",
            (
                f"risk.strategy_enabled.{strategy_id} is enabled"
                if strategy_allowed
                else f"risk.strategy_enabled.{strategy_id} is disabled"
            ),
        )
    )
    planned_orders = int(summary["planned_order_count"])
    cycle_common = float(summary["cycle_quote_common"])
    max_order_common = float(summary["max_order_common"])
    budget_reasons = []
    if risk.max_order_quote > 0 and max_order_common > risk.max_order_quote + 1e-9:
        budget_reasons.append(
            f"max order {max_order_common:.8g} exceeds {risk.max_order_quote:.8g}"
        )
    if risk.max_cycle_quote > 0 and cycle_common > risk.max_cycle_quote + 1e-9:
        budget_reasons.append(
            f"cycle total {cycle_common:.8g} exceeds {risk.max_cycle_quote:.8g}"
        )
    if risk.max_orders_per_cycle > 0 and planned_orders > risk.max_orders_per_cycle:
        budget_reasons.append(
            f"planned orders {planned_orders} exceed {risk.max_orders_per_cycle}"
        )
    checks.append(
        _check(
            "risk_budget",
            "Order and cycle budget",
            "blocked" if budget_reasons else "passed",
            "; ".join(budget_reasons)
            if budget_reasons
            else f"{planned_orders} order(s), {cycle_common:.8g} {cfg.common_quote_currency}",
        )
    )
    daily_pnl = current_daily_pnl_quote(cfg)
    daily_summary = (state_payload.get("order_activity") or {}).get("daily_pnl")
    if isinstance(daily_summary, dict):
        daily_pnl += float(daily_summary.get("total_realized_pnl") or 0.0)
    daily_blocked = (
        risk.max_daily_loss_quote > 0 and daily_pnl <= -risk.max_daily_loss_quote
    )
    checks.append(
        _check(
            "daily_loss",
            "Daily loss",
            "blocked" if daily_blocked else "passed",
            f"Current {daily_pnl:.8g}; stop threshold {-risk.max_daily_loss_quote:.8g}",
        )
    )

    balances = state_payload.get("account_balances") or {}
    activity = state_payload.get("order_activity") or {}
    balance_age = (
        now - float(balances.get("last_finished"))
        if isinstance(balances.get("last_finished"), (int, float))
        else None
    )
    activity_age = (
        now - float(activity.get("last_finished"))
        if isinstance(activity.get("last_finished"), (int, float))
        else None
    )
    max_private_age = max(15.0, float(risk.max_order_book_age_seconds or 0.0) * 2)
    private_fresh = (
        balance_age is not None
        and activity_age is not None
        and 0 <= balance_age <= max_private_age
        and 0 <= activity_age <= max_private_age
    )
    checks.append(
        _check(
            "private_data_freshness",
            "Private account data",
            "passed" if private_fresh else "blocked",
            (
                f"Balances {balance_age:.1f}s old; orders {activity_age:.1f}s old"
                if balance_age is not None and activity_age is not None
                else "Balance or order checks have not completed"
            ),
        )
    )

    open_orders = [
        row for row in activity.get("open_orders", []) if isinstance(row, dict)
    ]
    non_owned_open_count = 0
    seen_routes: set[tuple[str, str]] = set()
    for route in routes:
        exchange = str(route["exchange"])
        symbol = str(route["symbol"])
        if not exchange or not symbol:
            checks.append(
                _check(
                    "route_config",
                    "Account and pair",
                    "blocked",
                    "Account and pair are required",
                )
            )
            continue
        route_key = (exchange, symbol)
        if route_key in seen_routes:
            continue
        seen_routes.add(route_key)
        scope = f"{exchange} {symbol}"
        account_enabled = bool(risk.account_enabled.get(exchange, False))
        checks.append(
            _check(
                f"account_switch:{exchange}",
                "Account risk switch",
                "passed" if account_enabled else "blocked",
                f"risk.account_enabled.{exchange} is "
                f"{'enabled' if account_enabled else 'disabled'}",
                scope=scope,
            )
        )
        balance_account = _account_row(balances, exchange)
        auth = balance_account.get("auth", {}) if balance_account else {}
        auth_ready = bool(
            balance_account
            and auth.get("configured")
            and not auth.get("missing_env")
            and (balance_account.get("balance") or {}).get("checked")
            and not balance_account.get("errors")
        )
        checks.append(
            _check(
                f"api:{exchange}",
                "API access",
                "passed" if auth_ready else "blocked",
                (
                    "Private balance and order access succeeded"
                    if auth_ready
                    else "; ".join((balance_account or {}).get("errors", []))
                    or "API credentials or private checks are unavailable"
                ),
                scope=scope,
            )
        )
        limit_row = _market_limit(balance_account, symbol)
        market_info = limit_row.get("market", {}) if limit_row else {}
        market_ready = bool(
            limit_row
            and limit_row.get("status") == "ok"
            and market_info.get("found")
            and market_info.get("active") is not False
        )
        checks.append(
            _check(
                f"market:{exchange}:{symbol}",
                "Market and minimum order",
                "passed" if market_ready else "blocked",
                (
                    f"Minimum notional {((market_info.get('limits') or {}).get('cost_min') or 0):.8g}"
                    if market_ready
                    else str(
                        (limit_row or {}).get("error") or "Market metadata unavailable"
                    )
                ),
                scope=scope,
            )
        )
        minimum = _number((market_info.get("limits") or {}).get("cost_min")) or 0.0
        order_quote = float(route.get("order_quote_local") or 0.0)
        if minimum > 0 and order_quote + 1e-12 < minimum:
            checks.append(
                _check(
                    f"minimum:{exchange}:{symbol}:{route.get('side')}",
                    "Planned order size",
                    "blocked",
                    f"Planned {order_quote:.8g} is below exchange minimum {minimum:.8g}",
                    scope=scope,
                )
            )
        market = _market_row(state_payload, exchange, symbol)
        bid = _number((market or {}).get("bid"))
        ask = _number((market or {}).get("ask"))
        top_depth = min(
            (_number((market or {}).get("bid_size")) or 0.0) * (bid or 0.0),
            (_number((market or {}).get("ask_size")) or 0.0) * (ask or 0.0),
        )
        spread_bps = (
            (ask - bid) / ((ask + bid) / 2) * 10_000
            if bid and ask and ask > bid
            else None
        )
        market_fresh = bool(
            market
            and market.get("status") == "ok"
            and bid
            and ask
            and spread_bps is not None
        )
        market_reasons = []
        if not market_fresh:
            market_reasons.append("Order book is unavailable")
        if (
            spread_bps is not None
            and risk.max_order_book_gap_bps > 0
            and spread_bps > risk.max_order_book_gap_bps
        ):
            market_reasons.append(
                f"spread {spread_bps:.4g} bps exceeds {risk.max_order_book_gap_bps:.4g}"
            )
        if (
            risk.min_order_book_depth_quote > 0
            and top_depth < risk.min_order_book_depth_quote
        ):
            market_reasons.append(
                f"top depth {top_depth:.8g} is below {risk.min_order_book_depth_quote:.8g}"
            )
        checks.append(
            _check(
                f"book:{exchange}:{symbol}",
                "Order book, depth, spread and rate",
                "blocked" if market_reasons else "passed",
                "; ".join(market_reasons)
                if market_reasons
                else f"spread {spread_bps:.4g} bps; top depth {top_depth:.8g}",
                scope=scope,
            )
        )
        if float(route.get("quote_rate") or 0.0) <= 0:
            checks.append(
                _check(
                    f"rate:{exchange}:{symbol}",
                    "Quote conversion",
                    "blocked",
                    f"No positive conversion rate for {route.get('quote_currency')}",
                    scope=scope,
                )
            )

    required_by_currency: dict[tuple[str, str], float] = {}
    for route in routes:
        exchange = str(route["exchange"])
        symbol = str(route["symbol"])
        base, quote = _base_quote(symbol)
        if float(route.get("required_quote_local") or 0.0) > 0:
            key = (exchange, quote)
            required_by_currency[key] = required_by_currency.get(key, 0.0) + float(
                route["required_quote_local"]
            )
        if float(route.get("required_base") or 0.0) > 0:
            key = (exchange, base)
            required_by_currency[key] = required_by_currency.get(key, 0.0) + float(
                route["required_base"]
            )
    for (exchange, currency), required in sorted(required_by_currency.items()):
        account = _account_row(balances, exchange)
        free = _balance_free(account, currency)
        enough = free is not None and free + 1e-12 >= required
        checks.append(
            _check(
                f"balance:{exchange}:{currency}",
                "Available balance",
                "passed" if enough else "blocked",
                (
                    f"{currency} free {float(free):.8g}; required {required:.8g}"
                    if free is not None
                    else f"{currency} balance is unavailable"
                ),
                scope=exchange,
            )
        )

    route_sides: dict[tuple[str, str], str] = {}
    for route in routes:
        key = (str(route["exchange"]), str(route["symbol"]))
        side = str(route.get("side") or "")
        previous = route_sides.get(key)
        route_sides[key] = "both" if previous and previous != side else side
    owned_order_ids: set[str] = set()
    coordinated_market_maker_order_ids: set[str] = set()
    coordinate_market_maker = False
    if strategy_id == "cross_exchange_rebalance":
        coordinate_market_maker = bool(
            candidate.get("coordinate_market_maker", True)
        )
    elif strategy_id == "slow_execution":
        coordinate_market_maker = bool(
            candidate.get("coordinate_market_maker", False)
        ) and bool(candidate.get("block_conflicting_market_maker", True))
    market_maker = state_payload.get("market_maker") or {}
    mm_runtime = market_maker.get("runtime") if isinstance(market_maker, dict) else {}
    mm_instances = (
        mm_runtime.get("instances", []) if isinstance(mm_runtime, dict) else []
    )
    if isinstance(mm_instances, dict):
        mm_instances = list(mm_instances.values())
    for instance in mm_instances or []:
        if not isinstance(instance, dict):
            continue
        key = (
            str(instance.get("open_order_exchange") or ""),
            str(instance.get("open_order_symbol") or ""),
        )
        if key not in route_sides:
            continue
        instance_order_ids = {
            str(order_id)
            for order_id in instance.get("open_order_ids", []) or []
            if order_id
        }
        if strategy_id == "market_maker":
            owned_order_ids.update(instance_order_ids)
        elif coordinate_market_maker:
            coordinated_market_maker_order_ids.update(instance_order_ids)
    slow_tasks = ((state_payload.get("slow_execution") or {}).get("tasks") or {}).get(
        "tasks", []
    )
    if strategy_id == "slow_execution":
        for task in slow_tasks or []:
            if not isinstance(task, dict):
                continue
            config = task.get("config") if isinstance(task.get("config"), dict) else {}
            key = (str(config.get("exchange") or ""), str(config.get("symbol") or ""))
            if key not in route_sides:
                continue
            owned_order_ids.update(
                str(order_id)
                for order_id in task.get("open_order_ids", []) or []
                if order_id
            )
    conflicts = []
    coordinated_conflict_count = 0
    for order in open_orders:
        key = (str(order.get("exchange") or ""), str(order.get("symbol") or ""))
        candidate_side = route_sides.get(key)
        if not candidate_side:
            continue
        order_id = str(order.get("id") or "")
        if order_id in owned_order_ids:
            continue
        order_side = str(order.get("side") or "").lower()
        opposing_order = candidate_side == "both" or order_side != candidate_side
        if order_id in coordinated_market_maker_order_ids and opposing_order:
            coordinated_conflict_count += 1
            continue
        owner = _order_strategy(order)
        if owner == strategy_id:
            continue
        non_owned_open_count += 1
        if candidate_side in {"both", "buy", "sell"} and opposing_order:
            conflicts.append(
                f"{key[0]} {key[1]} {order_side} order {order.get('id')} ({owner})"
            )
    checks.append(
        _check(
            "order_conflicts",
            "Conflicting open orders",
            "blocked" if conflicts else "passed",
            "; ".join(conflicts[:5])
            if conflicts
            else "No opposing strategy orders detected",
        )
    )
    if coordinated_conflict_count:
        checks.append(
            _check(
                "market_maker_coordination",
                "Market maker coordination",
                "warning",
                (
                    f"{coordinated_conflict_count} tracked conflicting MM order(s) "
                    "will be canceled and confirmed before strategy orders are placed"
                ),
            )
        )
    projected_open = non_owned_open_count + planned_orders
    open_blocked = risk.max_open_orders > 0 and projected_open > risk.max_open_orders
    checks.append(
        _check(
            "open_order_limit",
            "Projected open orders",
            "blocked" if open_blocked else "passed",
            f"Projected {projected_open}; limit {risk.max_open_orders}",
        )
    )

    blockers = [row["detail"] for row in checks if row["status"] == "blocked"]
    warnings = [row["detail"] for row in checks if row["status"] == "warning"]
    return {
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "strategy_id": strategy_id,
        "candidate_hash": candidate_hash(strategy_id, candidate),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "summary": summary,
        "checked_at": now,
        "expires_at": now + PREFLIGHT_TOKEN_TTL_SECONDS,
    }


@dataclass(frozen=True)
class PreflightGrant:
    token: str
    owner_email: str
    strategy_id: str
    candidate_hash: str
    issued_at: float
    expires_at: float


class StrategyPreflightService:
    def __init__(self, *, ttl_seconds: float = PREFLIGHT_TOKEN_TTL_SECONDS) -> None:
        self.ttl_seconds = max(5.0, float(ttl_seconds))
        self._grants: dict[str, PreflightGrant] = {}

    def _prune(self) -> None:
        now = time.time()
        self._grants = {
            token: grant
            for token, grant in self._grants.items()
            if grant.expires_at > now
        }

    def issue(
        self,
        *,
        owner_email: str,
        strategy_id: str,
        candidate: dict[str, Any],
    ) -> PreflightGrant:
        self._prune()
        now = time.time()
        token = secrets.token_urlsafe(32)
        grant = PreflightGrant(
            token=token,
            owner_email=str(owner_email or "legacy-admin").lower(),
            strategy_id=strategy_id,
            candidate_hash=candidate_hash(strategy_id, candidate),
            issued_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._grants[token] = grant
        return grant

    def consume(
        self,
        token: str,
        *,
        owner_email: str,
        strategy_id: str,
        candidate: dict[str, Any],
    ) -> PreflightGrant:
        self._prune()
        grant = self._grants.pop(str(token or ""), None)
        if grant is None:
            raise ValueError(
                "run the strategy preflight again; its approval is missing or expired"
            )
        if grant.owner_email != str(owner_email or "legacy-admin").lower():
            raise ValueError("strategy preflight approval belongs to another user")
        if grant.strategy_id != strategy_id:
            raise ValueError("strategy preflight approval is for another strategy")
        if grant.candidate_hash != candidate_hash(strategy_id, candidate):
            raise ValueError(
                "strategy parameters changed after preflight; run it again"
            )
        return grant
