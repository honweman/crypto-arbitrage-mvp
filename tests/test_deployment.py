from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from arbitrage_bot.web.deployment import (
    RuntimeLeaderLease,
    RuntimeSupervisor,
    deployment_mutation_middleware,
)


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


class RuntimeLeaderLeaseTest(unittest.TestCase):
    def test_only_one_lease_can_hold_the_runtime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.lock"
            first = RuntimeLeaderLease(path, release_id="first")
            second = RuntimeLeaderLease(path, release_id="second")

            self.assertTrue(first.try_acquire())
            self.assertFalse(second.try_acquire())
            first.release()
            self.assertTrue(second.try_acquire())
            second.release()


class RuntimeSupervisorTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _standby_supervisor(lock_path: Path) -> RuntimeSupervisor:
        supervisor = RuntimeSupervisor(
            RuntimeLeaderLease(lock_path),
            task_factories={},
            recover_orders=lambda: asyncio.sleep(0, result={"unresolved_count": 0}),
            on_failure=lambda _: asyncio.sleep(0),
            enforce_leader_writes=True,
        )
        supervisor.process_ready = True
        supervisor.role = "standby"
        return supervisor

    async def test_standby_rejects_api_mutations_until_it_is_leader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._standby_supervisor(Path(tmp) / "runtime.lock")
            app = web.Application()
            app["runtime_supervisor"] = supervisor
            request = make_mocked_request("POST", "/api/control", app=app)

            blocked = await deployment_mutation_middleware(
                request,
                lambda _: asyncio.sleep(0, result=web.json_response({"ok": True})),
            )
            self.assertEqual(blocked.status, 503)
            self.assertEqual(blocked.headers["Retry-After"], "1")

            supervisor.role = "leader"
            supervisor.leader_ready = True
            allowed = await deployment_mutation_middleware(
                request,
                lambda _: asyncio.sleep(0, result=web.json_response({"ok": True})),
            )
            self.assertEqual(allowed.status, 200)

    async def test_standby_takes_over_only_after_the_leader_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "runtime.lock"
            active = RuntimeLeaderLease(lock_path, release_id="active")
            self.assertTrue(active.try_acquire())
            started = asyncio.Event()

            async def runtime_loop() -> None:
                started.set()
                await asyncio.Event().wait()

            failures: list[str] = []
            supervisor = RuntimeSupervisor(
                RuntimeLeaderLease(lock_path, release_id="candidate"),
                task_factories={"monitor": runtime_loop},
                recover_orders=lambda: asyncio.sleep(0, result={"unresolved_count": 0}),
                on_failure=lambda reason: asyncio.sleep(
                    0, result=failures.append(reason)
                ),
                enforce_leader_writes=True,
                acquire_poll_seconds=0.01,
            )
            supervisor_task = asyncio.create_task(supervisor.run())
            try:
                await _wait_for(lambda: supervisor.role == "standby")
                self.assertFalse(started.is_set())
                self.assertFalse(supervisor.status()["mutation_allowed"])

                active.release()
                await asyncio.wait_for(started.wait(), timeout=2.0)
                self.assertEqual(supervisor.role, "leader")
                self.assertTrue(supervisor.status()["leader_ready"])
                self.assertTrue(supervisor.status()["mutation_allowed"])
                self.assertEqual(failures, [])
            finally:
                supervisor_task.cancel()
                await asyncio.gather(supervisor_task, return_exceptions=True)
                active.release()

    async def test_runtime_task_failure_auto_stops_and_keeps_error_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            failures: list[str] = []

            async def failing_loop() -> None:
                await asyncio.sleep(0)
                raise RuntimeError("loop failed")

            supervisor = RuntimeSupervisor(
                RuntimeLeaderLease(Path(tmp) / "runtime.lock"),
                task_factories={"monitor": failing_loop},
                recover_orders=lambda: asyncio.sleep(0, result={"unresolved_count": 0}),
                on_failure=lambda reason: asyncio.sleep(
                    0, result=failures.append(reason)
                ),
                enforce_leader_writes=True,
            )
            supervisor_task = asyncio.create_task(supervisor.run())
            try:
                await _wait_for(lambda: supervisor.role == "error")
                self.assertFalse(supervisor.status()["leader_ready"])
                self.assertFalse(supervisor.status()["mutation_allowed"])
                self.assertIn("RuntimeError: loop failed", supervisor.error or "")
                self.assertEqual(len(failures), 1)
            finally:
                supervisor_task.cancel()
                await asyncio.gather(supervisor_task, return_exceptions=True)
