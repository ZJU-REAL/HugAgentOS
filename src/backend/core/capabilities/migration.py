"""One-time, idempotent move from the pre-store layout to the capability store.

Order: copy/verify → switch → observe → clean. Nothing that might be user data
is deleted here; it is moved into ``<root>/.capabilities/migrations/quarantine/``
with a log entry so a person can inspect or restore it. Re-materializable caches
(DB-skill copies, session workspace copies, old view links) are the only things
removed outright, and only because the store rebuilds them from their source.

Legacy locations handled:

- ``<workspace>/skills/<id>``            real dirs (built-in copies, DB materializations, cloud mirrors)
- ``<workspace>/skills_u/<uid>/<id>``    real dirs (private materializations) and old view symlinks
- ``<workspace>/skills_u/skills_shared`` the old shared-hop symlink
- ``<workspace>/skills_cloud``           the old cloud snapshot directory (account unknown → quarantine)
- ``<workspace>/.sessions/*/skills``     per-session copies left by the copytree fallback
- ``<root>/skills/<id>/SKILL.md``        hand-dropped flat skills → imported into the ``local`` profile
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import junction, skills, store
from .paths import KIND_SKILL, LOCAL_PROFILE, capabilities_enabled, migrations_root, safe_segment

logger = logging.getLogger(__name__)


@dataclass
class MigrationReport:
    started_at: float = field(default_factory=time.time)
    quarantine_dir: Optional[str] = None
    imported: List[str] = field(default_factory=list)
    quarantined: List[str] = field(default_factory=list)
    removed_links: List[str] = field(default_factory=list)
    removed_session_copies: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.imported or self.quarantined or self.removed_links or self.removed_session_copies
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _quarantine(report: MigrationReport, path: Path, label: str) -> None:
    if report.quarantine_dir is None:
        qdir = migrations_root() / "quarantine" / time.strftime("%Y%m%d-%H%M%S")
        qdir.mkdir(parents=True, exist_ok=True)
        report.quarantine_dir = str(qdir)
    target = Path(report.quarantine_dir) / label / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target = target.with_name(f"{target.name}-{int(time.time() * 1000)}")
    shutil.move(str(path), str(target))
    report.quarantined.append(f"{path} -> {target}")


def _sweep_view_dir(report: MigrationReport, view: Path) -> None:
    """Old view dirs held real copies and symlinks; the store version holds links only."""
    if not view.is_dir():
        return
    for entry in list(view.iterdir()):
        if junction.is_directory_link(entry):
            junction.remove_directory_link(entry)
            report.removed_links.append(str(entry))
        elif entry.is_dir() and (entry / "SKILL.md").is_file():
            _quarantine(report, entry, f"view-{view.name}")


def _import_flat_skills(report: MigrationReport) -> None:
    from core.agent_skills.binary_files import decode_binary, is_binary_value, pack_directory
    from core.services.desktop_capability_protocol import skill_content_hash

    for flat in store.flat_import_candidates(KIND_SKILL):
        try:
            packed = pack_directory(flat)
            content = packed.pop("SKILL.md", "")
            files = {"SKILL.md": content}
            for rel, body in packed.items():
                files[rel] = decode_binary(body) if is_binary_value(body) else body
            skills.publish_local_skill(
                safe_segment(flat.name),
                files=files,
                content_hash=skill_content_hash(content, packed),
                display_name=flat.name,
            )
        except Exception as exc:  # noqa: BLE001 - leave the folder where it is, say why
            report.skipped[str(flat)] = f"import failed: {exc}"
            continue
        _quarantine(report, flat, "imported-flat")
        report.imported.append(flat.name)


def _tree_inventory(root: Path) -> Optional[Dict[str, str]]:
    """Full relative directory/file inventory; links and unreadable data block cleanup."""
    if not root.is_dir() or junction.is_directory_link(root):
        return None
    inventory: Dict[str, str] = {}
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            for entry in directory.iterdir():
                if junction.is_directory_link(entry):
                    return None
                relative = entry.relative_to(root).as_posix()
                if entry.is_dir():
                    inventory[relative] = "directory"
                    pending.append(entry)
                elif entry.is_file():
                    digest = hashlib.sha256()
                    with entry.open("rb") as fh:
                        for block in iter(lambda: fh.read(1024 * 1024), b""):
                            digest.update(block)
                    inventory[relative] = f"sha256:{digest.hexdigest()}"
                else:
                    return None
    except OSError:
        return None
    return inventory


def _session_copy_sources(report: MigrationReport, workspace: Path) -> Dict[str, List[Path]]:
    """Preserved original views and immutable components from which copies can be rebuilt."""
    roots = [workspace / "skills", skills.builtin_dir()]
    users = workspace / "skills_u"
    if users.is_dir():
        roots.extend(
            entry
            for entry in users.iterdir()
            if entry.is_dir() and not junction.is_directory_link(entry)
        )
    # The earlier migration phase moved original view directories aside. Those
    # preserved originals remain valid sources, with their complete bytes intact.
    if report.quarantine_dir:
        roots.extend(Path(report.quarantine_dir).glob("view-*"))
    sources: Dict[str, List[Path]] = {}
    for root in roots:
        if not root.is_dir() or junction.is_directory_link(root):
            continue
        for entry in root.iterdir():
            if (
                entry.is_dir()
                and not junction.is_directory_link(entry)
                and (entry / "SKILL.md").is_file()
            ):
                sources.setdefault(entry.name, []).append(entry)
    if capabilities_enabled():
        for component in store.iter_components(KIND_SKILL):
            sources.setdefault(component.key, []).append(component.path)
    return sources


def _sweep_sessions(report: MigrationReport, workspace: Path) -> None:
    sessions = workspace / ".sessions"
    if not sessions.is_dir() or junction.is_directory_link(sessions):
        return
    sources = _session_copy_sources(report, workspace)
    for session in sessions.iterdir():
        # Never traverse a session-directory link into some other workspace.
        if not session.is_dir() or junction.is_directory_link(session):
            continue
        link = session / "skills"
        if junction.is_directory_link(link):
            junction.remove_directory_link(link)
            report.removed_links.append(str(link))
        elif link.is_dir():
            before = _tree_inventory(link)
            entries = list(link.iterdir())
            if (
                not before
                or not entries
                or any(
                    not child.is_dir()
                    or junction.is_directory_link(child)
                    or not (child / "SKILL.md").is_file()
                    for child in entries
                )
            ):
                report.skipped[str(link)] = "empty, unknown or non-skill entries; left untouched"
                continue
            matches = []
            for child in entries:
                copied_inventory = _tree_inventory(child)
                original = next(
                    (
                        source
                        for source in sources.get(child.name, [])
                        if copied_inventory and _tree_inventory(source) == copied_inventory
                    ),
                    None,
                )
                if original is None:
                    break
                matches.append((original, copied_inventory))
            if len(matches) != len(entries):
                report.skipped[str(link)] = (
                    "no identical preserved source for every copied skill; left untouched"
                )
                continue
            # Recheck immediately before cleanup; a changed source or copy no
            # longer supplies the proof that this tree is safely reconstructible.
            if _tree_inventory(link) != before or any(
                _tree_inventory(source) != inventory for source, inventory in matches
            ):
                report.skipped[str(link)] = (
                    "source or session copy changed during verification; left untouched"
                )
                continue
            shutil.rmtree(link)
            report.removed_session_copies.append(str(link))


def migrate_legacy_layout() -> Optional[MigrationReport]:
    """Run the migration once per process start when the store is enabled."""
    if not capabilities_enabled():
        return None
    from core.agent_skills.config import get_sandbox_skills_dir, get_user_skills_root

    report = MigrationReport()
    shared = get_sandbox_skills_dir()
    workspace = shared.parent
    user_root = get_user_skills_root()

    _sweep_view_dir(report, shared)
    if user_root.is_dir():
        shared_hop = user_root / "skills_shared"
        if junction.is_directory_link(shared_hop):
            junction.remove_directory_link(shared_hop)
            report.removed_links.append(str(shared_hop))
        for user_dir in user_root.iterdir():
            if user_dir.is_dir() and not junction.is_directory_link(user_dir):
                _sweep_view_dir(report, user_dir)
    legacy_cloud = shared.parent / f"{shared.name}_cloud"
    if legacy_cloud.is_dir():
        _quarantine(report, legacy_cloud, "skills_cloud")
    _sweep_sessions(report, workspace)
    _import_flat_skills(report)

    if report.changed or report.skipped:
        log_path = migrations_root() / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        log_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "[caps-migration] imported=%d quarantined=%d links=%d session_copies=%d skipped=%d log=%s",
            len(report.imported),
            len(report.quarantined),
            len(report.removed_links),
            len(report.removed_session_copies),
            len(report.skipped),
            log_path,
        )
    return report
