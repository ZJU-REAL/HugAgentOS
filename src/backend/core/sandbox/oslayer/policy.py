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
the caller's directory layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from .errors import SandboxPolicyError


class AccessMode(str, Enum):
    """What a filesystem entry grants.

    ``WRITE`` implies ``READ``; ``DENY`` removes both. When two entries of equal
    specificity target the same path they are compared by conflict precedence
    rather than by breadth, so the more restrictive one wins.
    """

    READ = "read"
    WRITE = "write"
    DENY = "deny"

    @property
    def can_read(self) -> bool:
        return self is not AccessMode.DENY

    @property
    def can_write(self) -> bool:
        return self is AccessMode.WRITE

    @property
    def precedence(self) -> int:
        return _ACCESS_PRECEDENCE[self]


_ACCESS_PRECEDENCE = {AccessMode.READ: 0, AccessMode.WRITE: 1, AccessMode.DENY: 2}


class SpecialPath(str, Enum):
    """Symbolic path tokens resolved against a :class:`PolicyContext`.

    ``ROOT`` is the whole filesystem, ``PROJECT_ROOTS`` expands to every
    workspace root the run is anchored on, and the two temp tokens exist because
    "may write scratch files" is a decision the policy owner makes, not a
    courtesy the backend grants on its own.

    ``MINIMAL`` names no path. It is the policy's request for the platform's
    baseline of system paths and services — the loader, the shared libraries,
    the logging and directory-service endpoints a process needs before it can
    execute anything at all. A policy that narrows reads has to ask for it
    explicitly, which is what keeps the baseline out of policies that already
    grant full read access.
    """

    ROOT = "root"
    MINIMAL = "minimal"
    PROJECT_ROOTS = "project_roots"
    HOME = "home"
    TMPDIR = "tmpdir"
    SLASH_TMP = "slash_tmp"


class FileSystemKind(str, Enum):
    """Whether the filesystem dimension is enforced at all.

    ``UNRESTRICTED`` is the explicit "no filesystem confinement" choice a user
    makes by picking the unrestricted permission preset. It is a policy the
    caller states, never a state the layer falls back into.
    """

    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


class NetworkPolicy(str, Enum):
    RESTRICTED = "restricted"
    ENABLED = "enabled"

    @property
    def is_enabled(self) -> bool:
        return self is NetworkPolicy.ENABLED


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


