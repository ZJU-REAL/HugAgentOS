"""Per-invocation execution time, excluding time spent waiting for a person."""
import asyncio
from contextlib import contextmanager


class InvocationBudget:
    """One task deadline; overlapping human waits pause the same clock once."""

    def __init__(self, seconds: float):
        self._timeout = asyncio.timeout(seconds)
        self._remaining = seconds
        self._waiters = 0
        self._active = False

    async def __aenter__(self):
        await self._timeout.__aenter__()
        self._active = True
        return self

    async def __aexit__(self, *exc):
        self._active = False
        return await self._timeout.__aexit__(*exc)

    @contextmanager
    def waiting_for_user(self):
        # A late callback must not resurrect a completed or already expired call.
        if not self._active or self._timeout.expired():
            raise TimeoutError("MCP invocation is no longer active")
        if self._waiters == 0:
            self._remaining = self._timeout.when() - asyncio.get_running_loop().time()
            if self._remaining <= 0:
                raise TimeoutError("MCP execution budget exhausted")
            self._timeout.reschedule(None)
        self._waiters += 1
        try:
            yield
        finally:
            self._waiters -= 1
            if self._waiters == 0 and self._active and not self._timeout.expired():
                self._timeout.reschedule(asyncio.get_running_loop().time() + self._remaining)
