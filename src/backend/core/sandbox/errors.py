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
