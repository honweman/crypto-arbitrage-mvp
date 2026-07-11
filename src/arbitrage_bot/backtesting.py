from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from typing import Any

from .config import BacktestConfig, DcaConfig, ExecutionAlgoConfig, SpotGridConfig
from .models import BookLevel, Side


@dataclass(frozen=True)
class PaperTrade:
    step: int
    strategy: str
    side: str
    price: float
    amount: float
    quote_notional: float
    fee_quote: float
    slippage_quote: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "strategy": self.strategy,
            "side": self.side,
            "price": self.price,
            "amount": self.amount,
            "quote_notional": self.quote_notional,
            "fee_quote": self.fee_quote,
            "slippage_quote": self.slippage_quote,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EquityPoint:
    step: int
    price: float
    cash: float
    base: float
    equity: float
    drawdown_pct: float
    timestamp_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "price": self.price,
            "cash": self.cash,
            "base": self.base,
            "equity": self.equity,
            "drawdown_pct": self.drawdown_pct,
            "timestamp_ms": self.timestamp_ms,
        }


@dataclass(frozen=True)
class BacktestResult:
    status: str
    strategy: str
    symbol: str
    quote_currency: str
    initial_equity: float
    final_equity: float
    total_return_quote: float
    return_pct: float
    max_drawdown_pct: float
    fee_quote: float
    slippage_quote: float
    filled_quote: float
    filled_base: float
    fill_rate: float
    trade_count: int
    data_source: str = "synthetic"
    bar_count: int = 0
    start_timestamp_ms: int | None = None
    end_timestamp_ms: int | None = None
    benchmark_return_pct: float = 0.0
    excess_return_pct: float = 0.0
    annualized_volatility_pct: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    positive_period_rate: float | None = None
    turnover_pct: float = 0.0
    points: list[EquityPoint] = field(default_factory=list)
    trades: list[PaperTrade] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "quote_currency": self.quote_currency,
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "total_return_quote": self.total_return_quote,
            "return_pct": self.return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "fee_quote": self.fee_quote,
            "slippage_quote": self.slippage_quote,
            "filled_quote": self.filled_quote,
            "filled_base": self.filled_base,
            "fill_rate": self.fill_rate,
            "trade_count": self.trade_count,
            "data_source": self.data_source,
            "bar_count": self.bar_count,
            "start_timestamp_ms": self.start_timestamp_ms,
            "end_timestamp_ms": self.end_timestamp_ms,
            "benchmark_return_pct": self.benchmark_return_pct,
            "excess_return_pct": self.excess_return_pct,
            "annualized_volatility_pct": self.annualized_volatility_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "positive_period_rate": self.positive_period_rate,
            "turnover_pct": self.turnover_pct,
            "points": [point.to_dict() for point in self.points],
            "trades": [trade.to_dict() for trade in self.trades],
            "warnings": self.warnings,
        }


def quote_currency(symbol: str) -> str:
    return symbol.split("/", 1)[1].partition(":")[0].upper() if "/" in symbol else ""


def synthetic_price_series(
    cfg: BacktestConfig,
    *,
    current_mid: float | None = None,
) -> list[float]:
    step_count = max(2, int(cfg.step_count))
    start = cfg.price_start or current_mid or 1.0
    end = cfg.price_end or start * (1 + cfg.trend_bps / 10_000)
    amplitude = max(0.0, cfg.volatility_bps) / 10_000
    prices = []
    for index in range(step_count):
        progress = index / max(1, step_count - 1)
        trend_price = start + (end - start) * progress
        wave = math.sin(progress * math.tau * 3) * amplitude
        price = max(1e-12, trend_price * (1 + wave))
        prices.append(price)
    return prices


def _validated_price_series(values: list[float]) -> list[float]:
    prices = []
    for value in values:
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("backtest price series contains a non-number") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("backtest price series must contain finite positive prices")
        prices.append(price)
    if len(prices) < 2:
        raise ValueError("backtest price series must contain at least two prices")
    return prices


