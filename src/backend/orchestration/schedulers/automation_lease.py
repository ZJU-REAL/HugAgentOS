"""Renewable execution ownership; lease expiry is never a task duration limit."""

import asyncio
import uuid

from core.infra import ephemeral

# Only bounds recovery after a dead worker. Healthy executions renew indefinitely.
LEASE_TTL_SECONDS = 60
RENEW_INTERVAL_SECONDS = 15


class AutomationLease:
    def __init__(self, key):
        self.key = key
        self.token = uuid.uuid4().hex
        self.state = ephemeral.get_ephemeral_state()
        self.renewal = None

    async def acquire(self):
        if not await self.state.hold(self.key, self.token, ttl=LEASE_TTL_SECONDS):
            return False
        self.renewal = asyncio.create_task(self._renew())
        return True

    async def _renew(self):
        while True:
            await asyncio.sleep(RENEW_INTERVAL_SECONDS)
            # Unlike hold(), CAS cannot reacquire an expired lease after another
            # worker could already have recovered the original running record.
            if not await self.state.compare_exchange(
                self.key, self.token, self.token, ttl=LEASE_TTL_SECONDS
            ):
                return

    async def run(self, awaitable):
        execution = asyncio.create_task(awaitable)
        try:
            done, _ = await asyncio.wait(
                (execution, self.renewal), return_when=asyncio.FIRST_COMPLETED
            )
            if self.renewal in done:
                # Never leave the child conversation running without ownership.
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                self.renewal.result()
                raise RuntimeError("定时任务执行锁已失效")
            return await execution
        finally:
            if not execution.done():
                execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    async def release(self):
        if self.renewal:
            self.renewal.cancel()
            await asyncio.gather(self.renewal, return_exceptions=True)
        await self.state.release(self.key, self.token)
