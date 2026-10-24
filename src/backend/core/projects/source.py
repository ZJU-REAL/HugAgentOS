"""Authorized reads and writes of mounted project files."""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import PurePosixPath

from core.auth.edition import resolve_project_permission
from core.db.models import Project
from core.projects.files import ProjectFileService
from core.files.storage import get_storage
from fastapi import HTTPException
from sqlalchemy.orm import Session


def reserve_sqlite_writer(db: Session) -> None:
    """SQLite has no row locks; reserve its writer before reading a write baseline."""
    if db.get_bind().dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


class ProjectSourceService:
    def __init__(self, db: Session):
        self.db = db

    def authorized_project(self, project_id: str, actor: str, *, write: bool):
        if write:
            reserve_sqlite_writer(self.db)
        query = (
            self.db.query(Project)
            .filter(
                Project.project_id == project_id,
                Project.deleted_at.is_(None),
            )
            .populate_existing()
        )
        if write:
            query = query.with_for_update()
        project = query.first()
        level = resolve_project_permission(self.db, actor, project) if project else "none"
        if level == "none":
            raise HTTPException(404, "项目不存在或无权访问")
        if write and level not in ("edit", "admin"):
            raise HTTPException(403, "当前项目只读")
        return project

    def snapshot(
        self, project_id: str, actor: str, *, lock: bool = False,
        relative_root: str = "", exclude_patterns: frozenset[str] = frozenset(),
    ) -> list[tuple[str, bytes]]:
        from core.files.spaces.projects import project_entries
        from core.files.storage.entries import validate_key
        project = self.authorized_project(project_id, actor, write=False)
        try:
            prefix = validate_key(relative_root, directory=True)
        except ValueError as exc:
            raise HTTPException(400, "源码目录必须是项目内相对路径") from exc
        storage = get_storage()
        result, size = [], 0
        for entry in project_entries(self.db, project, storage):
            if not entry["name"].startswith(prefix):
                continue
            name = entry["name"][len(prefix):]
            if any(fnmatch.fnmatchcase(part, pattern)
                   for part in PurePosixPath(name).parts for pattern in exclude_patterns):
                continue
            size += entry["size_bytes"]
            if size > 30 * 1024 * 1024:
                raise HTTPException(413, "项目源码超过大小限制")
            result.append((name, storage.download_bytes(entry["storage_key"])))
        return result

    def read_bytes(self, project_id: str, actor: str, path: str) -> tuple[bytes, str]:
        from core.files.spaces.projects import source_key
        project = self.authorized_project(project_id, actor, write=False)
        storage = get_storage()
        key = source_key(self.db, project, path)
        info = storage.stat_object(key)
        if info is None or info.is_directory:
            raise HTTPException(404, "源码文件不存在")
        if info.size > 50 * 1024 * 1024:
            raise HTTPException(413, "源码文件超过大小限制")
        return storage.download_bytes(key), path

    def read(self, project_id: str, actor: str, path: str) -> dict:
        data, artifact_id = self.read_bytes(project_id, actor, path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(415, "该文件不是 UTF-8 文本") from exc
        return {"path": path, "content": text,
                "revision": hashlib.sha256(data).hexdigest(), "artifact_id": artifact_id}

    def write_files(
        self, project_id: str, actor: str, files: list[tuple[str, bytes]],
        *, revisions: dict[str, str] | None = None,
    ) -> list[dict]:
        from core.files.spaces.leases import space_lock
        from core.files.spaces.projects import source_key, project_entries
        with space_lock(actor, engine=self.db.get_bind()):
            self.db.expire_all()
            project = self.authorized_project(project_id, actor, write=True)
            storage = get_storage()
            entries = project_entries(self.db, project, storage)
            total = sum(entry["size_bytes"] for entry in entries)
            known = {entry["storage_key"]: entry["size_bytes"] for entry in entries}
            prepared, seen = [], set()
            for path, data in files:
                key = source_key(self.db, project, path)
                from core.files.spaces.tree import ensure_parents
                ensure_parents(storage, key)
                existing = storage.stat_object(key)
                if (existing and existing.is_directory) or storage.list_directory(key + "/", limit=1).entries:
                    raise HTTPException(409, "源码路径已有文件夹")
                if key in seen:
                    raise HTTPException(400, "源码包包含重复路径")
                seen.add(key)
                if len(data) > 50 * 1024 * 1024:
                    raise HTTPException(413, "源码文件超过大小限制")
                if path == "AGENTS.md":
                    from core.projects.instructions import _decode
                    _decode(data)
                    project.instructions = None
                    project.extra_data = {**(project.extra_data or {}), "agents_file_managed": True}
                if revisions is not None:
                    old = storage.download_bytes(key) if key in known else None
                    actual = hashlib.sha256(old).hexdigest() if old is not None else ""
                    if revisions.get(path) != actual:
                        raise HTTPException(409, "源码已修改，请重新读取后再保存")
                total += len(data) - known.get(key, 0)
                if total > ProjectFileService.capacity_limit():
                    raise HTTPException(413, "项目空间容量不足")
                prepared.append((path, key, data))
            result = []
            for path, key, data in prepared:
                url = storage.upload_bytes(data, key)
                result.append({"path": path, "artifact_id": "",
                    "revision": hashlib.sha256(data).hexdigest(),
                    "storage_key": key, "download_url": url})
            self.db.commit()
            return result
