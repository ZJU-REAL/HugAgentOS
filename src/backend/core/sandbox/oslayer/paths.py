"""Platform-neutral sandbox policy model.

This is the single description of "what may this command touch", shared by every
platform backend. It is a port of the Codex sandbox permission model
(``codex-rs/protocol/src/permissions.rs``, Apache-2.0) so the three backends can
be written the way Codex writes them: a *closed* policy that lists what is
allowed, rather than an open one that subtracts a few denials.

Two dimensions, both first-class:

* **Filesystem** — an ordered set of entries, each mapping a path to
  :class:`AccessMode` (``read`` / ``write`` / ``deny``). Entries are resolved by
  *path specificity*: a broader entry is applied first and a narrower one
  overrides it, so ``/repo = write``, ``/repo/a = deny``, ``/repo/a/b = write``
  means exactly what it reads like. At equal specificity the more restrictive
  mode wins (``deny`` > ``write`` > ``read``).
* **Network** — ``restricted`` or ``enabled``. Backends that cannot enforce a
  restriction raise instead of ignoring it.

Paths may be literal absolute paths or one of the :class:`SpecialPath` tokens,
which are resolved against a :class:`PolicyContext` at build time. Keeping them
symbolic is what lets one policy travel from the permission layer to a launcher
process on the other side of a process boundary without either side hardcoding
the caller's directory layout."""

from __future__ import annotations

import os

from core.sandbox.oslayer.errors import SandboxPolicyError


def normalize_logical(path: str) -> str:
    """Absolute, ``..``-free form of ``path`` with symlinks left intact.

    Writable roots are normalized this way on purpose: resolving a symlink would
    grant its *target*, so a link the agent itself can rewrite would silently
    widen the sandbox on the next command.
    """
    expanded = os.path.expanduser((path or "").strip())
    if not expanded:
        raise SandboxPolicyError("沙箱策略中的路径不能为空")
    return os.path.normpath(os.path.abspath(expanded))


def resolve_symlinks(path: str) -> str:
    """Fully resolved form of ``path``, used where granting the target is intended."""
    return os.path.realpath(normalize_logical(path))


def normalize_trusted_top_level_alias(path: str) -> str:
    """Resolve only a symlinked *top-level* component of ``path``.

    Some systems expose their real directories through top-level links the OS
    itself owns and the sandboxed process cannot change — on macOS ``/tmp`` is a
    link to ``/private/tmp``, and a policy naming ``/tmp`` has to reach the
    kernel as the resolved path or it matches nothing. Resolving the *whole*
    path would be unsafe for a writable root, so only that first component is
    followed, and only when it really is a link. No per-OS table: the filesystem
    is asked.
    """
    normalized = normalize_logical(path)
    parts = [part for part in normalized.split(os.sep) if part]
    if not parts:
        return normalized
    top_level = os.sep + parts[0]
    if not os.path.islink(top_level):
        return normalized
    resolved_top = os.path.realpath(top_level)
    return os.path.normpath(os.path.join(resolved_top, *parts[1:]))


def path_depth(path: str) -> int:
    """Specificity of an absolute path: how many components below the root it is."""
    return len([part for part in path.replace(os.sep, "/").split("/") if part])


def is_within(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or lives beneath it, component-wise."""
    if path == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path.startswith(prefix)


def filesystem_root() -> str:
    """The single root every backend anchors ``:root`` on."""
    return os.path.abspath(os.sep)
