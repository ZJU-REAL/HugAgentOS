"""OS-level sandbox wrapping for desktop local execution (tickets #09 / #12).

The local host-subprocess sandbox runs bash as the user with no filesystem jail —
the string-level policy gate (``local_policy``) is defense-in-depth, not
isolation. This module is the real isolation layer: it wraps a shell command so
the OS confines *writes* to an allow-list (the workspace root + user-authorized
folders). Standard mode also gets a private/approved temp area; strict mode has
no writable host path. Reads stay open, matching the reference sandbox modes.

- **macOS**: Apple Seatbelt via ``sandbox-exec -p <profile>``.
- **Linux**: ``bwrap`` (bubblewrap) with a read-only root + writable binds.
- **Windows**: no filesystem-confining runner is currently bundled.

The sandbox is enabled by default; ``HUGAGENT_LOCAL_OS_SANDBOX=0`` is an
explicit administrative disable. Whether an unavailable backend is fatal is
**not decided here** — :func:`confinement_unavailable_reason` reports the fact
and the caller's permission preset decides (see
``core.llm.tool_permissions.LOCAL_CONFINEMENT_BY_MODE``): ``strict`` requires
confinement and refuses without it, ``standard`` degrades to the command-policy
gate with a warning, ``full`` waives it. Keeping the decision in the policy
layer is what stops "no backend on this platform" from silently meaning "no
shell at all" on Windows.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from typing import List, Optional

_HEREDOC = "HG_SBX_EOF_9Z"

# Standard mode gets the platform temp area. Linux uses a private tmpfs in the
# mount namespace; macOS Seatbelt needs the canonical host temp paths listed.
_MAC_EXTRA_WRITE = ["/tmp", tempfile.gettempdir()]


class OsSandboxUnavailableError(RuntimeError):
    """Raised when a restricted local command cannot be strongly confined."""


def os_sandbox_enabled() -> bool:
    raw = os.getenv("HUGAGENT_LOCAL_OS_SANDBOX", "").strip().casefold()
    return raw not in ("0", "false", "no", "off")


def confinement_runner(platform: Optional[str] = None) -> str:
    """Name of the filesystem-confinement runner for ``platform`` ("" if none)."""
    plat = platform or _current_platform()
    return "sandbox-exec" if plat == "macos" else "bwrap" if plat == "linux" else ""


def confinement_unavailable_reason(platform: Optional[str] = None) -> str:
    """Why strong write confinement cannot be applied here ("" when it can).

    Callers use this to *decide* — a preset that merely prefers confinement can
    degrade with a warning, while one that requires it must refuse. Keeping the
    probe separate from :func:`wrap_command` is what lets that choice live in
    the permission policy instead of at each execution site.
    """
    if not os_sandbox_enabled():
        return "本机 OS 沙箱已被 HUGAGENT_LOCAL_OS_SANDBOX 显式关闭"
    plat = platform or _current_platform()
    runner = confinement_runner(plat)
    if not runner:
        return f"当前平台 {plat} 尚无可用的文件系统隔离后端"
    if shutil.which(runner) is None:
        return f"本机缺少 OS 沙箱运行器 {runner}"
    return ""


def confinement_available(platform: Optional[str] = None) -> bool:
    return not confinement_unavailable_reason(platform)


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
        escaped = p.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    (subpath "{escaped}")')
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


def _wrap_linux(cmd: str, write_paths: List[str], *, allow_temp: bool) -> str:
    args = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--die-with-parent",
    ]
    if allow_temp:
        # Private and ephemeral: never expose the host's shared /tmp writable.
        args += ["--tmpfs", "/tmp"]
    for p in write_paths:
        if p == "/tmp":
            continue
        args += ["--bind", p, p]
    args += ["/bin/bash"]
    prefix = " ".join(shlex.quote(a) for a in args)
    return f"{prefix} <<'{_HEREDOC}'\n{cmd}\n{_HEREDOC}"


def wrap_command(
    cmd: str,
    write_paths: List[str],
    *,
    platform: Optional[str] = None,
    read_only: bool = False,
) -> str:
    """Wrap ``cmd`` so the OS confines persistent writes.

    ``read_only`` removes every caller-provided writable root and does not add a
    writable temp area. Disabled, unsupported, or missing runners raise instead
    of returning the raw command. ``platform`` overrides autodetection for tests.
    """
    unavailable = confinement_unavailable_reason(platform)
    if unavailable:
        raise OsSandboxUnavailableError(unavailable)
    plat = platform or _current_platform()
    # de-dup + keep only absolute paths
    seen: List[str] = []
    extras = _MAC_EXTRA_WRITE if plat == "macos" else []
    requested = [] if read_only else list(write_paths) + extras
    for p in requested:
        ap = os.path.realpath(os.path.abspath(os.path.expanduser(p))) if p else ""
        if ap and ap not in seen:
            seen.append(ap)
    if plat == "macos":
        return _wrap_macos(cmd, seen)
    if plat == "linux":
        return _wrap_linux(cmd, seen, allow_temp=not read_only)
    raise OsSandboxUnavailableError(f"当前平台 {plat} 无法执行受限命令")


__all__ = [
    "OsSandboxUnavailableError",
    "confinement_available",
    "confinement_runner",
    "confinement_unavailable_reason",
    "os_sandbox_enabled",
    "wrap_command",
]
