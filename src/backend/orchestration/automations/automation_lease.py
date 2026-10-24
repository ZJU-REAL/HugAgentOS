"""Renewable ownership with cooperative stop and bounded token-safe release."""

import asyncio
import uuid

from core.infra import ephemeral
from core.infra.background import spawn
from core.infra.logging import get_logger

logger = get_logger(__name__)
LEASE_TTL_SECONDS = 60
RENEW_INTERVAL_SECONDS = 15
RELEASE_WAIT_SECONDS = 2


class AutomationLease:
    def __init__(self, key):
        self.key = key
        self.token = uuid.uuid4().hex
        self.state = ephemeral.get_ephemeral_state()
        self.renewal = None
        self.execution = None
        self._stop = asyncio.Event()
        self._release_task = None

    async def acquire(self):
        if not await self.state.hold(self.key, self.token, ttl=LEASE_TTL_SECONDS):
            return False
        self.renewal = spawn(self._renew(), name="automation.lease.renew")
        return True

    def _finished(self):
        return self._stop.is_set() or (self.execution is not None and self.execution.done())

    async def _renew(self):
        stopped = asyncio.create_task(self._stop.wait())
        attempt = None
        try:
            while not self._finished():
                await asyncio.wait({stopped}, timeout=RENEW_INTERVAL_SECONDS)
                if self._finished():
                    return
                # CAS cannot recreate a deleted key or replace another token.
                attempt = spawn(
                    self.state.compare_exchange(
                        self.key, self.token, self.token, ttl=LEASE_TTL_SECONDS
                    ), name="automation.lease.cas",
                )
                done, _ = await asyncio.wait({attempt}, timeout=RENEW_INTERVAL_SECONDS)
                if not done or not attempt.result():
                    return
                if self._finished():
                    return
        finally:
            if attempt is not None and not attempt.done():
                attempt.cancel()
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def run(self, awaitable):
        execution = self.execution = asyncio.create_task(awaitable)
        # This also stops renewal if an outer release path is never reached.
        execution.add_done_callback(lambda _: self._stop.set())
        try:
            done, _ = await asyncio.wait(
                (execution, self.renewal), return_when=asyncio.FIRST_COMPLETED
            )
            if execution in done:
                return await execution
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            self.renewal.result()
            raise RuntimeError("定时任务执行锁已失效")
        finally:
            if not execution.done():
                execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    async def owns(self):
        if self._finished():
            return False
        read = spawn(self.state.get(self.key), name="automation.lease.check")
        done, _ = await asyncio.wait({read}, timeout=RELEASE_WAIT_SECONDS)
        if not done:
            read.cancel()
            return False
        return not self._finished() and read.result() == self.token

    async def release(self):
        self._stop.set()
        if self._release_task is None:
            self._release_task = spawn(self._release(), name="automation.lease.release")
        # Concurrent callers share cleanup; caller cancellation cannot abort it.
        await asyncio.shield(self._release_task)

    async def _release(self):
        try:
            if self.renewal:
                self.renewal.cancel()
                _, pending = await asyncio.wait({self.renewal}, timeout=RELEASE_WAIT_SECONDS)
                if pending:
                    logger.error("automation_renewal_stop_timeout", key=self.key)
        finally:
            # Always delete by token, including when renewal ignores cancellation.
            # A late CAS observes deletion/new ownership and cannot resurrect us.
            deletion = spawn(
                self.state.release(self.key, self.token), name="automation.lease.delete"
            )
            _, pending = await asyncio.wait({deletion}, timeout=RELEASE_WAIT_SECONDS)
            if pending:
                logger.error("automation_lock_delete_timeout", key=self.key)
            else:
                deletion.result()