@dataclass(frozen=True)
class PolicyContext:
    """Executor-owned facts needed to resolve a policy's symbolic paths.

    ``protected_metadata_names`` are basenames that stay read-only inside every
    writable root — the directories that describe the project's own authority
    (version control metadata, agent instructions, the agent's own home). The
    caller supplies them because their names come from the caller's deployment,
    not from this layer.

    ``state_dir`` is where a backend may keep per-host state it must remember
    between runs. Only the Windows backend needs it today, for the capability
    SIDs whose grants are written onto the authorized folders themselves.
    """

    cwd: str
    workspace_roots: tuple[str, ...] = ()
    home_dir: Optional[str] = None
    temp_dirs: tuple[str, ...] = ()
    protected_metadata_names: tuple[str, ...] = ()
    state_dir: Optional[str] = None

    def resolve_special(self, special: SpecialPath) -> tuple[str, ...]:
        if special is SpecialPath.ROOT:
            return (filesystem_root(),)
        if special is SpecialPath.MINIMAL:
            # Names no path: it is a request for the platform baseline, which
            # each backend expresses in its own vocabulary.
            return ()
        if special is SpecialPath.PROJECT_ROOTS:
            roots = self.workspace_roots or (self.cwd,)
            return tuple(normalize_logical(root) for root in roots)
        if special is SpecialPath.HOME:
            if not self.home_dir:
                raise SandboxPolicyError("策略引用了 home 目录，但执行上下文没有提供")
            return (normalize_logical(self.home_dir),)
        if special is SpecialPath.TMPDIR:
            if not self.temp_dirs:
                raise SandboxPolicyError("策略引用了临时目录，但执行上下文没有提供")
            return tuple(normalize_logical(temp) for temp in self.temp_dirs)
        if special is SpecialPath.SLASH_TMP:
            return (normalize_logical("/tmp"),)
        raise SandboxPolicyError(f"未知的特殊路径：{special}")

    def to_json(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "workspace_roots": list(self.workspace_roots),
            "home_dir": self.home_dir,
            "temp_dirs": list(self.temp_dirs),
            "protected_metadata_names": list(self.protected_metadata_names),
            "state_dir": self.state_dir,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "PolicyContext":
        return cls(
            cwd=str(payload["cwd"]),
            workspace_roots=tuple(str(item) for item in payload.get("workspace_roots") or ()),
            home_dir=payload.get("home_dir") or None,
            temp_dirs=tuple(str(item) for item in payload.get("temp_dirs") or ()),
            protected_metadata_names=tuple(
                str(item) for item in payload.get("protected_metadata_names") or ()
            ),
            state_dir=payload.get("state_dir") or None,
        )


def filesystem_root() -> str:
    """The single root every backend anchors ``:root`` on."""
    return os.path.abspath(os.sep)


@dataclass(frozen=True)
class FileSystemEntry:
    """One path → access-mode rule.

    Exactly one of ``path`` / ``special`` is set. ``skip_if_missing`` marks an
    entry that describes a path which may legitimately not exist yet; backends
    drop it rather than materializing a placeholder.

    ``ephemeral`` marks a write root that exists only as scratch space for this
    command. A backend that can provide private storage should — handing out the
    host's shared temp directory instead would open a write channel between the
    sandbox and every other process on the machine, which is not what "the
    command may write scratch files" was meant to grant.
    """

    access: AccessMode
    path: Optional[str] = None
    special: Optional[SpecialPath] = None
    skip_if_missing: bool = False
    ephemeral: bool = False

    def __post_init__(self) -> None:
        if (self.path is None) == (self.special is None):
            raise SandboxPolicyError("文件系统策略条目必须且只能给出 path 或 special 之一")

    def resolve(self, context: PolicyContext) -> tuple[str, ...]:
        if self.special is not None:
            return context.resolve_special(self.special)
        return (normalize_logical(self.path or ""),)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"access": self.access.value}
        if self.special is not None:
            payload["special"] = self.special.value
        else:
            payload["path"] = self.path
        if self.skip_if_missing:
            payload["skip_if_missing"] = True
        if self.ephemeral:
            payload["ephemeral"] = True
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FileSystemEntry":
        special = payload.get("special")
        return cls(
            access=AccessMode(payload["access"]),
            path=payload.get("path"),
            special=SpecialPath(special) if special else None,
            skip_if_missing=bool(payload.get("skip_if_missing")),
            ephemeral=bool(payload.get("ephemeral")),
        )


@dataclass(frozen=True)
class ResolvedEntry:
    """A policy entry with its symbolic path expanded to one absolute path."""

    path: str
    access: AccessMode
    skip_if_missing: bool = False
    ephemeral: bool = False


@dataclass(frozen=True)
class AccessRoot:
    """A root a backend has to express, with the carve-outs that apply inside it.

    ``read_only_subpaths`` and ``denied_subpaths`` are the narrower entries that
    override this root. ``protected_metadata_names`` are basenames that must stay
    read-only anywhere directly inside the root.
    """

    root: str
    read_only_subpaths: tuple[str, ...] = ()
    denied_subpaths: tuple[str, ...] = ()
    protected_metadata_names: tuple[str, ...] = ()
    ephemeral: bool = False