def _performance_metrics(
    points: list[EquityPoint],
    prices: list[float],
    *,
    timeframe_seconds: float | None,
    filled_quote: float,
    initial_equity: float,
    strategy_return_pct: float,
) -> dict[str, float | None]:
    benchmark_return_pct = (prices[-1] / prices[0] - 1) * 100
    returns = [
        current.equity / previous.equity - 1
        for previous, current in zip(points, points[1:])
        if previous.equity > 0
    ]
    positive_period_rate = (
        sum(1 for value in returns if value > 0) / len(returns)
        if returns
        else None
    )
    volatility_pct: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    if timeframe_seconds and timeframe_seconds > 0 and len(returns) >= 2:
        periods_per_year = 365.0 * 24.0 * 3600.0 / timeframe_seconds
        mean_return = statistics.fmean(returns)
        stdev = statistics.stdev(returns)
        if stdev > 0:
            volatility_pct = stdev * math.sqrt(periods_per_year) * 100
            sharpe_ratio = mean_return / stdev * math.sqrt(periods_per_year)
        downside_deviation = math.sqrt(
            statistics.fmean(min(0.0, value) ** 2 for value in returns)
        )
        if downside_deviation > 0:
            sortino_ratio = (
                mean_return / downside_deviation * math.sqrt(periods_per_year)
            )
    return {
        "benchmark_return_pct": benchmark_return_pct,
        "excess_return_pct": strategy_return_pct - benchmark_return_pct,
        "annualized_volatility_pct": volatility_pct,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "positive_period_rate": positive_period_rate,
        "turnover_pct": (
            filled_quote / initial_equity * 100 if initial_equity > 0 else 0.0
        ),
    }


def _mark_equity(cash: float, base: float, price: float) -> float:
    return cash + base * price


def _append_point(
    points: list[EquityPoint],
    *,
    step: int,
    price: float,
    cash: float,
    base: float,
    peak_equity: float,
) -> float:
    equity = _mark_equity(cash, base, price)
    peak = max(peak_equity, equity)
    drawdown_pct = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak * 100)
    points.append(
        EquityPoint(
            step=step,
            price=price,
            cash=cash,
            base=base,
            equity=equity,
            drawdown_pct=drawdown_pct,
        )
    )
    return peak


def synthetic_depth_levels(
    *,
    reference_price: float,
    side: Side,
    quote_per_level: float,
    step_bps: float,
    level_count: int,
) -> list[BookLevel]:
    if reference_price <= 0 or quote_per_level <= 0 or level_count <= 0:
        return []
    step = max(0.0, step_bps) / 10_000
    levels = []
    for index in range(1, level_count + 1):
        price = reference_price * (
            1 + step * index if side == "buy" else 1 - step * index
        )
        if price <= 0:
            continue
        levels.append(BookLevel(price=price, amount=quote_per_level / price))
    return levels


def estimate_depth_execution(
    levels: list[BookLevel],
    *,
    side: Side,
    reference_price: float,
    quote_notional: float = 0.0,
    base_amount: float = 0.0,
) -> dict[str, float] | None:
    if reference_price <= 0:
        return None
    if side == "buy":
        if quote_notional <= 0:
            return None
        remaining_quote = quote_notional
        gross_quote = 0.0
        amount = 0.0
        for level in levels:
            take_quote = min(remaining_quote, level.price * level.amount)
            gross_quote += take_quote
            amount += take_quote / level.price
            remaining_quote -= take_quote
            if remaining_quote <= 1e-12:
                break
        if remaining_quote > 1e-9 or amount <= 0:
            return None
        average_price = gross_quote / amount
        slippage_quote = max(0.0, average_price - reference_price) * amount
        return {
            "amount": amount,
            "average_price": average_price,
            "gross_quote": gross_quote,
            "slippage_quote": slippage_quote,
        }

    if base_amount <= 0:
        return None
    remaining_base = base_amount
    gross_quote = 0.0
    for level in levels:
        take_base = min(remaining_base, level.amount)
        gross_quote += take_base * level.price
        remaining_base -= take_base
        if remaining_base <= 1e-12:
            break
    if remaining_base > 1e-9 or gross_quote <= 0:
        return None
    average_price = gross_quote / base_amount
    slippage_quote = max(0.0, reference_price - average_price) * base_amount
    return {
        "amount": base_amount,
        "average_price": average_price,
        "gross_quote": gross_quote,
        "slippage_quote": slippage_quote,
    }


