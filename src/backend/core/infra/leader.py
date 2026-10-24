"""Lease-owned background work and fail-closed worker startup barriers."""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import socket
import uuid
from typing import Awaitable, Callable

from core.infra.ephemeral import get_ephemeral_state
from core.infra.logging import get_logger
from core.infra.worker_count import resolve as resolved_workers

logger = get_logger(__name__)
_LEASE_SECONDS = 45
_POLL_SECONDS = 0.2
Callback = Callable[[], Awaitable[None]]


def _token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def coordination_required() -> bool:
    return resolved_workers() > 1


class Leadership:
    """Stop owned work before releasing the token; uncertainty revokes ownership."""

    def __init__(self, role: str, *, lease_seconds: int = _LEASE_SECONDS) -> None:
        self._key = f"jx:leader:{role}"
        self._role = role
        self._token = _token()
        self._lease = int(lease_seconds)
        self._elected = False
        self._task: asyncio.Task | None = None
        self._work: asyncio.Task | None = None
        self._on_deposed: Callback | None = None

    async def _hold(self) -> bool:
        if not coordination_required():
            return True
        try:
            state = get_ephemeral_state()
            # Renewal must never recreate an expired lease.
            operation = (
                state.compare_exchange(self._key, self._token, self._token, ttl=self._lease)
                if self._elected
                else state.hold(self._key, self._token, ttl=self._lease)
            )
            return await asyncio.wait_for(operation, timeout=self._lease / 3)
        except Exception as exc:
            logger.warning("leader_hold_failed", role=self._role, error=str(exc))
            return False

    async def _depose(self) -> None:
        if not self._elected:
            return
        if self._work is not None:
            if not self._work.done():
                self._work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._work
            self._work = None
        # A failed stop must not release ownership or permit a new local start.
        if self._on_deposed is not None:
            await self._on_deposed()
        self._elected = False
        if coordination_required():
            try:
                await get_ephemeral_state().release(self._key, self._token)
            except Exception as exc:
                logger.warning("leader_release_failed", role=self._role, error=str(exc))

    async def _supervise(self, on_elected: Callback) -> None:
        while True:
            holding = await self._hold()
            failed = self._work is not None and self._work.done() and (
                self._work.cancelled() or self._work.exception() is not None
            )
            if self._elected and (not holding or failed):
                logger.warning("leader_deposed", role=self._role, startup_failed=failed)
                await self._depose()
            elif holding and not self._elected:
                self._elected = True
                self._work = asyncio.create_task(on_elected(), name=f"leader_work:{self._role}")
            await asyncio.sleep(self._lease / 3)

    def start(self, *, on_elected: Callback, on_deposed: Callback) -> None:
        if self._task is not None:
            raise RuntimeError("Leadership already started")
        self._on_deposed = on_deposed
        self._task = asyncio.create_task(self._supervise(on_elected), name=f"leader:{self._role}")

    async def stop(self) -> None:
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._depose()


async def run_once(
    name: str, step: Callback, *, timeout: float = 300.0
) -> None:
    """All workers observe success, failure or timeout of required initialization."""
    if not coordination_required():
        await step()
        return
    state = get_ephemeral_state()
    boot = f"jx:boot:{socket.gethostname()}:{os.getppid()}:{name}"
    lock, done = f"{boot}:lock", f"{boot}:done"
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        outcome = await state.get(done)
        if outcome == "ok":
            return
        if outcome is not None:
            raise RuntimeError(f"Required startup step failed: {name}")
        if await state.claim(lock, ttl=max(1, math.ceil(timeout) + 1)):
            try:
                await asyncio.wait_for(step(), timeout=timeout)
            except BaseException:
                await state.put(done, "failed", ttl=max(60, math.ceil(timeout) * 2))
                raise
            await state.put(done, "ok", ttl=max(60, math.ceil(timeout) * 2))
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Required startup step timed out: {name}")
        await asyncio.sleep(_POLL_SECONDS)


__all__ = ["Leadership", "coordination_required", "run_once"]
