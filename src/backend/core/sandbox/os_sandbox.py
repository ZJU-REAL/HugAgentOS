"""OS-level sandbox wrapping for desktop local execution (tickets #09 / #12).

The local host-subprocess sandbox runs bash as the user with no filesystem jail —
the string-level policy gate (``local_policy``) is defense-in-depth, not
isolation. This module is the real isolation layer: it wraps a shell command so
the OS confines *writes* to an allow-list (the workspace root + user-authorized
folders + the usual temp/dev paths), while reads stay open.

- **macOS**: Apple Seatbelt via ``sandbox-exec -p <profile>``.
- **Linux**: ``bwrap`` (bubblewrap) with a read-only root + writable binds.
- **Windows**: not handled here (Job Object / AppContainer, ticket #11).

SAFETY / ROLLOUT: a wrong profile can break every command, and this can't be
end-to-end validated without the running local server, so it is **opt-in**:
enabled only when ``HUGAGENT_LOCAL_OS_SANDBOX=1``. When off (default),
``wrap_command`` returns the command unchanged. The command-string policy gate
(always on in local mode) still applies either way.
"""

from __future__ import annotations

import os
import shlex
from typing import List, Optional

_HEREDOC = "HG_SBX_EOF_9Z"

# Baseline writable locations any normal toolchain needs.
_MAC_EXTRA_WRITE = ["/tmp", "/private/tmp", "/private/var/folders", "/dev"]
_LINUX_EXTRA_WRITE = ["/tmp", "/dev", "/run"]


def os_sandbox_enabled() -> bool:
    return os.getenv("HUGAGENT_LOCAL_OS_SANDBOX", "").strip() in ("1", "true", "yes")


def _current_platform() -> str:
    if os.name == "nt":
        return "windows"
    import sys

    return "macos" if sys.platform == "darwin" else "linux"


def _macos_profile(write_paths: List[str]) -> str:
    lines = [
        "(version 1)",
        "(allow default)",  # allow-by-default, then subtract writes outside the allow-list
        "(deny file-write*)",
        "(allow file-write*",
    ]
    for p in write_paths:
        lines.append(f'    (subpath "{p}")')
    lines.append('    (literal "/dev/null")')
    lines.append('    (literal "/dev/stdout")')
    lines.append('    (literal "/dev/stderr")')
    lines.append(")")
    return "\n".join(lines)


def _wrap_macos(cmd: str, write_paths: List[str]) -> str:
    profile = _macos_profile(write_paths)
    # profile uses only double quotes → safe inside a single-quoted -p arg.
    return (
        f"sandbox-exec -p {shlex.quote(profile)} /bin/bash <<'{_HEREDOC}'\n"
        f"{cmd}\n"
        f"{_HEREDOC}"
    )


def _wrap_linux(cmd: str, write_paths: List[str]) -> str:
    args = ["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in write_paths:
        args += ["--bind", p, p]
    args += ["/bin/bash"]
    prefix = " ".join(shlex.quote(a) for a in args)
    return f"{prefix} <<'{_HEREDOC}'\n{cmd}\n{_HEREDOC}"


def wrap_command(
    cmd: str,
    write_paths: List[str],
    *,
    platform: Optional[str] = None,
) -> str:
    """Wrap ``cmd`` so the OS confines writes to ``write_paths`` (+ temp/dev).

    Returns the command unchanged when the OS sandbox is disabled or the platform
    is unsupported. ``platform`` overrides autodetection (for tests).
    """
    if not os_sandbox_enabled():
        return cmd
    plat = platform or _current_platform()
    # de-dup + keep only absolute paths
    seen: List[str] = []
    extras = _MAC_EXTRA_WRITE if plat == "macos" else _LINUX_EXTRA_WRITE
    for p in list(write_paths) + extras:
        ap = os.path.abspath(os.path.expanduser(p)) if p else ""
        if ap and ap not in seen:
            seen.append(ap)
    if plat == "macos":
        return _wrap_macos(cmd, seen)
    if plat == "linux":
        return _wrap_linux(cmd, seen)
    return cmd  # windows / unknown → unchanged (handled elsewhere)


__all__ = ["os_sandbox_enabled", "wrap_command"]
