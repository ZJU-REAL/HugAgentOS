"""The partitioned, immutable file store: ``<root>/<kind>s/<profile>/<key>/<revision>/``.

Writes go to a staging directory and are published with one ``os.replace`` so a
half-written revision is never visible. A revision is never modified in place —
new content means a new revision directory; the view is re-pointed afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import archive
from .errors import IntegrityFailed
from .paths import (
    KIND_AGENT,
    KIND_PLUGIN,
    KIND_SKILL,
    component_dir,
    assert_managed_path,
    kind_root,
    profile_dir,
    safe_segment,
    staging_root,
)

_ENTRY_FILE = {KIND_SKILL: "SKILL.md", KIND_PLUGIN: "plugin.json", KIND_AGENT: "agent.json"}
_INVENTORY = ".inventory.json"


@dataclass(frozen=True)
class StoredComponent:
    kind: str
    profile: str
    key: str
    revision: str
    path: Path

    @property
    def entry_file(self) -> Path:
        return self.path / _ENTRY_FILE[self.kind]


def entry_file_name(kind: str) -> str:
    return _ENTRY_FILE[kind]


def _validate_entry(kind: str, path: Path) -> None:
    if not (path / _ENTRY_FILE[kind]).is_file():
        raise IntegrityFailed(f"{kind} package has no {_ENTRY_FILE[kind]}")


def _publish(kind: str, profile: str, key: str, revision: str, staging: Path) -> StoredComponent:
    _validate_entry(kind, staging)
    inventory = [rel for rel, _ in archive.iter_files(staging)]
    (staging / _INVENTORY).write_text(
        json.dumps({"files": inventory}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    final = component_dir(kind, profile, key, revision)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        # Immutable: same revision already published. Discard the staged copy.
        shutil.rmtree(staging, ignore_errors=True)
    else:
        os.replace(staging, final)
    return StoredComponent(kind, profile, key, revision, final)


def _staging_dir() -> Path:
    return staging_root() / uuid.uuid4().hex


def write_from_zip(
    kind: str, profile: str, key: str, revision: str, data: bytes
) -> StoredComponent:
    staging = _staging_dir()
    try:
        archive.extract_zip(data, staging)
        return _publish(kind, profile, key, revision, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def write_from_files(
    kind: str, profile: str, key: str, revision: str, files: Dict[str, bytes | str]
) -> StoredComponent:
    staging = _staging_dir()
    try:
        archive.write_files(staging, files)
        return _publish(kind, profile, key, revision, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def get(kind: str, profile: str, key: str, revision: str) -> Optional[StoredComponent]:
    path = component_dir(kind, profile, key, revision)
    if not path.is_dir() or not (path / _ENTRY_FILE[kind]).is_file():
        return None
    return StoredComponent(kind, profile, key, revision, path)


def iter_components(kind: str, profile: Optional[str] = None) -> Iterator[StoredComponent]:
    root = kind_root(kind)
    profiles = (
        [profile_dir(kind, profile)] if profile else sorted(p for p in root.iterdir() if p.is_dir())
    )
    for pdir in profiles:
        assert_managed_path(pdir)
        if not pdir.is_dir():
            continue
        for kdir in sorted(p for p in pdir.iterdir() if p.is_dir()):
            assert_managed_path(kdir)
            for rdir in sorted(p for p in kdir.iterdir() if p.is_dir()):
                assert_managed_path(rdir)
                if (rdir / _ENTRY_FILE[kind]).is_file():
                    yield StoredComponent(kind, pdir.name, kdir.name, rdir.name, rdir)


def revisions(kind: str, profile: str, key: str) -> List[StoredComponent]:
    return [c for c in iter_components(kind, profile) if c.key == key]


def remove_revision(kind: str, profile: str, key: str, revision: str) -> bool:
    path = component_dir(kind, profile, key, revision)
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return True


def remove_key(kind: str, profile: str, key: str) -> int:
    removed = 0
    for comp in revisions(kind, profile, key):
        removed += int(remove_revision(kind, profile, key, comp.revision))
    return removed


def content_hash_of_dir(path: Path) -> str:
    """Order-independent hash of a stored component (inventory file excluded)."""
    digest = hashlib.sha256()
    for rel, file in archive.iter_files(path):
        if rel == _INVENTORY:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def inventory(component: StoredComponent) -> List[str]:
    try:
        return list(json.loads((component.path / _INVENTORY).read_text(encoding="utf-8"))["files"])
    except (OSError, ValueError, KeyError):
        return [rel for rel, _ in archive.iter_files(component.path) if rel != _INVENTORY]


def flat_import_candidates(kind: str) -> List[Path]:
    """Directories sitting directly under ``<kind>s/`` that are not profiles.

    A user who dropped ``<root>/skills/my-skill/SKILL.md`` by hand gets that
    folder imported into the ``local`` profile by the migration step; it is never
    deleted just because the scanner did not expect it.
    """
    root = kind_root(kind)
    entry = _ENTRY_FILE[kind]
    found: List[Path] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        assert_managed_path(child)
        if (child / entry).is_file():
            try:
                safe_segment(child.name)
            except ValueError:
                continue
            found.append(child)
    return found
