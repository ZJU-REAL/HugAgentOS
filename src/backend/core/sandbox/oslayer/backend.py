"""Backend contract and platform selection for the OS sandbox layer.

Every platform backend answers the same three questions, and the answers are
kept separate on purpose:

``unavailable_reason``
    Can this backend run on this host at all? (Runner installed, kernel feature
    present.)
``unenforceable_reason``
    Given this specific policy, can the backend enforce *everything* it asks
    for? A backend that would have to ignore part of the policy says so here
    instead of quietly enforcing the rest.
``build``
    Turn the policy into a concrete launch — an argv prefix and an environment
    overlay — that the process-spawning boundary applies verbatim.

Splitting the probes from the build is what lets the permission layer decide
what an unenforceable policy means (refuse the command, or ask the user to widen
the preset) rather than having each backend invent its own answer.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol

from .errors import SandboxUnavailableError, SandboxUnenforceableError
from .policy import PolicyContext, ResolvedFileSystem, SandboxPolicy


@dataclass(frozen=True)
class SandboxLaunch:
    """How to start a confined process.

    Two forms, because the platforms genuinely differ:

    ``argv_prefix`` (+ ``env``)
        The command is wrapped. ``sandbox-exec`` and ``bwrap`` are programs that
        take the real command as their tail, so the confinement *is* an argv
        prefix and the spawning boundary needs to know nothing about it.
    ``spawn_plan``
        The command is started differently. Windows confinement lives in an
        access token that must be attached at ``CreateProcessAsUser`` time;
        there is no command that can wrap another command into it. The plan
        carries what the token needs, and the spawning boundary hands it to
        :func:`core.sandbox.oslayer.windows_runtime.spawn_confined`.

    Exactly one form is populated. A boundary that receives a ``spawn_plan`` it
    cannot honour must refuse — ignoring it would run the command unconfined
    while every layer above believed otherwise.
    """

    backend: str
    argv_prefix: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    spawn_plan: Optional[Mapping[str, object]] = None

    def to_json(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "argv_prefix": list(self.argv_prefix),
            "env": dict(self.env),
            "spawn_plan": dict(self.spawn_plan) if self.spawn_plan is not None else None,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> "SandboxLaunch":
        plan = payload.get("spawn_plan")
        return cls(
            backend=str(payload.get("backend") or ""),
            argv_prefix=tuple(str(item) for item in payload.get("argv_prefix") or ()),
            env={
                str(key): str(value)
                for key, value in dict(payload.get("env") or {}).items()  # type: ignore[arg-type]
            },
            spawn_plan=dict(plan) if isinstance(plan, Mapping) else None,
        )


class SandboxBackend(Protocol):
    """A platform's filesystem+network confinement mechanism."""

    name: str

    def unavailable_reason(self) -> str:
        """Why this backend cannot run here, or ``""`` when it can."""

    def unenforceable_reason(self, policy: SandboxPolicy, context: PolicyContext) -> str:
        """Why this backend cannot fully enforce ``policy``, or ``""`` when it can."""

    def build(self, policy: SandboxPolicy, context: PolicyContext) -> SandboxLaunch:
        """Produce the launch for ``policy``; callers must have checked the probes."""


class BasePlatformBackend:
    """Shared plumbing: resolve once, enforce the probes, then delegate.

    Subclasses implement :meth:`_build_launch` against an already-resolved
    filesystem view, so none of them repeats policy resolution or probe
    handling — and none of them can accidentally skip a probe.
    """

    name = ""

    def unavailable_reason(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def unenforceable_reason(self, policy: SandboxPolicy, context: PolicyContext) -> str:
        return self._unenforceable(policy.filesystem.resolve(context), policy)

    def build(self, policy: SandboxPolicy, context: PolicyContext) -> SandboxLaunch:
        unavailable = self.unavailable_reason()
        if unavailable:
            raise SandboxUnavailableError(unavailable)
        resolved = policy.filesystem.resolve(context)
        unenforceable = self._unenforceable(resolved, policy)
        if unenforceable:
            raise SandboxUnenforceableError(unenforceable)
        return self._build_launch(resolved, policy, context)

    def _unenforceable(self, resolved: ResolvedFileSystem, policy: SandboxPolicy) -> str:
        return ""

    def _build_launch(
        self,
        resolved: ResolvedFileSystem,
        policy: SandboxPolicy,
        context: PolicyContext,
    ) -> SandboxLaunch:  # pragma: no cover - overridden
        raise NotImplementedError


def current_platform() -> str:
    """``macos`` / ``linux`` / ``windows`` / the raw ``sys.platform`` otherwise."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def get_platform_backend(platform: Optional[str] = None) -> Optional[SandboxBackend]:
    """The backend for ``platform``, or ``None`` when the platform has none.

    Imports are deferred so a backend's platform-specific dependencies (the
    Windows ``ctypes`` bindings above all) are only loaded where they exist.
    """
    target = platform or current_platform()
    if target == "macos":
        from .seatbelt import SeatbeltBackend

        return SeatbeltBackend()
    if target == "linux":
        from .bwrap import BubblewrapBackend

        return BubblewrapBackend()
    if target == "windows":
        from .windows_token import WindowsRestrictedTokenBackend

        return WindowsRestrictedTokenBackend()
    return None


def unavailable_reason(platform: Optional[str] = None) -> str:
    """Why no confinement is possible here, or ``""`` when a backend is ready."""
    target = platform or current_platform()
    backend = get_platform_backend(target)
    if backend is None:
        return f"当前平台 {target} 没有可用的沙箱后端"
    return backend.unavailable_reason()


def build_launch(
    policy: SandboxPolicy,
    context: PolicyContext,
    *,
    platform: Optional[str] = None,
) -> SandboxLaunch:
    """Build the confined launch for ``policy`` on this host.

    Raises :class:`SandboxUnavailableError` when the platform has no usable
    backend and :class:`SandboxUnenforceableError` when the backend cannot
    enforce every dimension the policy asks for. It never returns an
    unconfined launch.
    """
    target = platform or current_platform()
    backend = get_platform_backend(target)
    if backend is None:
        raise SandboxUnavailableError(f"当前平台 {target} 没有可用的沙箱后端")
    return backend.build(policy, context)


__all__ = [
    "BasePlatformBackend",
    "SandboxBackend",
    "SandboxLaunch",
    "build_launch",
    "current_platform",
    "get_platform_backend",
    "unavailable_reason",
]
