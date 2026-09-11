"""macOS backend: Apple Seatbelt, closed by default.

The profile starts at ``(deny default)`` and every capability the command gets
is written out explicitly — reads, writes, network, Mach services, sysctls. The
baseline lives in :mod:`profiles` as verbatim Seatbelt policy files; this module
only generates the *dynamic* part: the roots the policy grants, the carve-outs
inside them, and the network section.

Ported from Codex's ``codex-rs/sandboxing/src/seatbelt.rs`` (Apache-2.0). Two
details from that implementation are load-bearing and easy to lose:

* Paths are passed as ``-D`` parameters and referenced as ``(param "NAME")``
  rather than interpolated into the profile text, so a directory whose name
  contains a quote or a backslash cannot alter the policy.
* A writable root is normalized *without* resolving its own symlinks. Resolving
  them would grant the link's target, and the link is something the sandboxed
  command itself may be able to rewrite.

Worth knowing about scratch space: Seatbelt can only grant paths, it cannot
substitute private storage the way a mount namespace can. So an ephemeral write
root here means the platform's temp directories really are writable — the
per-user one macOS hands out, and ``/tmp``. That is what Codex grants too, and
it is the honest limit of this backend rather than an oversight; Linux and
Windows give the command private scratch because their mechanisms allow it.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .backend import BasePlatformBackend, SandboxLaunch
from .policy import (
    AccessMode,
    AccessRoot,
    NetworkPolicy,
    PolicyContext,
    ResolvedFileSystem,
    SandboxPolicy,
    filesystem_root,
    is_within,
    normalize_trusted_top_level_alias,
    resolve_symlinks,
)

# Only ever the system binary: an attacker who can put a different
# ``sandbox-exec`` earlier on PATH would otherwise choose the sandbox. If this
# path itself has been tampered with, the machine is already lost.
SEATBELT_EXECUTABLE = "/usr/bin/sandbox-exec"

_PROFILE_DIR = Path(__file__).resolve().parent / "profiles"


@lru_cache(maxsize=None)
def _profile(name: str) -> str:
    return (_PROFILE_DIR / f"{name}.sbpl").read_text(encoding="utf-8")


def _quote_regex_literal(value: str) -> str:
    """Escape ``value`` for a Seatbelt ``#"..."`` regex literal."""
    return re.escape(value).replace('"', '\\"')


def _protected_metadata_regex(root: str, name: str) -> str:
    """Anchored regex matching ``<root>/<name>`` and everything under it."""
    trimmed = root.rstrip("/") or "/"
    escaped_name = _quote_regex_literal(name)
    if trimmed == "/":
        return rf"^/{escaped_name}(/.*)?$"
    return rf"^{_quote_regex_literal(trimmed)}/{escaped_name}(/.*)?$"


