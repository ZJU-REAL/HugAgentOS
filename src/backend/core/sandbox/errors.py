"""Unified exception types for sandbox drivers.

Each Provider implementation is responsible for mapping low-level exceptions
(httpx.TimeoutException / opensandbox.SandboxException, etc.) to the unified
types here, so callers handle them through a uniform interface.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Base exception for sandbox execution."""


class SandboxTimeoutError(SandboxError):
    """Script execution or HTTP call timed out."""


class SandboxConnectError(SandboxError):
    """Cannot connect to the sandbox service (container not started / network unreachable / health check failed)."""


class SandboxFileTooLargeError(SandboxError):
    """A sandbox file exceeds the configured artifact export limit."""

    def __init__(self, *, actual_size: int, max_size: int) -> None:
        self.actual_size = max(0, int(actual_size))
        self.max_size = max(1, int(max_size))
        super().__init__(f"文件过大: {self.actual_size} bytes > {self.max_size} bytes")


class SandboxCapacityExhausted(SandboxError):
    """No capacity slot (or no creation gate, ``queue_full``) became free in time.

    Slot exhaustion is reported only after a reconciliation that completed after
    the request began.
    """

    def __init__(self, *, limit: int, background: bool = False, queue_full: bool = False) -> None:
        self.limit = limit
        self.background = background
        self.queue_full = queue_full
        super().__init__(
            "沙箱创建排队超时：同时创建的沙箱过多。请稍后重试" if queue_full else
            f"沙箱资源已满（上限 {limit} 个）。请稍后重试，或关闭不再使用的会话以释放资源"
        )


class SandboxNotFoundError(SandboxError):
    """The controller explicitly confirmed that the sandbox no longer exists."""
