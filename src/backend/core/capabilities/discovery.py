"""Discover user-dropped skill folders without traversing managed revisions."""

import threading
import re
from . import archive, registry, skills
from .errors import CapabilityError
from .paths import capabilities_enabled, kind_root, assert_managed_path
from core.agent_skills.registry import _load_skill_metadata_from_str

_lock = threading.RLock()


def scan(user_id):
    if not user_id or not capabilities_enabled():
        return []
    errors = []
    with _lock:
        root = kind_root("skill")
        for parent in (root, root / "local"):
            if not parent.is_dir():
                continue
            try:
                assert_managed_path(parent)
                folders = sorted(parent.iterdir())
            except (CapabilityError, OSError):
                continue
            for folder in folders:
                try:
                    if parent == root and (
                        folder.name in ("local", "builtin")
                        or re.fullmatch(r"p_[0-9a-f]{10,32}", folder.name)
                    ):
                        continue
                    if not folder.is_dir() or not (folder / "SKILL.md").is_file():
                        continue
                    assert_managed_path(folder)
                    entries = list(archive.iter_files(folder))
                    sizes = [file.stat().st_size for _, file in entries]
                    if (
                        len(entries) > archive.MAX_MEMBERS
                        or sum(sizes) > archive.MAX_TOTAL_BYTES
                        or any(size > archive.MAX_MEMBER_BYTES for size in sizes)
                    ):
                        raise ValueError("skill folder exceeds package limits")
                    md = (folder / "SKILL.md").read_text(encoding="utf-8")
                    metadata = _load_skill_metadata_from_str(md, folder.name)
                    iid = registry.install_id("skill", "local", metadata.id)
                    existing = registry.get(iid)
                    if existing and (
                        existing.payload.get("owner_user_id") != str(user_id)
                        or existing.payload.get("discovered_path") != str(folder)
                    ):
                        errors.append({"folder": folder.name, "code": "name_conflict"})
                        continue
                    digest = skills.skill_dir_hash(folder)
                    if existing and existing.payload.get("discovered_hash") == digest:
                        continue
                    files = {name: file.read_bytes() for name, file in entries}
                    skills.publish_local_skill(
                        metadata.id,
                        files=files,
                        content_hash=digest,
                        owner_user_id=str(user_id),
                        display_name=metadata.name,
                        description=metadata.description,
                        version=metadata.version,
                        enabled=existing.enabled if existing else True,
                    )
                    row = registry.get(iid)
                    registry.set_state(
                        iid,
                        row.state,
                        payload_update={"discovered_path": str(folder), "discovered_hash": digest},
                    )
                except (CapabilityError, OSError, ValueError) as exc:
                    errors.append(
                        {"folder": folder.name, "code": getattr(exc, "code", "invalid_skill")}
                    )
    return errors