class _ParamTable:
    """Accumulates ``-D`` definitions and hands back their ``(param ...)`` names."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []

    def add(self, name: str, value: str) -> str:
        self._items.append((name, value))
        return name

    @property
    def definitions(self) -> tuple[str, ...]:
        return tuple(f"-D{name}={value}" for name, value in self._items)


def _access_section(
    action: str,
    prefix: str,
    roots: Iterable[AccessRoot],
    params: _ParamTable,
    *,
    writable: bool,
) -> str:
    """Build one ``(allow file-read*|file-write* ...)`` section for ``roots``.

    Each root becomes a ``subpath`` filter; its read-only children, denied
    children and protected metadata names become ``require-not`` clauses on that
    same filter, which is how a narrower rule reopens or closes part of a
    broader one without rule ordering mattering.
    """
    components: list[str] = []
    anchor_denies: list[str] = []
    for index, access_root in enumerate(roots):
        root = (
            normalize_trusted_top_level_alias(access_root.root)
            if writable
            else resolve_symlinks(access_root.root)
        )
        root_param = params.add(f"{prefix}_{index}", root)
        if writable:
            # The sandboxed process must not be able to replace an authority
            # boundary that the next policy will be built from.
            anchor_denies.append(
                f'(deny file-write-unlink (require-all (literal (param "{root_param}"))'
                " (vnode-type DIRECTORY)))"
            )
        root_filter = f'(subpath (param "{root_param}"))'
        carve_outs = tuple(access_root.denied_subpaths) + tuple(
            access_root.read_only_subpaths if writable else ()
        )
        if not carve_outs and not access_root.protected_metadata_names:
            components.append(root_filter)
            continue

        require_parts = [root_filter]
        for carve_index, carve_out in enumerate(carve_outs):
            logical = (
                normalize_trusted_top_level_alias(carve_out)
                if writable
                else resolve_symlinks(carve_out)
            )
            carve_param = params.add(f"{prefix}_{index}_EXCLUDED_{carve_index}", logical)
            # Exclude the exact path as well as everything beneath it: `subpath`
            # alone still permits first-time creation of the path itself.
            require_parts.append(f'(require-not (literal (param "{carve_param}")))')
            require_parts.append(f'(require-not (subpath (param "{carve_param}")))')
            resolved = resolve_symlinks(logical)
            if writable and resolved != logical:
                resolved_param = params.add(
                    f"{prefix}_{index}_EXCLUDED_{carve_index}_RESOLVED", resolved
                )
                require_parts.append(f'(require-not (literal (param "{resolved_param}")))')
                require_parts.append(f'(require-not (subpath (param "{resolved_param}")))')
        for name in access_root.protected_metadata_names:
            regex = _protected_metadata_regex(root, name)
            require_parts.append(f'(require-not (regex #"{regex}"))')
        components.append("(require-all " + " ".join(require_parts) + " )")

    if not components:
        return ""
    sections = [f"(allow {action}\n" + " ".join(components) + "\n)"]
    sections.extend(anchor_denies)
    return "\n".join(sections)


def _network_section(network: NetworkPolicy) -> str:
    """Network rules, or nothing at all.

    With ``(deny default)`` at the top of the profile, emitting nothing *is* the
    restriction — there is no separate deny to write.
    """
    if not network.is_enabled:
        return ""
    return "(allow network-outbound)\n(allow network-inbound)\n" + _profile("network")


class SeatbeltBackend(BasePlatformBackend):
    """Confines a command with ``sandbox-exec`` and a generated profile."""

    name = "seatbelt"

    def unavailable_reason(self) -> str:
        if not os.path.exists(SEATBELT_EXECUTABLE):
            return f"本机缺少 macOS 沙箱运行器 {SEATBELT_EXECUTABLE}"
        return ""

    def _build_launch(
        self,
        resolved: ResolvedFileSystem,
        policy: SandboxPolicy,
        context: PolicyContext,
    ) -> SandboxLaunch:
        params = _ParamTable()
        sections = [_profile("base")]

        sections.append(self._read_section(resolved, params))
        sections.append(self._write_section(resolved, params))
        sections.append(_network_section(policy.network))
        if resolved.has_full_disk_read_access:
            sections.append(_profile("preferences"))
        if resolved.include_platform_defaults:
            sections.append(_profile("platform_defaults"))
            sections.append(_profile("process_defaults"))

        profile_text = "\n".join(section for section in sections if section)
        argv = (SEATBELT_EXECUTABLE, "-p", profile_text, *params.definitions, "--")
        return SandboxLaunch(backend=self.name, argv_prefix=argv)

    def _read_section(self, resolved: ResolvedFileSystem, params: _ParamTable) -> str:
        denied = resolved.roots_for(AccessMode.DENY)
        if resolved.has_full_disk_read_access:
            if not denied:
                return "; allow read-only file operations\n(allow file-read*)"
            root = AccessRoot(
                root=filesystem_root(),
                denied_subpaths=tuple(entry.root for entry in denied),
            )
            section = _access_section("file-read*", "READABLE_ROOT", [root], params, writable=False)
            return f"; allow read-only file operations\n{section}"

        readable = resolved.roots_for(AccessMode.READ)
        scoped = [
            AccessRoot(
                root=entry.root,
                denied_subpaths=tuple(
                    denied_entry.root
                    for denied_entry in denied
                    if is_within(denied_entry.root, entry.root)
                ),
            )
            for entry in readable
        ]
        section = _access_section("file-read*", "READABLE_ROOT", scoped, params, writable=False)
        return f"; allow read-only file operations\n{section}" if section else ""

    def _write_section(self, resolved: ResolvedFileSystem, params: _ParamTable) -> str:
        if resolved.has_full_disk_write_access:
            denied = resolved.roots_for(AccessMode.DENY)
            root = AccessRoot(
                root=filesystem_root(),
                denied_subpaths=tuple(entry.root for entry in denied),
                protected_metadata_names=resolved.protected_metadata_names,
            )
            return _access_section("file-write*", "WRITABLE_ROOT", [root], params, writable=True)
        writable = resolved.roots_for(AccessMode.WRITE)
        return _access_section("file-write*", "WRITABLE_ROOT", writable, params, writable=True)


__all__ = ["SEATBELT_EXECUTABLE", "SeatbeltBackend"]
