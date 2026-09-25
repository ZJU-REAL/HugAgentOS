"""Sandbox driver abstraction layer.

Callers only depend on ``get_sandbox_provider()`` + ``ProcessRequest`` / ``StageFile``;
the concrete implementation is decided by the environment variable ``SANDBOX_PROVIDER``.
"""

from .errors import SandboxConnectError, SandboxError, SandboxFileTooLargeError, SandboxTimeoutError
from .factory import get_sandbox_provider, reset_provider_cache
from .protocol import (
    ProcessRequest,
    ProcessResult,
    SandboxProvider,
    StagedFile,
    StageFile,
)

__all__ = [
    "ProcessRequest",
    "ProcessResult",
    "SandboxConnectError",
    "SandboxError",
    "SandboxFileTooLargeError",
    "SandboxProvider",
    "SandboxTimeoutError",
    "StageFile",
    "StagedFile",
    "get_sandbox_provider",
    "reset_provider_cache",
]
