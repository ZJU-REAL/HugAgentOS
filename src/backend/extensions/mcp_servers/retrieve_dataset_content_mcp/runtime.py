"""Bounded execution of blocking knowledge-base operations."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict


def read_positive_float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def read_positive_int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


class BlockingLane:
    """Run synchronous work in a dedicated, bounded executor.

    A timed-out caller does not release its slot until the underlying thread
    really finishes. This prevents repeated timeouts from creating an
    unbounded queue of orphaned blocking work.
    """

    def __init__(self, *, name: str, max_workers: int) -> None:
        self._name = name
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"kb-{name}",
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self._max_workers)
        return self._semaphore

    async def run(self, func, *, timeout: float):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        semaphore = self._get_semaphore()

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{self._name} 等待执行槽超过 {timeout:.0f}s") from exc

        try:
            future = loop.run_in_executor(self._executor, func)
        except Exception:
            semaphore.release()
            raise

        # Keep the slot occupied after caller timeout/cancellation until the
        # synchronous operation exits, so executor pressure always stays bounded.
        future.add_done_callback(lambda _: semaphore.release())
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"{self._name} 执行超过 {timeout:.0f}s")

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{self._name} 执行超过 {timeout:.0f}s") from exc


def tool_timeout_payload(
    *, tool: str, timeout: float, message: str | None = None
) -> Dict[str, Any]:
    return {
        "items": [],
        "error": {
            "code": "tool_timeout",
            "tool": tool,
            "message": message or f"检索超过 {timeout:.0f} 秒，已停止等待",
            "retryable": True,
        },
    }