def _depth_trade_enabled(cfg: BacktestConfig) -> bool:
    return (
        cfg.depth_simulation_enabled
        and cfg.depth_quote_per_level > 0
        and cfg.depth_levels > 0
    )


def _execution_price_for_step(
    prices: list[float],
    *,
    step: int,
    signal_price: float,
    cfg: BacktestConfig,
) -> float:
    latency_steps = max(0, int(cfg.latency_steps))
    if latency_steps <= 0:
        return signal_price
    index = min(len(prices) - 1, step + latency_steps)
    return prices[index]


def _trade(
    *,
    step: int,
    strategy: str,
    side: str,
    price: float,
    quote_notional: float,
    fee_bps: float,
    slippage_bps: float,
    cash: float,
    base: float,
    reason: str,
    depth_simulation_enabled: bool = False,
    depth_quote_per_level: float = 0.0,
    depth_step_bps: float = 5.0,
    depth_levels: int = 5,
) -> tuple[float, float, PaperTrade | None]:
    if quote_notional <= 0 or price <= 0:
        return cash, base, None
    if depth_simulation_enabled and depth_quote_per_level > 0 and depth_levels > 0:
        levels = synthetic_depth_levels(
            reference_price=price,
            side=side,  # type: ignore[arg-type]
            quote_per_level=depth_quote_per_level,
            step_bps=depth_step_bps,
            level_count=depth_levels,
        )
        fee_rate = fee_bps / 10_000
        if side == "buy":
            target_quote = min(quote_notional, max(0.0, cash / (1 + fee_rate)))
            fill = estimate_depth_execution(
                levels,
                side="buy",
                reference_price=price,
                quote_notional=target_quote,
            )
            if fill is None:
                return cash, base, None
            fee_quote = fill["gross_quote"] * fee_rate
            return (
                cash - fill["gross_quote"] - fee_quote,
                base + fill["amount"],
                PaperTrade(
                    step=step,
                    strategy=strategy,
                    side=side,
                    price=fill["average_price"],
                    amount=fill["amount"],
                    quote_notional=fill["gross_quote"],
                    fee_quote=fee_quote,
                    slippage_quote=fill["slippage_quote"],
                    reason=reason,
                ),
            )
        base_amount = min(base, quote_notional / price)
        fill = estimate_depth_execution(
            levels,
            side="sell",
            reference_price=price,
            base_amount=base_amount,
        )
        if fill is None:
            return cash, base, None
        fee_quote = fill["gross_quote"] * fee_rate
        return (
            cash + fill["gross_quote"] - fee_quote,
            base - fill["amount"],
            PaperTrade(
                step=step,
                strategy=strategy,
                side=side,
                price=fill["average_price"],
                amount=fill["amount"],
                quote_notional=fill["gross_quote"],
                fee_quote=fee_quote,
                slippage_quote=fill["slippage_quote"],
                reason=reason,
            ),
        )

    slip = slippage_bps / 10_000
    fee_quote = quote_notional * fee_bps / 10_000
    slippage_quote = quote_notional * abs(slip)
    if side == "buy":
        execution_price = price * (1 + slip)
        cost = quote_notional + fee_quote + slippage_quote
        if cost > cash:
            quote_notional = max(0.0, cash / (1 + fee_bps / 10_000 + abs(slip)))
            fee_quote = quote_notional * fee_bps / 10_000
            slippage_quote = quote_notional * abs(slip)
            cost = quote_notional + fee_quote + slippage_quote
        if quote_notional <= 0:
            return cash, base, None
        amount = quote_notional / execution_price
        return (
            cash - cost,
            base + amount,
            PaperTrade(
                step=step,
                strategy=strategy,
                side=side,
                price=execution_price,
                amount=amount,
                quote_notional=quote_notional,
                fee_quote=fee_quote,
                slippage_quote=slippage_quote,
                reason=reason,
            ),
        )
    execution_price = price * (1 - slip)
    amount = min(base, quote_notional / max(execution_price, 1e-12))
    quote_notional = amount * execution_price
    if amount <= 0 or quote_notional <= 0:
        return cash, base, None
    fee_quote = quote_notional * fee_bps / 10_000
    slippage_quote = quote_notional * abs(slip)
    return (
        cash + quote_notional - fee_quote - slippage_quote,
        base - amount,
        PaperTrade(
            step=step,
            strategy=strategy,
            side=side,
            price=execution_price,
            amount=amount,
            quote_notional=quote_notional,
            fee_quote=fee_quote,
            slippage_quote=slippage_quote,
            reason=reason,
        ),
    )