@dataclass(frozen=True)
class ResolvedFileSystem:
    """The backend-facing view of a filesystem policy resolved for one run."""

    kind: FileSystemKind
    entries: tuple[ResolvedEntry, ...]
    protected_metadata_names: tuple[str, ...]
    include_platform_defaults: bool = False

    @property
    def is_restricted(self) -> bool:
        return self.kind is FileSystemKind.RESTRICTED

    def effective_access(self, path: str) -> AccessMode:
        """Access mode that applies to ``path``, narrowest matching entry wins."""
        if not self.is_restricted:
            return AccessMode.WRITE
        winner = AccessMode.DENY
        best_depth = -1
        for entry in self.entries:
            if not is_within(path, entry.path):
                continue
            depth = path_depth(entry.path)
            if depth > best_depth:
                best_depth, winner = depth, entry.access
            elif depth == best_depth and entry.access.precedence > winner.precedence:
                winner = entry.access
        return winner

    @property
    def has_full_disk_read_access(self) -> bool:
        return not self.is_restricted or self.effective_access(filesystem_root()).can_read

    @property
    def has_full_disk_write_access(self) -> bool:
        return not self.is_restricted or self.effective_access(filesystem_root()).can_write

    def roots_for(self, access: AccessMode) -> tuple[AccessRoot, ...]:
        """Roots whose *own* effective mode is ``access``, with their carve-outs.

        A write root is also a read root; asking for :attr:`AccessMode.READ`
        therefore returns every root the command may read, write roots included.
        """
        wanted = [
            entry
            for entry in self.entries
            if self._effective_for_entry(entry) is access
            or (access is AccessMode.READ and self._effective_for_entry(entry).can_read)
        ]
        roots: list[AccessRoot] = []
        for entry in wanted:
            read_only: list[str] = []
            denied: list[str] = []
            for other in self.entries:
                if other.path == entry.path or not is_within(other.path, entry.path):
                    continue
                other_access = self._effective_for_entry(other)
                if other_access is AccessMode.DENY:
                    denied.append(other.path)
                elif access is AccessMode.WRITE and not other_access.can_write:
                    read_only.append(other.path)
            roots.append(
                AccessRoot(
                    root=entry.path,
                    read_only_subpaths=tuple(sorted(set(read_only))),
                    denied_subpaths=tuple(sorted(set(denied))),
                    protected_metadata_names=(
                        self._protected_names_for(entry.path)
                        if access is AccessMode.WRITE and not entry.ephemeral
                        else ()
                    ),
                    ephemeral=entry.ephemeral,
                )
            )
        return tuple(roots)

    def _effective_for_entry(self, entry: ResolvedEntry) -> AccessMode:
        """Mode that actually applies *at* an entry's own path.

        An entry can be overridden at its own path by an equally specific, more
        restrictive entry, so this is not simply ``entry.access``.
        """
        return self.effective_access(entry.path)

    def _protected_names_for(self, root: str) -> tuple[str, ...]:
        """Protected basenames that no entry explicitly opens for writing.

        An entry naming ``<root>/.git`` directly is a deliberate choice by the
        policy author and is honoured; the protection only covers the names
        nobody asked to open up.
        """
        explicit = {entry.path for entry in self.entries if entry.access is AccessMode.WRITE}
        names = [
            name
            for name in self.protected_metadata_names
            if os.path.join(root, name) not in explicit
        ]
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class FileSystemPolicy:
    """Filesystem dimension of a sandbox policy."""

    kind: FileSystemKind = FileSystemKind.RESTRICTED
    entries: tuple[FileSystemEntry, ...] = ()

    def resolve(self, context: PolicyContext) -> ResolvedFileSystem:
        resolved: list[ResolvedEntry] = []
        for entry in self.entries:
            for path in entry.resolve(context):
                if entry.skip_if_missing and not os.path.exists(path):
                    continue
                resolved.append(
                    ResolvedEntry(
                        path=path,
                        access=entry.access,
                        skip_if_missing=entry.skip_if_missing,
                        ephemeral=entry.ephemeral,
                    )
                )
        deduped: dict[str, ResolvedEntry] = {}
        for entry in resolved:
            existing = deduped.get(entry.path)
            if existing is None or entry.access.precedence > existing.access.precedence:
                deduped[entry.path] = entry
        ordered = sorted(deduped.values(), key=lambda item: (path_depth(item.path), item.path))
        wants_baseline = any(
            entry.special is SpecialPath.MINIMAL and entry.access.can_read for entry in self.entries
        )
        return ResolvedFileSystem(
            kind=self.kind,
            entries=tuple(ordered),
            protected_metadata_names=context.protected_metadata_names,
            include_platform_defaults=wants_baseline and self.kind is FileSystemKind.RESTRICTED,
        )

    def with_entries(self, entries: Iterable[FileSystemEntry]) -> "FileSystemPolicy":
        return replace(self, entries=tuple(self.entries) + tuple(entries))

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FileSystemPolicy":
        return cls(
            kind=FileSystemKind(payload.get("kind", FileSystemKind.RESTRICTED.value)),
            entries=tuple(FileSystemEntry.from_json(item) for item in payload.get("entries") or ()),
        )


