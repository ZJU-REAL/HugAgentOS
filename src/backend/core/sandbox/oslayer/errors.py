"""Errors raised by the OS sandbox layer.

The layer never degrades silently. Every reason a command cannot be confined is
an exception the caller has to handle explicitly, so "the sandbox was not
applied" can never be the quiet outcome of a missing runner or of a policy the
platform backend has no mechanism for.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Base class for every OS sandbox failure."""


class SandboxUnavailableError(SandboxError):
    """The platform backend itself cannot run here.

    Raised when the host has no backend at all, or when the backend's runner
    (``sandbox-exec``, ``bwrap``, the Windows launcher interpreter) is missing.
    """


class SandboxUnenforceableError(SandboxError):
    """The backend exists but cannot enforce the requested policy.

    Raised when a policy asks for a restriction the platform backend has no
    mechanism for — the Windows restricted-token backend and read denials, for
    example. Refusing is the only correct answer: running anyway would report a
    confinement that is not there.
    """


class SandboxPolicyError(SandboxError):
    """The policy is malformed or cannot be resolved against its context."""


__all__ = [
    "SandboxError",
    "SandboxPolicyError",
    "SandboxUnavailableError",
    "SandboxUnenforceableError",
]
