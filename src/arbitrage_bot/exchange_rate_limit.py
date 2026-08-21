from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


# This account currently reports Gate's `(012)3/10` UID restriction. A small
# margin above 10 / 3 seconds keeps rolling-window bursts below that limit.
GATE_SPOT_ORDER_INTERVAL_SECONDS = 3.5
GATE_SPOT_RATE_LIMIT_COOLDOWN_SECONDS = 10.5


@dataclass(frozen=True)
class RequestPacingPolicy:
    interval_seconds: float
    cooldown_seconds: float


def exchange_request_pacing_policy(
    exchange_cfg: Any,
    *,
    operation: str,
) -> RequestPacingPolicy | None:
    if (
        str(getattr(exchange_cfg, "id", "")).lower() == "gateio"
        and str(getattr(exchange_cfg, "market_type", "spot")).lower() == "spot"
        and operation == "create_order"
    ):
        return RequestPacingPolicy(
            interval_seconds=GATE_SPOT_ORDER_INTERVAL_SECONDS,
            cooldown_seconds=GATE_SPOT_RATE_LIMIT_COOLDOWN_SECONDS,
        )
    return None


class AsyncRequestPacer:
    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def wait(self) -> float:
        async with self._lock:
            delay = max(0.0, self._next_allowed_at - self._clock())
            if delay:
                await self._sleeper(delay)
            self._next_allowed_at = self._clock() + self.interval_seconds
            return delay

    async def defer(self, seconds: float) -> None:
        async with self._lock:
            self._next_allowed_at = max(
                self._next_allowed_at,
                self._clock() + max(0.0, float(seconds)),
            )


_PACERS_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, str], AsyncRequestPacer],
] = weakref.WeakKeyDictionary()


def _request_pacer(
    exchange_cfg: Any,
    *,
    operation: str,
    policy: RequestPacingPolicy,
) -> AsyncRequestPacer:
    loop = asyncio.get_running_loop()
    pacers = _PACERS_BY_LOOP.setdefault(loop, {})
    key = (str(getattr(exchange_cfg, "key", "")), operation)
    pacer = pacers.get(key)
    if pacer is None or pacer.interval_seconds != policy.interval_seconds:
        pacer = AsyncRequestPacer(policy.interval_seconds)
        pacers[key] = pacer
    return pacer


async def pace_exchange_request(exchange_cfg: Any, *, operation: str) -> float:
    policy = exchange_request_pacing_policy(exchange_cfg, operation=operation)
    if policy is None:
        return 0.0
    return await _request_pacer(
        exchange_cfg,
        operation=operation,
        policy=policy,
    ).wait()


async def defer_exchange_request(exchange_cfg: Any, *, operation: str) -> None:
    policy = exchange_request_pacing_policy(exchange_cfg, operation=operation)
    if policy is None:
        return
    await _request_pacer(
        exchange_cfg,
        operation=operation,
        policy=policy,
    ).defer(policy.cooldown_seconds)


def is_exchange_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return "ratelimit" in name or any(
        token in message
        for token in (
            "too_many_requests",
            "rate limit exceeded",
            "request rate limit",
            "http 429",
        )
    )
