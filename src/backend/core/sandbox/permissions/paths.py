"""Local-mode execution policy gate (desktop only, host-subprocess sandbox).

Ticket #07 decision core. A **pure function** that classifies a shell command
about to run in the no-Docker host-subprocess sandbox as ``allow`` / ``deny`` /
``confirm``, given the user's authorized folders (grants) and their
danger-command policy.

SECURITY NOTE
-------------
This is a *string/argument-level heuristic*. It prevents fat-finger mistakes and
produces an audit trail, but it is **not** a strong isolation boundary — a
crafted shell command (variable indirection, base64, sub-shells) can evade it.
The real boundary is the OS sandbox in :mod:`core.sandbox.oslayer` (macOS
Seatbelt, Linux bubblewrap, Windows low-integrity token), which every preset except
the unrestricted one runs under and which refuses the command outright when it
cannot be applied. Keep this command classifier as defense-in-depth, never as
the only line.

EDITION / WEB-SAFETY
--------------------
Edition-agnostic: lives in shared ``core`` so it is present in both the cloud
(EE) tree and the desktop CE derivation. The module is **inert** until wired
into the bash tool upstream, and that wiring is gated on local mode — so the
cloud / web deployment is unaffected.

The decision vocabulary intentionally matches the existing human-in-the-loop
(HITL) semantics: ``confirm`` maps onto the ``tool_pending`` / ``file_confirm``
approval flow, ``deny`` blocks outright, ``allow`` runs unattended."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re


def _pathmod(platform: str):
    return ntpath if platform == "windows" else posixpath


def _norm(path: str, platform: str) -> str:
    pm = _pathmod(platform)
    norm = pm.normpath(path)
    if platform == "windows":
        norm = norm.replace("/", "\\").casefold()
    return norm.rstrip(pm.sep) or pm.sep


def _canonical(path: str, platform: str) -> str:
    """Resolve path identity before doing containment checks.

    On the current host this resolves symlinks (including an existing symlink
    parent of a not-yet-created file). Cross-platform unit-test inputs retain
    their native lexical semantics instead of being interpreted by the host OS.
    """
    expanded = os.path.expanduser(path)
    actual_windows = os.name == "nt"
    if (platform == "windows") == actual_windows:
        expanded = os.path.realpath(os.path.abspath(expanded))
    return _norm(expanded, platform)


def _is_under(path: str, base: str, platform: str) -> bool:
    """True when ``path`` equals ``base`` or is nested inside it."""
    pm = _pathmod(platform)
    p = _norm(path, platform)
    b = _norm(base, platform)
    if platform != "windows" and b == pm.sep:
        return p.startswith(pm.sep)
    return p == b or p.startswith(b + pm.sep)


def _looks_like_path(token: str, platform: str) -> bool:
    if not token or token.startswith("-"):
        return False
    # URLs are command arguments, not host paths.  Treating them as relative
    # paths produces misleading scope decisions for curl/wget.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", token):
        return False
    if token.startswith("~") or "/" in token:
        return True
    if platform == "windows":
        # backslash path or drive-letter absolute
        return "\\" in token or bool(re.match(r"^[A-Za-z]:", token))
    return False


def _resolve(token: str, cwd: str, platform: str) -> str:
    pm = _pathmod(platform)
    t = token
    if t.startswith("~"):
        return _canonical(t, platform)
    if pm.isabs(t):
        return _canonical(t, platform)
    return _canonical(pm.join(cwd, t), platform)


def _system_write_roots(platform: str) -> tuple[str, ...]:
    return (
        (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)")
        if platform == "windows"
        else (
            "/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/boot",
            "/sys",
            "/lib",
            "/lib64",
            "/System",
            "/Library",
        )
    )


def _is_system_write_path(path: str, platform: str) -> bool:
    roots = _system_write_roots(platform)
    return any(_is_under(path, root, platform) for root in roots)


def intersects_system_write_area(path: str, platform: str = "posix") -> bool:
    """Whether a writable root overlaps a protected system directory."""
    resolved = _canonical(path, platform)
    return any(
        _is_under(resolved, root, platform) or _is_under(root, resolved, platform)
        for root in _system_write_roots(platform)
    )