@dataclass(frozen=True)
class SandboxPolicy:
    """The complete confinement request: filesystem plus network."""

    filesystem: FileSystemPolicy = field(default_factory=FileSystemPolicy)
    network: NetworkPolicy = NetworkPolicy.RESTRICTED

    @property
    def is_unconfined(self) -> bool:
        """True when the policy asks for nothing a backend could enforce."""
        return self.filesystem.kind is FileSystemKind.UNRESTRICTED and self.network.is_enabled

    def to_json(self) -> dict[str, Any]:
        return {"filesystem": self.filesystem.to_json(), "network": self.network.value}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SandboxPolicy":
        return cls(
            filesystem=FileSystemPolicy.from_json(payload.get("filesystem") or {}),
            network=NetworkPolicy(payload.get("network", NetworkPolicy.RESTRICTED.value)),
        )


def unrestricted_policy() -> SandboxPolicy:
    """The policy that states "confine nothing" — chosen, never fallen into."""
    return SandboxPolicy(
        filesystem=FileSystemPolicy(kind=FileSystemKind.UNRESTRICTED),
        network=NetworkPolicy.ENABLED,
    )


def build_filesystem_policy(
    *,
    writable_roots: Sequence[str],
    readable_roots: Sequence[str] = (),
    denied_paths: Sequence[str] = (),
    full_disk_read: bool = True,
    writable_temp: bool = True,
) -> FileSystemPolicy:
    """Assemble a restricted filesystem policy from a caller's access decision.

    The parameters are the vocabulary the permission layer already speaks —
    which folders the user authorized for writing, which for reading only, which
    paths are off-limits — so callers never hand-build entry lists.
    """
    entries: list[FileSystemEntry] = []
    if full_disk_read:
        entries.append(FileSystemEntry(access=AccessMode.READ, special=SpecialPath.ROOT))
    else:
        # Narrowed reads still need the platform's own baseline, or the command
        # cannot load a shared library, let alone run.
        entries.append(FileSystemEntry(access=AccessMode.READ, special=SpecialPath.MINIMAL))
    for root in readable_roots:
        entries.append(FileSystemEntry(access=AccessMode.READ, path=root))
    for root in writable_roots:
        entries.append(FileSystemEntry(access=AccessMode.WRITE, path=root))
    if writable_temp:
        entries.append(
            FileSystemEntry(access=AccessMode.WRITE, special=SpecialPath.TMPDIR, ephemeral=True)
        )
        entries.append(
            FileSystemEntry(
                access=AccessMode.WRITE,
                special=SpecialPath.SLASH_TMP,
                skip_if_missing=True,
                ephemeral=True,
            )
        )
    for path in denied_paths:
        entries.append(FileSystemEntry(access=AccessMode.DENY, path=path))
    return FileSystemPolicy(kind=FileSystemKind.RESTRICTED, entries=tuple(entries))


__all__ = [
    "AccessMode",
    "AccessRoot",
    "FileSystemEntry",
    "FileSystemKind",
    "FileSystemPolicy",
    "NetworkPolicy",
    "PolicyContext",
    "ResolvedEntry",
    "ResolvedFileSystem",
    "SandboxPolicy",
    "SpecialPath",
    "build_filesystem_policy",
    "filesystem_root",
    "is_within",
    "normalize_logical",
    "normalize_trusted_top_level_alias",
    "path_depth",
    "resolve_symlinks",
    "unrestricted_policy",
]
