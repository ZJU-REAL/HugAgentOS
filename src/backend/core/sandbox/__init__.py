"""Sandbox driver abstraction layer.

Callers only depend on ``get_sandbox_provider()`` + ``ProcessRequest``;
the concrete implementation is decided by the environment variable ``SANDBOX_PROVIDER``.
"""

from .errors import SandboxConnectError, SandboxError, SandboxFileTooLargeError, SandboxTimeoutError
from .factory import get_sandbox_provider, reset_provider_cache
from .protocol import (
    ProcessRequest,
    ProcessResult,
    SandboxProvider,
)

__all__ = [
    "ProcessRequest",
    "ProcessResult",
    "SandboxConnectError",
    "SandboxError",
    "SandboxFileTooLargeError",
    "SandboxProvider",
    "SandboxTimeoutError",
    "get_sandbox_provider",
    "reset_provider_cache",
]
