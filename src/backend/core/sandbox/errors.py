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
