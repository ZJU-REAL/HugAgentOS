"""Materialize the registry's paths. Failed or missing files are retried on every barrier."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.llm.tools import myspace_vfs as _ms
from core.myspace import mirror as mm

logger = logging.getLogger(__name__)


@dataclass
class PullReport:
    materialized: int = 0
    removed: int = 0
    failed: int = 0
    conflicted: int = 0


def write_projection(user_id: str, rel: str, data: bytes, timestamp: Optional[float]) -> None:
    """Atomic replacement, with temporary files outside the watched MySpace tree."""
    from core.sandbox._common import myspace_cache_dir

    root = myspace_cache_dir(user_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / rel
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("MySpace projection path escapes its root")
    staging = root.parent.parent / "myspace_staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        if timestamp is not None:
            os.utime(temporary, (timestamp, timestamp))
        from core.space_sync.files import parent_fd

        with parent_fd(root, rel, create=True) as (parent, leaf):
            os.replace(temporary, leaf, dst_dir_fd=parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pull_myspace_updates(*, user_id: str) -> PullReport:
    """Check the complete path set; download only absent or older mirror files.

    An updated_at cursor cannot detect a missing mirror, a renamed ancestor or a
    failed download. Metadata is cheap to enumerate and is the sole path source.
    Live identities take precedence over tombstones left by earlier identities.
    """
    from core.db.engine import SessionLocal
    from core.db.models import Artifact, UserFolder
    from core.services.artifact_edition import personal_artifact_predicates
    from core.storage import get_storage

    report = PullReport()
    if not user_id:
        return report
    db = SessionLocal()
    try:
        rows = (
            db.query(Artifact)
            .filter(Artifact.user_id == user_id, *personal_artifact_predicates(Artifact))
            .all()
        )
        import hashlib

        from core.space_sync.content import signature
        from core.space_sync.files import delete_source, read_source
        from core.space_sync.index import forget, remember, snapshot

        tracked = snapshot(user_id)
        folders: dict[Any, Optional[str]] = {}
        live, deleted = {}, {}
        for art in rows:
            directory = _folder_rel(db, user_id, art.user_folder_id, folders)
            if directory is None or not art.filename:
                continue
            rel = f"{directory}/{art.filename}" if directory else str(art.filename)
            target = deleted if art.deleted_at is not None else live
            if target is live and rel in live:
                # Never silently choose one identity and report an incomplete view.
                raise RuntimeError("MySpace contains duplicate paths; run the duplicate cleanup")
            target[rel] = art
        from core.sandbox._common import myspace_cache_dir

        root = myspace_cache_dir(user_id)
        root.mkdir(parents=True, exist_ok=True)
        folder_rows = db.query(UserFolder).filter_by(user_id=user_id, deleted_at=None).all()
        from core.space_sync.personal_projection import project_directories

        blocked = project_directories(
            user_id, root, folder_rows, lambda fid: _folder_rel(db, user_id, fid, folders), tracked
        )
        tracked = snapshot(user_id)
        for folder in folder_rows:
            directory = _folder_rel(db, user_id, folder.folder_id, folders)
            if directory and not any(
                directory == p or directory.startswith(p + "/") for p in blocked
            ):
                target = root / directory
                if directory in tracked and not target.exists():
                    continue  # Do not recreate an unsaved local directory deletion/move.
                if not target.resolve().is_relative_to(root.resolve()):
                    report.failed += 1
                    continue
                from core.space_sync.files import parent_fd

                with parent_fd(root, directory + "/.directory", create=True):
                    pass
                remember(
                    user_id,
                    directory,
                    {"id": folder.folder_id, "directory": True, "signature": signature(target)},
                )
        storage = get_storage()
        for rel, art in live.items():
            if any(rel.startswith(p + "/") for p in blocked):
                report.conflicted += 1
                continue
            try:
                from core.db.personal_file_names import validate_filename

                validate_filename(art.filename)
                path = _ms.myspace_cache_file(user_id, rel)
                timestamp = mm._artifact_ts(art)
                baseline = tracked.get(rel)
                if (
                    baseline
                    and not path.exists()
                    and baseline.get("id") == getattr(art, "artifact_id", None)
                    and baseline.get("key") == art.storage_key
                ):
                    continue  # A tracked absence is pending local work, not a cache miss.
                if baseline and path.is_file():
                    clean = baseline.get("signature") == signature(path)
                    if not clean:
                        clean = hashlib.sha256(read_source(root, rel)).hexdigest() == baseline.get(
                            "sha256"
                        )
                    if not clean:
                        from core.space_sync.content import adopt_cloud_bytes

                        entry = mm.mirror_entry(user_id, rel)
                        if (
                            entry
                            and (
                                baseline.get("key") != art.storage_key
                                or baseline.get("version") != timestamp
                            )
                            and adopt_cloud_bytes(art, entry)
                        ):
                            continue
                        report.conflicted += int(
                            baseline.get("key") != art.storage_key
                            or baseline.get("version", timestamp) != timestamp
                        )
                        continue
                    if (
                        baseline.get("key") == art.storage_key
                        and baseline.get("version", timestamp) == timestamp
                    ):
                        continue
                if not baseline and path.is_file():
                    from core.space_sync.content import current_snapshot

                    entry = mm.mirror_entry(user_id, rel)
                    if entry and current_snapshot(art, entry):
                        continue
                    report.conflicted += 1
                    continue
                before = signature(path) if path.exists() else None
                data = storage.download_bytes(str(art.storage_key))
                after = signature(path) if path.exists() else None
                if after != before:
                    report.conflicted += 1
                    continue
                write_projection(user_id, rel, data, timestamp)
                if getattr(art, "artifact_id", None):
                    remember(
                        user_id,
                        rel,
                        {
                            "id": art.artifact_id,
                            "key": art.storage_key,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "signature": signature(path),
                            "directory": False,
                            "version": timestamp,
                        },
                    )
                report.materialized += 1
            except Exception as exc:
                logger.error(
                    "MySpace projection failed user=%s artifact=%s stage=materialize error=%s",
                    user_id,
                    art.artifact_id,
                    type(exc).__name__,
                )
                report.failed += 1
        live_ids = {getattr(a, "artifact_id", None) for a in live.values()}
        live_folders = {
            f.folder_id
            for f in db.query(UserFolder).filter_by(user_id=user_id, deleted_at=None).all()
        }
        for rel, baseline in tracked.items():
            path = root / rel
            if baseline.get("directory"):
                if baseline.get("id") not in live_folders and path.is_dir():
                    try:
                        path.rmdir()  # Nonempty/dirty directories are preserved.
                        forget(user_id, rel)
                    except OSError:
                        pass
            elif baseline.get("id") in live_ids and rel not in live and path.is_file():
                if baseline.get("signature") == signature(path) or hashlib.sha256(
                    read_source(root, rel)
                ).hexdigest() == baseline.get("sha256"):
                    delete_source(root, rel)
                    forget(user_id, rel)
        for rel, art in deleted.items():
            if rel in live:
                continue
            path = _ms.myspace_cache_file(user_id, rel)
            try:
                if not path.is_file():
                    continue
                baseline = tracked.get(rel)
                if baseline:
                    clean = baseline.get("signature") == signature(path)
                    if not clean:
                        clean = hashlib.sha256(read_source(root, rel)).hexdigest() == baseline.get(
                            "sha256"
                        )
                    if not clean:
                        report.conflicted += 1
                        continue
                elif path.stat().st_mtime > mm._ts(art.deleted_at) + 0.000001:
                    continue
                delete_source(root, rel)
                forget(user_id, rel)
                report.removed += 1
            except OSError as exc:
                logger.error(
                    "MySpace projection failed user=%s artifact=%s stage=delete error=%s",
                    user_id,
                    art.artifact_id,
                    type(exc).__name__,
                )
                report.failed += 1
    finally:
        db.close()
    return report


def _folder_rel(
    db: Any, user_id: str, folder_id: Any, memo: dict[Any, Optional[str]]
) -> Optional[str]:
    """把 folder_id 还原成相对用户根目录的路径；链断了返回 None。

    **包含已删除的目录**：用户删掉整个文件夹后，镜像里的副本还在那条路径下，要把路径算
    出来才能清掉残留。这里只做路径换算，不承担鉴权。
    """
    if folder_id is None:
        return ""
    if folder_id in memo:
        return memo[folder_id]
    from core.db.models import UserFolder

    names: list[str] = []
    cur: Any = folder_id
    seen: set[str] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = (
            db.query(UserFolder)
            .filter(UserFolder.folder_id == cur, UserFolder.user_id == user_id)
            .first()
        )
        if row is None:
            memo[folder_id] = None
            return None
        names.append(row.name)
        cur = row.parent_folder_id
    rel = "/".join(reversed(names))
    memo[folder_id] = rel
    return rel
