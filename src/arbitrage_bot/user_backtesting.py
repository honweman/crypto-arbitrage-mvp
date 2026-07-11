from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .backtesting import run_paper_backtest
from .config import BacktestConfig, DcaConfig, SpotGridConfig
from .exchanges import ExchangeManager
from .user_account_check import workspace_exchange_config
from .user_strategies import UserStrategy
from .user_workspace import UserExchangeAccount, UserWorkspaceStore


TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
}
SUPPORTED_STRATEGIES = {"spot_grid", "dca"}
MIN_HISTORY_BARS = 20
MAX_HISTORY_BARS = 500
DEFAULT_HISTORY_BARS = 200
MAX_RUNS_PER_OWNER = 50
MAX_ACTIVE_RUNS_PER_OWNER = 3
PUBLIC_FETCH_TIMEOUT_SECONDS = 30.0
ACTIVE_STATUSES = {"queued", "fetching", "running"}
PUBLIC_OHLCV_BATCH_LIMIT = 300
PUBLIC_OHLCV_MAX_PAGES = 8


def _now() -> float:
    return time.time()


def _new_run_id() -> str:
    return f"backtest-{uuid.uuid4().hex[:16]}"


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
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{label} must be at most {maximum:g}")
    return result


def _bounded_int(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    number = _finite_float(
        value,
        label=label,
        minimum=float(minimum),
        maximum=float(maximum),
    )
    if not number.is_integer():
        raise ValueError(f"{label} must be an integer")
    return int(number)


def normalize_ohlcv_rows(rows: list[list[Any]]) -> list[dict[str, float | int]]:
    normalized: dict[int, dict[str, float | int]] = {}
    for raw in rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 5:
            continue
        try:
            timestamp_ms = int(raw[0])
            open_price = float(raw[1])
            high_price = float(raw[2])
            low_price = float(raw[3])
            close_price = float(raw[4])
            volume = float(raw[5]) if len(raw) > 5 and raw[5] is not None else 0.0
        except (TypeError, ValueError):
            continue
        prices = (open_price, high_price, low_price, close_price)
        if timestamp_ms <= 0 or any(
            not math.isfinite(price) or price <= 0 for price in prices
        ):
            continue
        if not math.isfinite(volume) or volume < 0:
            volume = 0.0
        normalized[timestamp_ms] = {
            "timestamp_ms": timestamp_ms,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
    return [normalized[key] for key in sorted(normalized)]


def completed_ohlcv_rows(
    rows: list[dict[str, float | int]],
    *,
    timeframe: str,
    now_ms: int | None = None,
) -> list[dict[str, float | int]]:
    period_seconds = TIMEFRAME_SECONDS.get(timeframe)
    if period_seconds is None:
        raise ValueError(f"unsupported backtest timeframe: {timeframe}")
    cutoff = int(_now() * 1000) if now_ms is None else int(now_ms)
    period_ms = period_seconds * 1000
    return [
        row
        for row in rows
        if int(row["timestamp_ms"]) + period_ms <= cutoff
    ]


def fill_ohlcv_gaps(
    rows: list[dict[str, float | int]],
    *,
    timeframe: str,
    limit: int,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    period_seconds = TIMEFRAME_SECONDS.get(timeframe)
    if period_seconds is None:
        raise ValueError(f"unsupported backtest timeframe: {timeframe}")
    period_ms = period_seconds * 1000
    cutoff = int(_now() * 1000) if now_ms is None else int(now_ms)
    last_bucket = cutoff // period_ms * period_ms - period_ms
    if last_bucket < 0:
        return []
    target_start = last_bucket - (max(1, limit) - 1) * period_ms
    bucketed: dict[int, dict[str, Any]] = {}
    for raw in completed_ohlcv_rows(rows, timeframe=timeframe, now_ms=cutoff):
        bucket = int(raw["timestamp_ms"]) // period_ms * period_ms
        bucketed[bucket] = {**raw, "timestamp_ms": bucket, "gap_filled": False}
    if not bucketed:
        return []

    prior_buckets = [bucket for bucket in bucketed if bucket <= target_start]
    if prior_buckets:
        start_bucket = target_start
        previous = dict(bucketed[max(prior_buckets)])
    else:
        start_bucket = min(bucketed)
        previous = None

    result: list[dict[str, Any]] = []
    bucket = start_bucket
    while bucket <= last_bucket:
        actual = bucketed.get(bucket)
        if actual is not None:
            current = dict(actual)
            previous = current
        elif previous is not None:
            close_price = float(previous["close"])
            current = {
                "timestamp_ms": bucket,
                "open": close_price,
                "high": close_price,
                "low": close_price,
                "close": close_price,
                "volume": 0.0,
                "gap_filled": True,
            }
            previous = current
        else:
            bucket += period_ms
            continue
        result.append(current)
        bucket += period_ms
    return result[-limit:]


async def fetch_public_ohlcv(
    account: UserExchangeAccount,
    *,
    timeframe: str,
    limit: int,
    timeout_seconds: float = PUBLIC_FETCH_TIMEOUT_SECONDS,
    manager_factory: Callable[..., ExchangeManager] = ExchangeManager,
) -> list[dict[str, float | int]]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported backtest timeframe: {timeframe}")
    limit = _bounded_int(
        limit,
        label="history bars",
        minimum=MIN_HISTORY_BARS,
        maximum=MAX_HISTORY_BARS,
    )
    exchange_cfg = workspace_exchange_config(
        exchange=account.exchange,
        market_type=account.market_type,
        api_variant=account.api_variant,
        runtime_key=f"public-backtest:{account.exchange}:{account.api_variant}",
    )
    manager = manager_factory()
    now_ms = int(_now() * 1000)
    period_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    last_bucket = now_ms // period_ms * period_ms - period_ms
    target_start = last_bucket - (limit - 1) * period_ms
    extra_lookback = max(MIN_HISTORY_BARS, math.ceil(limit * 0.5))
    cursor_ms = target_start - extra_lookback * period_ms
    try:
        collected: dict[int, list[Any]] = {}
        for _ in range(PUBLIC_OHLCV_MAX_PAGES):
            rows = await asyncio.wait_for(
                manager.fetch_ohlcv(
                    exchange_cfg,
                    symbol=account.symbol,
                    timeframe=timeframe,
                    since_ms=cursor_ms,
                    limit=PUBLIC_OHLCV_BATCH_LIMIT,
                ),
                timeout=max(1.0, timeout_seconds),
            )
            valid_rows = [
                row
                for row in rows
                if isinstance(row, (list, tuple)) and len(row) >= 5
            ]
            page_timestamps: list[int] = []
            for row in valid_rows:
                try:
                    timestamp_ms = int(row[0])
                except (TypeError, ValueError):
                    continue
                collected[timestamp_ms] = list(row)
                page_timestamps.append(timestamp_ms)
            if not page_timestamps:
                cursor_ms += PUBLIC_OHLCV_BATCH_LIMIT * period_ms
                if cursor_ms > last_bucket:
                    break
                continue
            latest_ms = max(page_timestamps)
            if latest_ms >= last_bucket:
                break
            next_cursor_ms = latest_ms + period_ms
            if next_cursor_ms <= cursor_ms:
                break
            cursor_ms = next_cursor_ms

        normalized = normalize_ohlcv_rows(list(collected.values()))
        completed = fill_ohlcv_gaps(
            normalized,
            timeframe=timeframe,
            limit=limit,
            now_ms=now_ms,
        )
        if len(completed) < MIN_HISTORY_BARS:
            raise ValueError(
                f"exchange returned only {len(completed)} usable OHLCV bars; "
                f"at least {MIN_HISTORY_BARS} are required"
            )
        return completed
    finally:
        await manager.close()


def _strategy_configs(
    strategy: UserStrategy,
    account: UserExchangeAccount,
) -> tuple[SpotGridConfig | None, DcaConfig | None]:
    parameters = strategy.parameters
    if strategy.strategy_type == "spot_grid":
        return (
            SpotGridConfig(
                enabled=True,
                live_enabled=False,
                exchange=account.exchange,
                symbol=account.symbol,
                lower_price=float(parameters["lower_price"]),
                upper_price=float(parameters["upper_price"]),
                grid_count=int(parameters["grid_count"]),
                spacing=str(parameters["spacing"]),
                quote_per_grid=float(parameters["quote_per_grid"]),
                max_open_orders=int(strategy.risk["max_open_orders"]),
                post_only=True,
            ),
            None,
        )
    if strategy.strategy_type == "dca":
        quote_per_order = float(parameters["quote_per_order"])
        total_quote = float(parameters["total_quote"])
        max_orders = max(1, math.ceil(total_quote / quote_per_order))
        trigger_price = float(parameters["trigger_price"])
        take_profit_pct = float(parameters["take_profit_pct"])
        side = str(parameters["side"])
        take_profit_price = 0.0
        if trigger_price > 0 and take_profit_pct > 0:
            direction = 1.0 if side == "buy" else -1.0
            take_profit_price = trigger_price * (
                1.0 + direction * take_profit_pct / 100.0
            )
        return (
            None,
            DcaConfig(
                enabled=True,
                live_enabled=False,
                exchange=account.exchange,
                symbol=account.symbol,
                side=side,
                trigger_price=trigger_price,
                interval_seconds=float(parameters["interval_seconds"]),
                quote_per_order=quote_per_order,
                size_multiplier=1.0,
                max_orders=max_orders,
                take_profit_price=take_profit_price,
                max_loss_quote=float(strategy.risk["max_daily_loss_quote"]),
                price_mode="taker",
            ),
        )
    raise ValueError(
        "historical backtests currently support Spot Grid and DCA strategies"
    )


class UserBacktestStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_backtest_runs (
                    id TEXT PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_backtest_owner_updated
                    ON user_backtest_runs(owner_email, updated_at DESC);
                """
            )
            rows = connection.execute(
                """
                SELECT id, payload FROM user_backtest_runs
                WHERE status IN ('queued', 'fetching', 'running')
                """
            ).fetchall()
            now = _now()
            for row in rows:
                payload = json.loads(row["payload"])
                payload.update(
                    {
                        "status": "interrupted",
                        "progress_pct": 0.0,
                        "updated_at": now,
                        "finished_at": now,
                        "error": "service restarted before the backtest completed",
                    }
                )
                connection.execute(
                    """
                    UPDATE user_backtest_runs
                    SET status = ?, updated_at = ?, payload = ? WHERE id = ?
                    """,
                    ("interrupted", now, self._dump(payload), row["id"]),
                )
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._ready = True

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_backtest_runs(
                    id, owner_email, project_id, status,
                    created_at, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["owner_email"],
                    payload["project_id"],
                    payload["status"],
                    payload["created_at"],
                    payload["updated_at"],
                    self._dump(payload),
                ),
            )
            connection.commit()
        self.compact(str(payload["owner_email"]))
        return dict(payload)

    def get(self, run_id: str) -> dict[str, Any] | None:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_backtest_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        self._ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM user_backtest_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"backtest run not found: {run_id}")
            payload = json.loads(row["payload"])
            payload.update(changes)
            payload["updated_at"] = _now()
            connection.execute(
                """
                UPDATE user_backtest_runs
                SET status = ?, updated_at = ?, payload = ? WHERE id = ?
                """,
                (
                    payload["status"],
                    payload["updated_at"],
                    self._dump(payload),
                    run_id,
                ),
            )
            connection.commit()
        return payload

    def list(
        self,
        *,
        owner_email: str,
        is_admin: bool,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        self._ensure()
        query = "SELECT payload FROM user_backtest_runs"
        params: tuple[Any, ...]
        if is_admin:
            params = (max(1, limit),)
        else:
            query += " WHERE owner_email = ?"
            params = (owner_email.strip().lower(), max(1, limit))
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def delete(self, run_id: str) -> None:
        self._ensure()
        with self._connect() as connection:
            connection.execute("DELETE FROM user_backtest_runs WHERE id = ?", (run_id,))
            connection.commit()

    def compact(self, owner_email: str) -> None:
        self._ensure()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, status FROM user_backtest_runs
                WHERE owner_email = ? ORDER BY updated_at DESC
                """,
                (owner_email.strip().lower(),),
            ).fetchall()
            stale_ids = [
                row["id"]
                for row in rows[MAX_RUNS_PER_OWNER:]
                if row["status"] not in ACTIVE_STATUSES
            ]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(
                    f"DELETE FROM user_backtest_runs WHERE id IN ({placeholders})",
                    tuple(stale_ids),
                )
                connection.commit()


class UserBacktestService:
    def __init__(
        self,
        workspace_store: UserWorkspaceStore,
        store: UserBacktestStore,
        *,
        fetcher: Callable[..., Awaitable[list[dict[str, float | int]]]] = fetch_public_ohlcv,
        cache_seconds: float = 300.0,
        max_concurrency: int = 2,
    ) -> None:
        self.workspace_store = workspace_store
        self.store = store
        self.fetcher = fetcher
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._cache: dict[
            tuple[str, str, str, str, str, int],
            tuple[float, list[dict[str, float | int]]],
        ] = {}
        self._inflight: dict[
            tuple[str, str, str, str, str, int],
            asyncio.Task[list[dict[str, float | int]]],
        ] = {}
        self._cache_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def _validate_scope(
        self,
        *,
        owner_email: str,
        project_id: str,
        strategy_id: str,
        account_id: str,
    ) -> tuple[dict[str, Any], UserStrategy, UserExchangeAccount]:
        project = self.workspace_store.get_project(project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        if project.owner_email != owner_email:
            raise ValueError("backtest project owner does not match the user")
        if project.status != "active":
            raise ValueError("project must be active before running a backtest")
        strategy = self.workspace_store.get_strategy(strategy_id)
        if strategy is None:
            raise ValueError(f"strategy not found: {strategy_id}")
        if strategy.owner_email != owner_email or strategy.project_id != project_id:
            raise ValueError("backtest strategy is outside the selected project")
        if strategy.strategy_type not in SUPPORTED_STRATEGIES:
            raise ValueError(
                "historical backtests currently support Spot Grid and DCA strategies"
            )
        account = self.workspace_store.get_account(account_id)
        if account is None:
            raise ValueError(f"exchange account not found: {account_id}")
        if account.owner_email != owner_email or account.project_id != project_id:
            raise ValueError("backtest account is outside the selected project")
        if account.id not in strategy.account_ids:
            raise ValueError("select an exchange account assigned to the strategy")
        if account.market_type != "spot" or not account.symbol:
            raise ValueError("historical backtests require a spot trading pair")
        return project.to_dict(), strategy, account

    async def create_run(
        self,
        *,
        owner_email: str,
        project_id: str,
        strategy_id: str,
        account_id: str,
        timeframe: str,
        history_bars: int = DEFAULT_HISTORY_BARS,
        initial_cash: float = 1000.0,
        initial_base: float = 0.0,
        fee_bps: float | None = None,
        slippage_bps: float = 5.0,
        latency_bars: int = 0,
    ) -> dict[str, Any]:
        owner = owner_email.strip().lower()
        active_count = sum(
            1
            for run in self.store.list(
                owner_email=owner,
                is_admin=False,
                limit=MAX_RUNS_PER_OWNER,
            )
            if run.get("status") in ACTIVE_STATUSES
        )
        if active_count >= MAX_ACTIVE_RUNS_PER_OWNER:
            raise ValueError(
                f"at most {MAX_ACTIVE_RUNS_PER_OWNER} backtests may run at once per user"
            )
        project, strategy, account = self._validate_scope(
            owner_email=owner,
            project_id=project_id,
            strategy_id=strategy_id,
            account_id=account_id,
        )
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported backtest timeframe: {timeframe}")
        bars = _bounded_int(
            history_bars,
            label="history bars",
            minimum=MIN_HISTORY_BARS,
            maximum=MAX_HISTORY_BARS,
        )
        cash = _finite_float(initial_cash, label="initial cash")
        base = _finite_float(initial_base, label="initial base")
        if cash <= 0 and base <= 0:
            raise ValueError("initial cash or base must be greater than zero")
        fee = _finite_float(
            strategy.risk["paper_fee_bps"] if fee_bps is None else fee_bps,
            label="fee bps",
            maximum=1_000.0,
        )
        slippage = _finite_float(
            slippage_bps,
            label="slippage bps",
            maximum=10_000.0,
        )
        latency = _bounded_int(
            latency_bars,
            label="latency bars",
            minimum=0,
            maximum=20,
        )
        now = _now()
        run = {
            "id": _new_run_id(),
            "owner_email": owner,
            "project_id": project_id,
            "strategy_id": strategy_id,
            "account_id": account_id,
            "status": "queued",
            "progress_pct": 0.0,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "mode": "research",
            "live_submit_allowed": False,
            "project": project,
            "strategy": strategy.to_dict(),
            "account": account.to_dict(),
            "request": {
                "timeframe": timeframe,
                "history_bars": bars,
                "initial_cash": cash,
                "initial_base": base,
                "fee_bps": fee,
                "slippage_bps": slippage,
                "latency_bars": latency,
            },
            "result": None,
            "error": "",
        }
        self.store.create(run)
        task = asyncio.create_task(self._run(run["id"]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _cached_history(
        self,
        account: UserExchangeAccount,
        *,
        timeframe: str,
        limit: int,
    ) -> tuple[list[dict[str, float | int]], bool]:
        key = (
            account.exchange,
            account.market_type,
            account.api_variant,
            account.symbol,
            timeframe,
            limit,
        )
        now = _now()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] <= self.cache_seconds:
                return [dict(row) for row in cached[1]], True
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self.fetcher(account, timeframe=timeframe, limit=limit)
                )
                self._inflight[key] = task
        try:
            rows = await task
        finally:
            async with self._cache_lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
        async with self._cache_lock:
            self._cache[key] = (_now(), [dict(row) for row in rows])
        return [dict(row) for row in rows], False

    async def _run(self, run_id: str) -> None:
        async with self._semaphore:
            run = self.store.get(run_id)
            if run is None:
                return
            try:
                self.store.update(
                    run_id,
                    status="fetching",
                    progress_pct=10.0,
                    started_at=_now(),
                    error="",
                )
                strategy = UserStrategy.from_dict(run["strategy"])
                account = UserExchangeAccount.from_dict(run["account"])
                request = run["request"]
                bars, cached = await self._cached_history(
                    account,
                    timeframe=str(request["timeframe"]),
                    limit=int(request["history_bars"]),
                )
                self.store.update(
                    run_id,
                    status="running",
                    progress_pct=55.0,
                )
                timestamps = [int(row["timestamp_ms"]) for row in bars]
                prices = [float(row["close"]) for row in bars]
                spot_grid, dca = _strategy_configs(strategy, account)
                backtest_cfg = BacktestConfig(
                    enabled=True,
                    strategy=strategy.strategy_type,
                    exchange=account.exchange,
                    symbol=account.symbol,
                    initial_cash=float(request["initial_cash"]),
                    initial_base=float(request["initial_base"]),
                    fee_bps=float(request["fee_bps"]),
                    slippage_bps=float(request["slippage_bps"]),
                    step_count=len(prices),
                    max_recent_points=min(MAX_HISTORY_BARS, len(prices)),
                    data_source="exchange_ohlcv",
                    latency_steps=int(request["latency_bars"]),
                )
                result = run_paper_backtest(
                    backtest_cfg,
                    spot_grid=spot_grid,
                    dca=dca,
                    price_series=prices,
                    timestamps_ms=timestamps,
                    timeframe_seconds=TIMEFRAME_SECONDS[str(request["timeframe"])],
                    data_source="exchange_ohlcv",
                ).to_dict()
                result["market_data"] = {
                    "exchange": account.exchange,
                    "market_type": account.market_type,
                    "api_variant": account.api_variant,
                    "symbol": account.symbol,
                    "timeframe": request["timeframe"],
                    "requested_bars": request["history_bars"],
                    "received_bars": len(bars),
                    "actual_bars": sum(
                        1 for row in bars if not bool(row.get("gap_filled"))
                    ),
                    "gap_filled_bars": sum(
                        1 for row in bars if bool(row.get("gap_filled"))
                    ),
                    "cached": cached,
                    "first_timestamp_ms": timestamps[0],
                    "last_timestamp_ms": timestamps[-1],
                    "total_volume": sum(float(row["volume"]) for row in bars),
                }
                gap_filled_bars = result["market_data"]["gap_filled_bars"]
                if gap_filled_bars:
                    result["warnings"].append(
                        "no-trade candle intervals were forward-filled with "
                        "the previous close and zero volume"
                    )
                finished = _now()
                self.store.update(
                    run_id,
                    status="complete",
                    progress_pct=100.0,
                    result=result,
                    finished_at=finished,
                    error="",
                )
            except asyncio.CancelledError:
                self.store.update(
                    run_id,
                    status="interrupted",
                    progress_pct=0.0,
                    finished_at=_now(),
                    error="backtest service stopped before completion",
                )
                raise
            except Exception as exc:
                self.store.update(
                    run_id,
                    status="error",
                    progress_pct=0.0,
                    finished_at=_now(),
                    error=f"{exc.__class__.__name__}: {exc}"[:500],
                )

    @staticmethod
    def _summary(run: dict[str, Any]) -> dict[str, Any]:
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        return {
            key: value
            for key, value in run.items()
            if key not in {"result", "strategy", "account", "project"}
        } | {
            "project": {
                "id": run.get("project", {}).get("id"),
                "name": run.get("project", {}).get("name"),
                "symbol": run.get("project", {}).get("symbol"),
            },
            "strategy": {
                "id": run.get("strategy", {}).get("id"),
                "name": run.get("strategy", {}).get("name"),
                "strategy_type": run.get("strategy", {}).get("strategy_type"),
            },
            "account": {
                "id": run.get("account", {}).get("id"),
                "label": run.get("account", {}).get("label"),
                "exchange": run.get("account", {}).get("exchange"),
                "symbol": run.get("account", {}).get("symbol"),
            },
            "metrics": {
                key: result.get(key)
                for key in (
                    "return_pct",
                    "benchmark_return_pct",
                    "excess_return_pct",
                    "max_drawdown_pct",
                    "sharpe_ratio",
                    "trade_count",
                )
            },
        }

    def public_payload(
        self,
        *,
        owner_email: str,
        is_admin: bool,
        run_id: str = "",
    ) -> dict[str, Any]:
        runs = self.store.list(
            owner_email=owner_email,
            is_admin=is_admin,
            limit=30,
        )
        selected = None
        if run_id:
            candidate = self.store.get(run_id)
            if candidate is not None and (
                is_admin or candidate.get("owner_email") == owner_email.strip().lower()
            ):
                selected = candidate
        elif runs:
            selected = runs[0]
        active_count = sum(
            1
            for run in runs
            if run.get("status") in ACTIVE_STATUSES
        )
        return {
            "status": "ok",
            "mode": "research",
            "live_submit_allowed": False,
            "timeframes": [
                {"id": key, "seconds": value}
                for key, value in TIMEFRAME_SECONDS.items()
            ],
            "limits": {
                "min_history_bars": MIN_HISTORY_BARS,
                "max_history_bars": MAX_HISTORY_BARS,
                "max_runs_per_owner": MAX_RUNS_PER_OWNER,
                "max_active_runs_per_owner": MAX_ACTIVE_RUNS_PER_OWNER,
            },
            "active_count": active_count,
            "runs": [self._summary(run) for run in runs],
            "selected": selected,
        }

    def delete_run(
        self,
        run_id: str,
        *,
        owner_email: str,
        is_admin: bool,
    ) -> None:
        run = self.store.get(run_id)
        if run is None:
            raise ValueError(f"backtest run not found: {run_id}")
        if not is_admin and run.get("owner_email") != owner_email.strip().lower():
            raise PermissionError("backtest run belongs to another user")
        if run.get("status") in ACTIVE_STATUSES:
            raise ValueError("wait for the backtest to finish before deleting it")
        self.store.delete(run_id)

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        async with self._cache_lock:
            inflight = list(self._inflight.values())
            self._inflight.clear()
            self._cache.clear()
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
