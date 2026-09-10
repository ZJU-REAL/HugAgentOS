"""Desktop local mode → OS sandbox policy.

This is the one place that translates what the user chose into what the sandbox
enforces. Everything below it (:mod:`core.sandbox.oslayer`) is platform
mechanics; everything above it is product vocabulary — the permission preset in
the composer, the folders authorized in 设置 → 本地权限, and the per-category
danger dispositions.

The mapping, in full:

===========================  ==============================================
User's choice                Resulting policy
===========================  ==============================================
Preset ``full``              No confinement at all. This is the preset whose
                             entire meaning is "run it as me"; honouring it is
                             not a fallback, it is the setting.
Preset ``ask`` / ``auto``    Filesystem restricted: reads open, writes limited
                             to the workspace, the folders granted read-write,
                             and the paths this one command was approved to
                             write. Scratch space is private.
Grant mode ``read``          Contributes a readable root and no write.
``workspace_write = block``  No writable roots at all — a read-only sandbox.
``danger.network = block``   Network restricted; anything else leaves it open,
                             because the command-level gate has already made
                             the call about this specific command.
``danger.system_write``      Unless set to ``allow``, protected system areas
                             never become writable roots, no matter which grant
                             or one-shot target asked for them.
===========================  ==============================================

There is no path through this module that produces "unconfined" without the user
having chosen it: an unavailable backend or an unenforceable policy raises out of
:func:`confine`, and the caller turns that into a refusal.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.sandbox.oslayer import (
    NetworkPolicy,
    PolicyContext,
    SandboxLaunch,
    SandboxPolicy,
    SandboxUnavailableError,
    SandboxUnenforceableError,
    build_filesystem_policy,
    build_launch,
    current_platform,
    get_platform_backend,
    unrestricted_policy,
)
from core.sandbox.oslayer.policy import normalize_logical

# Basenames that stay read-only inside every writable root. These are the files
# that describe a project's own authority rather than its content: rewriting the
# version-control metadata would let a command rewrite the record of what it
# did, and the agent's own state directory decides what the next run is allowed
# to do. Codex protects the same class of paths for the same reason.
VERSION_CONTROL_METADATA_NAME = ".git"


def local_state_dir() -> str:
    """Where the desktop install keeps its local state.

    Reuses the data directory the rest of local mode already agrees on rather
    than introducing a sandbox-specific location.
    """
    return str(Path(os.getenv("HUGAGENT_HOME", str(Path.home() / ".hugagent"))).expanduser())


def protected_metadata_names() -> tuple[str, ...]:
    """Protected basenames for this install.

    The agent's own state directory is derived from where local mode actually
    put it, so a rebranded build protects its own directory name without anyone
    editing a list.
    """
    return (VERSION_CONTROL_METADATA_NAME, os.path.basename(local_state_dir()))


def build_context(*, workspace_root: str, cwd: Optional[str] = None) -> PolicyContext:
    """Executor facts the policy is resolved against."""
    root = normalize_logical(workspace_root)
    return PolicyContext(
        cwd=normalize_logical(cwd) if cwd else root,
        workspace_roots=(root,),
        home_dir=str(Path.home()),
        temp_dirs=(tempfile.gettempdir(),),
        protected_metadata_names=protected_metadata_names(),
        state_dir=local_state_dir(),
    )


@dataclass(frozen=True)
class LocalAccessDecision:
    """What the permission layer decided this command may touch.

    ``writable_roots`` are already filtered by the permission layer's own rules
    (system areas, out-of-scope targets); this module does not second-guess
    them, it only expresses them.
    """

    approval_mode: str
    unconfined: bool = False
    writable_roots: tuple[str, ...] = ()
    readable_roots: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    network_allowed: bool = True
    writable_scratch: bool = True


def build_policy(decision: LocalAccessDecision) -> SandboxPolicy:
    """Turn a permission decision into a sandbox policy."""
    if decision.unconfined:
        return unrestricted_policy()
    return SandboxPolicy(
        filesystem=build_filesystem_policy(
            writable_roots=decision.writable_roots,
            readable_roots=decision.readable_roots,
            denied_paths=decision.denied_paths,
            writable_temp=decision.writable_scratch,
        ),
        network=NetworkPolicy.ENABLED if decision.network_allowed else NetworkPolicy.RESTRICTED,
    )


def confine(policy: SandboxPolicy, context: PolicyContext) -> SandboxLaunch:
    """Build the confined launch, or raise saying exactly why it cannot."""
    return build_launch(policy, context)


def backend_name(platform: Optional[str] = None) -> str:
    """Human-facing name of the platform's confinement mechanism."""
    backend = get_platform_backend(platform)
    return backend.name if backend is not None else ""


def confinement_unavailable_reason(platform: Optional[str] = None) -> str:
    """Why no confinement is possible on this host, or ``""`` when it is."""
    target = platform or current_platform()
    backend = get_platform_backend(target)
    if backend is None:
        return f"当前平台 {target} 没有可用的沙箱后端"
    return backend.unavailable_reason()


def confinement_available(platform: Optional[str] = None) -> bool:
    return not confinement_unavailable_reason(platform)


def policy_unenforceable_reason(
    policy: SandboxPolicy, context: PolicyContext, platform: Optional[str] = None
) -> str:
    """Why this policy cannot be fully enforced here, or ``""`` when it can."""
    backend = get_platform_backend(platform)
    if backend is None:
        return confinement_unavailable_reason(platform)
    return backend.unenforceable_reason(policy, context)


__all__ = [
    "LocalAccessDecision",
    "SandboxUnavailableError",
    "SandboxUnenforceableError",
    "VERSION_CONTROL_METADATA_NAME",
    "backend_name",
    "build_context",
    "build_policy",
    "confine",
    "confinement_available",
    "confinement_unavailable_reason",
    "local_state_dir",
    "policy_unenforceable_reason",
    "protected_metadata_names",
]