def _run_grid(
    prices: list[float],
    cfg: BacktestConfig,
    strategy_cfg: SpotGridConfig,
) -> tuple[list[EquityPoint], list[PaperTrade], float, float]:
    cash = cfg.initial_cash
    base = cfg.initial_base
    points: list[EquityPoint] = []
    trades: list[PaperTrade] = []
    peak = _mark_equity(cash, base, prices[0])
    lower = strategy_cfg.lower_price or min(prices)
    upper = strategy_cfg.upper_price or max(prices)
    grid_count = max(1, strategy_cfg.grid_count)
    if upper <= lower:
        upper = lower * 1.05
    step_size = (upper - lower) / grid_count
    levels = [lower + step_size * index for index in range(grid_count + 1)]
    last_price = prices[0]
    total_target = strategy_cfg.quote_per_grid * len(levels)
    for step, price in enumerate(prices):
        for level in levels:
            side = ""
            if last_price > level >= price:
                side = "buy"
            elif last_price < level <= price:
                side = "sell"
            if not side:
                continue
            execution_price = _execution_price_for_step(
                prices,
                step=step,
                signal_price=level,
                cfg=cfg,
            )
            cash, base, trade = _trade(
                step=step,
                strategy="spot_grid",
                side=side,
                price=execution_price,
                quote_notional=strategy_cfg.quote_per_grid,
                fee_bps=cfg.fee_bps,
                slippage_bps=cfg.slippage_bps,
                cash=cash,
                base=base,
                reason=f"grid level {level:.8f} crossed",
                depth_simulation_enabled=_depth_trade_enabled(cfg),
                depth_quote_per_level=cfg.depth_quote_per_level,
                depth_step_bps=cfg.depth_step_bps,
                depth_levels=cfg.depth_levels,
            )
            if trade is not None:
                trades.append(trade)
        peak = _append_point(
            points,
            step=step,
            price=price,
            cash=cash,
            base=base,
            peak_equity=peak,
        )
        last_price = price
    return points, trades, cash, base


def _run_dca(
    prices: list[float],
    cfg: BacktestConfig,
    strategy_cfg: DcaConfig,
    timestamps_ms: list[int] | None = None,
) -> tuple[list[EquityPoint], list[PaperTrade], float, float]:
    cash = cfg.initial_cash
    base = cfg.initial_base
    points: list[EquityPoint] = []
    trades: list[PaperTrade] = []
    peak = _mark_equity(cash, base, prices[0])
    side = strategy_cfg.side if strategy_cfg.side in {"buy", "sell"} else "buy"
    max_orders = max(1, strategy_cfg.max_orders)
    order_count = 0
    next_due_ms: float | None = None
    for step, price in enumerate(prices):
        timestamp_ms = timestamps_ms[step] if timestamps_ms is not None else None
        interval_due = (
            timestamp_ms is None
            or next_due_ms is None
            or timestamp_ms >= next_due_ms
        )
        trigger = strategy_cfg.trigger_price
        triggered = trigger <= 0 or (
            side == "buy" and price <= trigger
        ) or (
            side == "sell" and price >= trigger
        )
        if triggered and interval_due and order_count < max_orders:
            quote_notional = strategy_cfg.quote_per_order * (
                strategy_cfg.size_multiplier ** order_count
            )
            execution_price = _execution_price_for_step(
                prices,
                step=step,
                signal_price=price,
                cfg=cfg,
            )
            cash, base, trade = _trade(
                step=step,
                strategy="dca",
                side=side,
                price=execution_price,
                quote_notional=quote_notional,
                fee_bps=cfg.fee_bps,
                slippage_bps=cfg.slippage_bps,
                cash=cash,
                base=base,
                reason="DCA trigger",
                depth_simulation_enabled=_depth_trade_enabled(cfg),
                depth_quote_per_level=cfg.depth_quote_per_level,
                depth_step_bps=cfg.depth_step_bps,
                depth_levels=cfg.depth_levels,
            )
            if trade is not None:
                trades.append(trade)
                order_count += 1
                if timestamp_ms is not None:
                    next_due_ms = timestamp_ms + max(
                        1.0,
                        strategy_cfg.interval_seconds,
                    ) * 1000
        peak = _append_point(
            points,
            step=step,
            price=price,
            cash=cash,
            base=base,
            peak_equity=peak,
        )
    return points, trades, cash, base


