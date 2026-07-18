"""Sandbox driver abstraction layer.

Callers only depend on ``get_sandbox_provider()`` + ``ExecuteRequest`` / ``StageFile``;
the concrete implementation is decided by the environment variable ``SANDBOX_PROVIDER``.
"""

from .errors import SandboxConnectError, SandboxError, SandboxTimeoutError
from .factory import get_sandbox_provider, reset_provider_cache
from .protocol import (
    ExecuteRequest,
    ExecuteResult,
    SandboxFile,
    SandboxProvider,
    StageFile,
    StagedFile,
)
from .script_runner_provider import result_to_dict

__all__ = [
    "ExecuteRequest",
    "ExecuteResult",
    "SandboxConnectError",
    "SandboxError",
    "SandboxFile",
    "SandboxProvider",
    "SandboxTimeoutError",
    "StageFile",
    "StagedFile",
    "get_sandbox_provider",
    "reset_provider_cache",
    "result_to_dict",
]
