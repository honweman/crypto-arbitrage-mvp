from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from aiohttp import web


LOGGER = logging.getLogger(__name__)
TaskFactory = Callable[[], Awaitable[None]]
RecoveryCallback = Callable[[], Awaitable[Mapping[str, Any]]]
FailureCallback = Callable[[str], Awaitable[None]]
StartupGuardCallback = Callable[[], Awaitable[None]]


class RuntimeLeaderLease:
    """Single-host process lease that prevents duplicate trading loops."""

    def __init__(self, path: str | Path, *, release_id: str = "") -> None:
        self.path = Path(path)
        self.release_id = str(
            release_id or os.environ.get("CRYPTO_ARB_RELEASE_ID") or "local"
        )
        self._handle: Any | None = None
        self.acquired_at: float | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        acquired_at = time.time()
        handle.seek(0)
        handle.truncate()
        handle.write(
            f"pid={os.getpid()} host={socket.gethostname()} "
            f"release={self.release_id} acquired_at={acquired_at:.6f}\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self.acquired_at = acquired_at
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self.acquired_at = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "acquired": self.acquired,
            "acquired_at": self.acquired_at,
            "release_id": self.release_id,
            "pid": os.getpid(),
        }


class RuntimeSupervisor:
    """Own the runtime lease and start trading loops only on the leader."""

    def __init__(
        self,
        lease: RuntimeLeaderLease,
        *,
        task_factories: Mapping[str, TaskFactory],
        recover_orders: RecoveryCallback,
        on_failure: FailureCallback,
        startup_guard: StartupGuardCallback | None = None,
        enforce_leader_writes: bool = False,
        acquire_poll_seconds: float = 0.2,
    ) -> None:
        self.lease = lease
        self.task_factories = dict(task_factories)
        self.recover_orders = recover_orders
        self.on_failure = on_failure
        self.startup_guard = startup_guard
        self.enforce_leader_writes = bool(enforce_leader_writes)
        self.acquire_poll_seconds = max(0.05, float(acquire_poll_seconds))
        self.role = "initializing"
        self.error: str | None = None
        self.process_ready = False
        self.leader_ready = False
        self.started_at = time.time()
        self.leader_started_at: float | None = None
        self.recovery: dict[str, Any] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.guard_task: asyncio.Task[None] | None = None

    async def _notify_failure(self, reason: str) -> None:
        try:
            await self.on_failure(reason)
        except Exception:  # noqa: BLE001
            LOGGER.exception("runtime failure callback failed")

    async def _run_startup_guard(self) -> None:
        if self.startup_guard is None:
            return
        try:
            result = self.startup_guard()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("startup configuration guard failed")

    async def _cancel_runtime_tasks(self) -> None:
        if self.guard_task is not None:
            self.guard_task.cancel()
        for task in self.tasks.values():
            task.cancel()
        pending: list[asyncio.Task[Any]] = [*self.tasks.values()]
        if self.guard_task is not None:
            pending.append(self.guard_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.guard_task = None

    @staticmethod
    def _task_failure(name: str, task: asyncio.Task[None]) -> str:
        if task.cancelled():
            return f"runtime task exited unexpectedly: {name} (cancelled)"
        exception = task.exception()
        if exception is None:
            return f"runtime task exited unexpectedly: {name}"
        return (
            f"runtime task failed: {name}: {exception.__class__.__name__}: {exception}"
        )

    async def run(self) -> None:
        self.process_ready = True
        try:
            while not self.lease.try_acquire():
                self.role = "standby"
                await asyncio.sleep(self.acquire_poll_seconds)

            self.role = "leader_starting"
            self.leader_started_at = time.time()
            try:
                self.recovery = dict(await self.recover_orders())
                self.tasks = {
                    name: asyncio.create_task(factory(), name=f"runtime:{name}")
                    for name, factory in self.task_factories.items()
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = f"runtime startup failed: {exc.__class__.__name__}: {exc}"
                self.role = "error"
                await self._notify_failure(self.error)
                await asyncio.Event().wait()
                return

            self.role = "leader"
            self.leader_ready = True
            if self.startup_guard is not None:
                self.guard_task = asyncio.create_task(
                    self._run_startup_guard(),
                    name="runtime:startup-config-guard",
                )

            done, _ = await asyncio.wait(
                self.tasks.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            failed = next(iter(done))
            failed_name = next(
                name for name, task in self.tasks.items() if task is failed
            )
            self.error = self._task_failure(failed_name, failed)
            self.leader_ready = False
            self.role = "error"
            await self._notify_failure(self.error)
            await self._cancel_runtime_tasks()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        finally:
            self.leader_ready = False
            await self._cancel_runtime_tasks()
            self.lease.release()
            if self.role != "error":
                self.role = "stopped"

    def status(self) -> dict[str, Any]:
        task_status = {
            name: "running" if not task.done() else "stopped"
            for name, task in self.tasks.items()
        }
        mutation_allowed = not self.enforce_leader_writes or (
            self.role == "leader" and self.leader_ready and not self.error
        )
        deployment_ready = bool(
            self.process_ready and not self.error and self.role in {"standby", "leader"}
        )
        lease = self.lease.status()
        return {
            "process_ready": self.process_ready,
            "deployment_ready": deployment_ready,
            "role": self.role,
            "leader_ready": self.leader_ready,
            "mutation_allowed": mutation_allowed,
            "enforce_leader_writes": self.enforce_leader_writes,
            "error": self.error,
            "started_at": self.started_at,
            "leader_started_at": self.leader_started_at,
            "release_id": lease["release_id"],
            "pid": lease["pid"],
            "tasks": task_status,
            "order_recovery": {
                "unresolved_count": int(self.recovery.get("unresolved_count") or 0),
                "recovered_count": int(self.recovery.get("recovered_count") or 0),
            },
        }


def zero_downtime_enabled() -> bool:
    value = str(os.environ.get("CRYPTO_ARB_ZERO_DOWNTIME") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


@web.middleware
async def deployment_mutation_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    supervisor = request.app.get("runtime_supervisor")
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.path.startswith("/api/")
        and isinstance(supervisor, RuntimeSupervisor)
        and not supervisor.status()["mutation_allowed"]
    ):
        return web.json_response(
            {
                "error": "service is warming up on a standby deployment; retry shortly",
                "deployment": supervisor.status(),
            },
            status=503,
            headers={"Retry-After": "1"},
        )
    return await handler(request)


__all__ = [
    "RuntimeLeaderLease",
    "RuntimeSupervisor",
    "deployment_mutation_middleware",
    "zero_downtime_enabled",
]
