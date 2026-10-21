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

from typing import Sequence

from core.sandbox.oslayer.models import (
    AccessMode,
    FileSystemEntry,
    FileSystemKind,
    FileSystemPolicy,
    NetworkPolicy,
    SandboxPolicy,
    SpecialPath,
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
