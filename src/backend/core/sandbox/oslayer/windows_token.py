"""Windows backend: the low-integrity sandbox.

Windows has no way to express this confinement as a command-line wrapper the way
``sandbox-exec`` and ``bwrap`` do — the token has to be built and handed to
``CreateProcessAsUser`` by whoever creates the process. So this backend produces
a *plan* instead of an argv prefix, and the spawning boundary applies it.

This module owns the *deciding* half: it resolves the policy once and produces a
flat plan. The applying half lives in
:mod:`core.sandbox.oslayer.windows_runtime` and runs inside whatever process
creates the child — for the desktop, the long-lived sidecar, so the token is
built without an extra process per command.

What the backend can and cannot enforce is declared, not discovered at runtime:

* **Writes** — enforced. The command runs at Low integrity, and only the roots
  of the current plan carry a Low label (labels left by earlier plans are taken
  back at spawn, see :mod:`core.sandbox.oslayer.windows_runtime`); everything
  else is Medium or higher and not writable. The honest limit is the few Low
  areas other low-integrity software keeps for itself, such as
  ``%USERPROFILE%/AppData/LocalLow``.
* **Reads** — *not* enforceable here. Integrity levels block writes upward, not
  reads, so a policy that narrows reads or denies any path is refused rather
  than half-applied. Codex refuses in the same situation and for the same
  reason.
* **Network** — no mechanism without administrator rights (the filtering
  platform needs them). A policy that restricts the network is refused.

Every one of those refusals is surfaced to the permission layer, which turns it
into a message telling the user which permission scope would let the command run
— never into a silently unconfined execution.
"""

from __future__ import annotations

import os

from core.sandbox.oslayer.models import AccessMode, PolicyContext, ResolvedFileSystem, SandboxPolicy

from .backend import BasePlatformBackend, SandboxLaunch
from .errors import SandboxUnavailableError

# Name of the private scratch directory the sandbox uses instead of the user's
# shared temp folder. See :func:`scratch_directory` for why it exists.
SCRATCH_DIRNAME = "sandbox_scratch"


def scratch_directory(state_dir: str) -> str:
    """The private directory that satisfies a policy's scratch-space grant.

    Windows expresses "writable" as an inheritable label, and applying one to
    the user's shared temp folder means walking and rewriting the security of
    every file already in it — tens of thousands of them on a real machine,
    which turns a one-second command into a minute-long one. It is also far more
    access than scratch space needs: the point of the ephemeral grant is a place
    to write that is *not* shared with everything else the user is running.

    So the sandbox gets its own directory and is pointed at it. Linux reaches
    the same outcome with a private tmpfs; this is the Windows spelling of it.
    """
    return os.path.join(state_dir, SCRATCH_DIRNAME)


def build_plan(resolved: ResolvedFileSystem, context: PolicyContext) -> dict:
    """Turn a resolved policy into the flat plan the launcher applies.

    Ephemeral roots collapse onto the single private scratch directory, and each
    root's existing read-only entries and protected metadata names are joined
    into one list — the launcher does not need to know which was which, only
    what must stay read-only.
    """
    if not context.state_dir:
        raise SandboxUnavailableError("Windows 沙箱需要 state_dir 来记录已打标签的目录")

    writable: list[dict] = []
    wants_scratch = False
    for access_root in resolved.roots_for(AccessMode.WRITE):
        if access_root.ephemeral:
            wants_scratch = True
            continue
        read_only = list(access_root.read_only_subpaths)
        read_only += [
            os.path.join(access_root.root, name) for name in access_root.protected_metadata_names
        ]
        # A label needs an object to sit on; a carve-out nothing has created yet
        # protects nothing and is left out so the launcher applies the plan verbatim.
        writable.append(
            {"path": access_root.root, "read_only": [p for p in read_only if os.path.exists(p)]}
        )

    plan: dict = {"state_dir": context.state_dir, "writable_roots": writable}
    if wants_scratch:
        scratch = scratch_directory(context.state_dir)
        writable.append({"path": scratch, "read_only": []})
        plan["scratch_dir"] = scratch
    return plan


class WindowsLowIntegrityBackend(BasePlatformBackend):
    """Confines a command with a Low-integrity token and per-root labels."""

    name = "windows_low_integrity"

    def unavailable_reason(self) -> str:
        # Nothing to probe: the mechanism is the operating system's own token
        # API, present on every Windows this product runs on. Whether the
        # process that spawns the command can *apply* the plan is that
        # boundary's own check, and it refuses rather than running unconfined.
        return ""

    def _unenforceable(self, resolved: ResolvedFileSystem, policy: SandboxPolicy) -> str:
        if not policy.network.is_enabled:
            return (
                "Windows 低完整性沙箱无法在不提权的情况下阻断网络"
                "（需要管理员权限的网络过滤平台），已拒绝执行"
            )
        if not resolved.has_full_disk_read_access:
            return "Windows 低完整性沙箱无法限制读取范围，已拒绝执行"
        if resolved.roots_for(AccessMode.DENY):
            return "Windows 低完整性沙箱无法执行「禁止访问」条目，已拒绝执行"
        return ""

    def _build_launch(
        self,
        resolved: ResolvedFileSystem,
        policy: SandboxPolicy,
        context: PolicyContext,
    ) -> SandboxLaunch:
        return SandboxLaunch(backend=self.name, spawn_plan=build_plan(resolved, context))


__all__ = [
    "SCRATCH_DIRNAME",
    "WindowsLowIntegrityBackend",
    "build_plan",
    "scratch_directory",
]
