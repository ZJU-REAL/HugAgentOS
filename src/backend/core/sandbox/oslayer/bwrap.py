"""Linux backend: bubblewrap mount namespaces.

Ported from Codex's ``codex-rs/linux-sandbox/src/bwrap.rs`` (Apache-2.0), which
is the backend Codex defaults to on Linux. The shape of the sandbox:

* the filesystem starts read-only (``--ro-bind / /``) or empty (``--tmpfs /``)
  depending on whether the policy grants full read access, and a minimal
  ``/dev`` is mounted before anything else so explicit ``/dev/*`` grants stay
  visible;
* writable roots are layered on with ``--bind`` from broadest to narrowest, so a
  narrower rule always lands on top of the broader one it overrides;
* read-only carve-outs inside a writable root are re-applied afterwards, and
  denied paths are replaced by an empty, unreadable mount — a locked-down
  ``tmpfs`` for directories, ``/dev/null`` for anything else, including paths
  that do not exist yet and symlinks that would otherwise let the command walk
  out of its root;
* user, PID and IPC namespaces are always unshared, and the network namespace is
  unshared whenever the policy restricts the network. Bubblewrap sets
  ``PR_SET_NO_NEW_PRIVS`` itself for unprivileged sandboxes, so there is nothing
  to add there.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

from .backend import BasePlatformBackend, SandboxLaunch
from .policy import (
    AccessMode,
    NetworkPolicy,
    PolicyContext,
    ResolvedFileSystem,
    SandboxPolicy,
    filesystem_root,
    is_within,
    normalize_logical,
    path_depth,
)

BWRAP_EXECUTABLE = "bwrap"

# Where a build may place a bubblewrap it ships itself, relative to the backend
# package root. Codex does the same thing with ``codex-resources/bwrap``: a
# machine whose distribution has no bubblewrap package, or whose user cannot
# install one, otherwise has no way to run a shell command at all.
BUNDLED_BWRAP_RELATIVE_PATH = os.path.join("resources", "sandbox", BWRAP_EXECUTABLE)


def bundled_bwrap_path() -> str:
    """Absolute path of the bubblewrap this build ships, shipped or not."""
    package_root = Path(__file__).resolve().parents[3]
    return str(package_root / BUNDLED_BWRAP_RELATIVE_PATH)


def resolve_bwrap() -> str:
    """The bubblewrap this host will use, or ``""`` when there is none.

    The system's own copy wins. It is the one the distribution keeps patched,
    and preferring it means a security update reaches the sandbox without us
    shipping a new build. The bundled copy is the answer for hosts that have
    none — not a preference.
    """
    system = shutil.which(BWRAP_EXECUTABLE)
    if system:
        return system
    bundled = bundled_bwrap_path()
    return bundled if os.access(bundled, os.X_OK) else ""


# The system paths a process needs before it can run anything at all. Only used
# when the policy narrows reads and asks for the platform baseline; entries that
# do not exist on this distribution are skipped rather than mounted blindly.
PLATFORM_DEFAULT_READ_ROOTS = (
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/lib",
    "/lib64",
    "/nix/store",
    "/run/current-system/sw",
)

# Bubblewrap masks a denied directory by mounting an empty tmpfs over it. `000`
# hides it completely; `111` leaves it traversable, which is required when a
# writable descendant has been explicitly re-opened underneath.
_DENIED_DIR_PERMS_OPAQUE = "000"
_DENIED_DIR_PERMS_TRAVERSABLE = "111"
_NULL_DEVICE = "/dev/null"


def _mount_path(path: str) -> str:
    """Path bubblewrap should mount on.

    A symlinked root is mounted at its target: mounting *over* the link would
    either fail or leave the command reaching the real directory unconfined.
    """
    resolved = os.path.realpath(path)
    return resolved if resolved != path and os.path.exists(resolved) else path


def _first_missing_component(path: str) -> str:
    """Shallowest ancestor of ``path`` (or ``path`` itself) that does not exist.

    Freezing that component is what stops a command from creating the whole
    missing chain and then writing inside it.
    """
    current = normalize_logical(path)
    if os.path.exists(current):
        return current
    parent = os.path.dirname(current)
    while parent and parent != current and not os.path.exists(parent):
        current, parent = parent, os.path.dirname(parent)
    return current


class BubblewrapBackend(BasePlatformBackend):
    """Confines a command in a bubblewrap mount + namespace sandbox."""

    name = "bwrap"

    def unavailable_reason(self) -> str:
        if not resolve_bwrap():
            return (
                f"本机缺少 Linux 沙箱运行器 {BWRAP_EXECUTABLE}（bubblewrap），"
                "本发行版也没有自带一份；请用系统包管理器安装 bubblewrap 后重试"
            )
        return ""

    def _build_launch(
        self,
        resolved: ResolvedFileSystem,
        policy: SandboxPolicy,
        context: PolicyContext,
    ) -> SandboxLaunch:
        args: list[str] = [resolve_bwrap()]
        args += self._base_filesystem_args(resolved)

        writable = sorted(
            resolved.roots_for(AccessMode.WRITE),
            key=lambda item: (path_depth(item.root), item.root),
        )
        mounted_writable: list[str] = []
        for access_root in writable:
            if access_root.ephemeral:
                # Private scratch: a fresh tmpfs, not the host's shared temp
                # directory. It needs no mount source, so it is also the one
                # writable root that does not have to already exist.
                args += ["--tmpfs", access_root.root]
                continue
            target = _mount_path(access_root.root)
            if not os.path.exists(target):
                continue
            args += ["--bind", target, target]
            mounted_writable.append(target)

        args += self._carve_out_args(resolved, mounted_writable)
        args += self._denied_args(resolved, mounted_writable)
        args += self._namespace_args(policy.network)
        args.append("--")
        return SandboxLaunch(backend=self.name, argv_prefix=tuple(args))

    def _base_filesystem_args(self, resolved: ResolvedFileSystem) -> list[str]:
        if resolved.has_full_disk_read_access:
            return ["--ro-bind", "/", "/", "--dev", "/dev"]

        readable = {entry.root for entry in resolved.roots_for(AccessMode.READ)}
        if resolved.include_platform_defaults:
            readable.update(path for path in PLATFORM_DEFAULT_READ_ROOTS if os.path.exists(path))
        if filesystem_root() in readable:
            return ["--ro-bind", "/", "/", "--dev", "/dev"]

        args = ["--tmpfs", "/", "--dev", "/dev"]
        for root in sorted(readable, key=lambda item: (path_depth(item), item)):
            if not os.path.exists(root):
                continue
            args += ["--ro-bind", root, root]
        return args

    def _carve_out_args(
        self, resolved: ResolvedFileSystem, mounted_writable: Iterable[str]
    ) -> list[str]:
        """Re-apply read-only paths that sit inside a writable root.

        Covers both the policy's own narrower read entries and the protected
        metadata names, and it has to run after the writable binds — otherwise
        the writable mount would simply cover it again.
        """
        writable = list(mounted_writable)
        args: list[str] = []
        for access_root in resolved.roots_for(AccessMode.WRITE):
            if access_root.ephemeral:
                continue
            targets = list(access_root.read_only_subpaths)
            targets += [
                os.path.join(access_root.root, name)
                for name in access_root.protected_metadata_names
            ]
            for target in targets:
                if not any(is_within(target, root) for root in writable):
                    continue
                args += self._read_only_mount(target)
        return args

    def _read_only_mount(self, path: str) -> list[str]:
        """Make one path read-only inside an already-writable root."""
        if os.path.islink(path):
            # The link itself must not be replaceable, and its target is not
            # what the policy named. Mask it outright.
            return ["--ro-bind", _NULL_DEVICE, path]
        if os.path.exists(path):
            return ["--ro-bind", path, path]
        missing = _first_missing_component(path)
        # Nothing is there yet; freeze the shallowest missing component so the
        # command cannot create it and then write inside it.
        return ["--perms", "555", "--tmpfs", missing, "--remount-ro", missing]

    def _denied_args(
        self, resolved: ResolvedFileSystem, mounted_writable: Iterable[str]
    ) -> list[str]:
        writable = list(mounted_writable)
        args: list[str] = []
        denied = sorted(
            (entry.root for entry in resolved.roots_for(AccessMode.DENY)),
            key=lambda item: (path_depth(item), item),
        )
        for path in denied:
            if os.path.isdir(path) and not os.path.islink(path):
                descendants = [root for root in writable if root != path and is_within(root, path)]
                perms = _DENIED_DIR_PERMS_TRAVERSABLE if descendants else _DENIED_DIR_PERMS_OPAQUE
                args += ["--perms", perms, "--tmpfs", path]
                # Re-create the re-opened writable descendants inside the fresh
                # tmpfs before freezing it: bubblewrap cannot create a mount
                # point under a directory it has already remounted read-only.
                for descendant in sorted(descendants, key=path_depth):
                    args += ["--bind", descendant, descendant]
                args += ["--remount-ro", path]
                continue
            args += ["--ro-bind", _NULL_DEVICE, path]
        return args

    def _namespace_args(self, network: NetworkPolicy) -> list[str]:
        args = [
            "--new-session",
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--proc",
            "/proc",
        ]
        if not network.is_enabled:
            args.append("--unshare-net")
        return args


__all__ = [
    "BUNDLED_BWRAP_RELATIVE_PATH",
    "BWRAP_EXECUTABLE",
    "PLATFORM_DEFAULT_READ_ROOTS",
    "BubblewrapBackend",
    "bundled_bwrap_path",
    "resolve_bwrap",
]
