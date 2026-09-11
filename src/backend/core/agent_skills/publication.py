"""Authoritative, complete skill publication shared by loaders and sandbox preparation.

Derived directories are never a source of truth. Writers serialize across processes,
read the current source while holding the lock, and publish a complete tree. Staging
and retired trees live outside every skill mount.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from core.capabilities.lockfile import locked as file_lock

logger = logging.getLogger(__name__)

_thread_lock = threading.RLock()
_local = threading.local()


@contextmanager
def publication_lock():
    from .config import get_sandbox_skills_dir

    with _thread_lock:
        if getattr(_local, "held", False):
            yield
            return
        root = get_sandbox_skills_dir()
        with file_lock(root.parent / f".{root.name}.publication.lock"):
            _local.held = True
            try:
                yield
            finally:
                _local.held = False


def safe_relative(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in name.split("/"))
        or ":" in path.parts[0]
    ):
        raise ValueError(f"Invalid skill file path: {name!r}")
    return path.as_posix()


def normalize_file_path(name: str) -> str:
    """Accept legacy Windows separators, then apply the strict relative-path policy."""
    return safe_relative(name.replace("\\", "/"))


def safe_skill_id(skill_id: str) -> str:
    if "/" in safe_relative(skill_id) or skill_id.startswith("."):
        raise ValueError("Invalid skill id")
    return skill_id


def decode_files(content: str, extra: dict) -> dict[str, bytes]:
    from .binary_files import decode_binary, is_binary_value

    files = {"SKILL.md": content.encode("utf-8")}
    for name, value in extra.items():
        name = normalize_file_path(name)
        if name in files:
            raise ValueError(f"Duplicate skill file path: {name!r}")
        files[name] = decode_binary(value) if is_binary_value(value) else value.encode("utf-8")
    for name in files:
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
            raise ValueError(f"Conflicting skill file path: {name!r}")
    return files


def read_tree(root: Path) -> dict[str, bytes]:
    """Read the shipped source, never following links outside a package."""
    files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if set(rel.parts) & {".git", "__pycache__", ".svn", ".hg"} or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"Skill package contains a symlink: {rel}")
        if path.is_file():
            files[safe_relative(rel.as_posix())] = path.read_bytes()
    return files


def remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def publish_tree(
    target: Path, files: dict[str, bytes], *, modes: dict[str, int] | None = None
) -> Path:
    """Publish an exact tree; failure before the swap leaves the old tree intact."""
    from .config import get_sandbox_skills_dir

    safe_skill_id(target.name)
    files = {safe_relative(name): data for name, data in files.items()}
    modes = modes or {}
    expected_dirs = {
        parent.as_posix()
        for name in files
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    with publication_lock():
        if target.is_dir() and not target.is_symlink():
            entries = list(target.rglob("*"))
            if (
                not any(p.is_symlink() for p in entries)
                and {p.relative_to(target).as_posix() for p in entries if p.is_file()} == set(files)
                and {p.relative_to(target).as_posix() for p in entries if p.is_dir()}
                == expected_dirs
                and all(
                    (target / name).read_bytes() == data
                    and ((target / name).stat().st_mode & 0o777) == modes.get(name, 0o644)
                    for name, data in files.items()
                )
            ):
                return target
        area = get_sandbox_skills_dir().parent / ".skill-publication"
        area.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="publish-", dir=area) as tmp:
            stage = Path(tmp) / "new"
            stage.mkdir()
            for name, data in files.items():
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(modes.get(name, 0o644))
            backup = Path(tmp) / "old"
            had_old = target.exists() or target.is_symlink()
            if had_old:
                os.replace(target, backup)
            try:
                os.replace(stage, target)
            except BaseException:
                if had_old:
                    os.replace(backup, target)
                raise
        return target


def prepare_skill_view(user_id: str | None = None) -> Path | None:
    """Reconcile from fresh sources before giving a new sandbox its mount.

    The publication lock also fences old loader instances. Preparation errors
    propagate: serving an unreconciled directory would violate deletion semantics.
    """
    from core.capabilities.paths import capabilities_enabled
    from .config import (
        get_sandbox_skills_dir,
        get_user_skills_dir,
        sync_user_skill_view,
    )
    from .loader import MultiSourceSkillLoader

    if capabilities_enabled():
        return sync_user_skill_view(user_id)
    with publication_lock():
        backend = MultiSourceSkillLoader._create_default_backend()
        shared = get_sandbox_skills_dir()
        private = get_user_skills_dir(user_id)
        wanted_shared, wanted_private = set(), set()
        # Read every selected source afresh. A stale on-disk folder never makes
        # a skill live, and DB payload/owner come from the same SELECT.
        selections = [(backend.scoped(), False)]
        if user_id:
            selections.append((backend.scoped(user_id), True))
        for selected, private_only in selections:
            _publish_selected(selected, user_id, private_only, wanted_shared, wanted_private)
        for entry in shared.iterdir():
            if entry.name not in wanted_shared:
                remove_entry(entry)
        if private is not None:
            private.mkdir(parents=True, exist_ok=True)
            for entry in private.iterdir():
                if entry.name not in wanted_private:
                    remove_entry(entry)
        return sync_user_skill_view(user_id)


def _publish_selected(backend, user_id, private_only, wanted_shared, wanted_private):
    from .config import skill_files_dir

    for info in backend.list_skill_files():
        owner = (info.metadata or {}).get("owner_user_id")
        if bool(owner) != private_only or (owner and owner != user_id):
            continue
        try:
            content, extra, owner = backend.read_snapshot(info.skill_id)
        except FileNotFoundError:
            continue
        if bool(owner) != private_only or (owner and owner != user_id):
            continue
        try:
            safe_skill_id(info.skill_id)
            files = (
                decode_files(content, extra)
                if info.is_database or info.content is not None
                else read_tree(info.file_path.parent)
            )
        except ValueError as exc:
            # Exclude invalid packages from the reconciled view, including any
            # previously published copy. One bad skill must not block login.
            logger.warning("skill_publication_invalid skill_id=%r: %s", info.skill_id, exc)
            continue
        target = skill_files_dir(info.skill_id, owner)
        modes = None
        if not info.is_database and info.content is None:
            modes = {name: (info.file_path.parent / name).stat().st_mode & 0o777 for name in files}
        publish_tree(target, files, modes=modes)
        (wanted_private if owner else wanted_shared).add(info.skill_id)