def _run_execution_algo(
    prices: list[float],
    cfg: BacktestConfig,
    strategy_cfg: ExecutionAlgoConfig,
) -> tuple[list[EquityPoint], list[PaperTrade], float, float]:
    cash = cfg.initial_cash
    base = cfg.initial_base
    points: list[EquityPoint] = []
    trades: list[PaperTrade] = []
    peak = _mark_equity(cash, base, prices[0])
    side = strategy_cfg.side if strategy_cfg.side in {"buy", "sell"} else "buy"
    slice_count = max(1, strategy_cfg.slice_count)
    target_quote = strategy_cfg.total_quote or strategy_cfg.total_base * prices[0]
    if target_quote <= 0:
        target_quote = cfg.initial_cash
    interval = max(1, len(prices) // slice_count)
    for step, price in enumerate(prices):
        if step % interval == 0 and len(trades) < slice_count:
            remaining = max(0.0, target_quote - sum(t.quote_notional for t in trades))
            quote_notional = min(remaining, target_quote / slice_count)
            execution_price = _execution_price_for_step(
                prices,
                step=step,
                signal_price=price,
                cfg=cfg,
            )
            cash, base, trade = _trade(
                step=step,
                strategy="execution_algo",
                side=side,
                price=execution_price,
                quote_notional=quote_notional,
                fee_bps=cfg.fee_bps,
                slippage_bps=cfg.slippage_bps,
                cash=cash,
                base=base,
                reason=f"{strategy_cfg.algo.upper()} slice",
                depth_simulation_enabled=_depth_trade_enabled(cfg),
                depth_quote_per_level=cfg.depth_quote_per_level,
                depth_step_bps=cfg.depth_step_bps,
                depth_levels=cfg.depth_levels,
            )
            if trade is not None:
                trades.append(trade)
        peak = _append_point(
            points,
            step=step,
            price=price,
            cash=cash,
            base=base,
            peak_equity=peak,
        )
    return points, trades, cash, base


def run_paper_backtest(
    cfg: BacktestConfig,
    *,
    spot_grid: SpotGridConfig | None = None,
    dca: DcaConfig | None = None,
    execution_algo: ExecutionAlgoConfig | None = None,
    current_mid: float | None = None,
    price_series: list[float] | None = None,
    timestamps_ms: list[int] | None = None,
    timeframe_seconds: float | None = None,
    data_source: str | None = None,
) -> BacktestResult:
    strategy = str(cfg.strategy or "spot_grid").lower()
    if strategy not in {"spot_grid", "dca", "execution_algo"}:
        raise ValueError("backtest.strategy must be spot_grid, dca, or execution_algo")
    if cfg.initial_cash < 0 or cfg.initial_base < 0:
        raise ValueError("backtest initial balances must be non-negative")
    if cfg.fee_bps < 0 or cfg.slippage_bps < 0:
        raise ValueError("backtest fee_bps and slippage_bps must be non-negative")

    prices = (
        _validated_price_series(price_series)
        if price_series is not None
        else synthetic_price_series(cfg, current_mid=current_mid)
    )
    normalized_timestamps: list[int] | None = None
    if timestamps_ms is not None:
        if len(timestamps_ms) != len(prices):
            raise ValueError("backtest timestamps must match the price series")
        normalized_timestamps = [int(value) for value in timestamps_ms]
        if any(
            current <= previous
            for previous, current in zip(
                normalized_timestamps,
                normalized_timestamps[1:],
            )
        ):
            raise ValueError("backtest timestamps must be strictly increasing")
    source = str(
        data_source
        or ("exchange_ohlcv" if price_series is not None else cfg.data_source)
        or "synthetic"
    ).strip().lower()
    symbol = cfg.symbol
    if strategy == "spot_grid":
        strategy_cfg = spot_grid or SpotGridConfig(symbol=symbol)
        symbol = symbol or strategy_cfg.symbol
        points, trades, cash, base = _run_grid(prices, cfg, strategy_cfg)
    elif strategy == "dca":
        strategy_cfg = dca or DcaConfig(symbol=symbol)
        symbol = symbol or strategy_cfg.symbol
        points, trades, cash, base = _run_dca(
            prices,
            cfg,
            strategy_cfg,
            normalized_timestamps,
        )
    else:
        strategy_cfg = execution_algo or ExecutionAlgoConfig(symbol=symbol)
        symbol = symbol or strategy_cfg.symbol
        points, trades, cash, base = _run_execution_algo(prices, cfg, strategy_cfg)

    initial_equity = _mark_equity(cfg.initial_cash, cfg.initial_base, prices[0])
    final_equity = _mark_equity(cash, base, prices[-1])
    total_return = final_equity - initial_equity
    return_pct = 0.0 if initial_equity <= 0 else total_return / initial_equity * 100
    filled_quote = sum(trade.quote_notional for trade in trades)
    target_quote = (
        cfg.initial_cash
        if strategy == "execution_algo"
        else max(filled_quote, sum(trade.quote_notional for trade in trades))
    )
    if strategy == "spot_grid" and spot_grid is not None:
        target_quote = spot_grid.quote_per_grid * max(1, spot_grid.grid_count)
    elif strategy == "dca" and dca is not None:
        target_quote = sum(
            dca.quote_per_order * (dca.size_multiplier**idx)
            for idx in range(max(1, dca.max_orders))
        )
    elif strategy == "execution_algo" and execution_algo is not None:
        target_quote = execution_algo.total_quote or execution_algo.total_base * prices[0]
    target_quote = max(target_quote, filled_quote, 1e-12)
    if normalized_timestamps is not None:
        points = [
            replace(point, timestamp_ms=normalized_timestamps[point.step])
            for point in points
        ]
    metrics = _performance_metrics(
        points,
        prices,
        timeframe_seconds=timeframe_seconds,
        filled_quote=filled_quote,
        initial_equity=initial_equity,
        strategy_return_pct=return_pct,
    )
    max_recent = max(1, cfg.max_recent_points)
    warnings = (
        ["synthetic price path; use exchange history before live deployment"]
        if source == "synthetic"
        else [
            "historical OHLCV close-price path; intrabar order sequence and queue priority are not modeled"
        ]
    )
    if _depth_trade_enabled(cfg):
        warnings.append("synthetic order book depth simulation is enabled")
    if cfg.latency_steps > 0:
        warnings.append(f"execution latency simulation uses {cfg.latency_steps} step(s)")
    return BacktestResult(
        status="ok",
        strategy=strategy,
        symbol=symbol,
        quote_currency=quote_currency(symbol),
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return_quote=total_return,
        return_pct=return_pct,
        max_drawdown_pct=max((point.drawdown_pct for point in points), default=0.0),
        fee_quote=sum(trade.fee_quote for trade in trades),
        slippage_quote=sum(trade.slippage_quote for trade in trades),
        filled_quote=filled_quote,
        filled_base=sum(trade.amount for trade in trades),
        fill_rate=min(1.0, filled_quote / target_quote),
        trade_count=len(trades),
        data_source=source,
        bar_count=len(prices),
        start_timestamp_ms=(
            normalized_timestamps[0] if normalized_timestamps is not None else None
        ),
        end_timestamp_ms=(
            normalized_timestamps[-1] if normalized_timestamps is not None else None
        ),
        benchmark_return_pct=float(metrics["benchmark_return_pct"] or 0.0),
        excess_return_pct=float(metrics["excess_return_pct"] or 0.0),
        annualized_volatility_pct=metrics["annualized_volatility_pct"],
        sharpe_ratio=metrics["sharpe_ratio"],
        sortino_ratio=metrics["sortino_ratio"],
        positive_period_rate=metrics["positive_period_rate"],
        turnover_pct=float(metrics["turnover_pct"] or 0.0),
        points=points[-max_recent:],
        trades=trades[-max_recent:],
        warnings=warnings,
    )
