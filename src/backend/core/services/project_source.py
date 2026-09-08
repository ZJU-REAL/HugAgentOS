"""Authorized project source reads and revision-aware writes.

New bytes use immutable storage keys. Folder/artifact changes commit together,
so a failed upload cannot replace the last good source or delete its identity.
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid

from core.auth.permissions_iface import resolve_project_permission
from core.db.models import Artifact, Project
from core.services.project_file_service import ProjectFileService
from core.storage import get_storage
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
        self, project_id: str, actor: str, *, lock: bool = False
    ) -> list[tuple[str, bytes]]:
        project = self.authorized_project(project_id, actor, write=False)
        entries = ProjectFileService(self.db).list_files(project)
        if len(entries) > 400:
            raise HTTPException(413, "项目源码文件过多，无法一次载入")
        result, size = [], 0
        seen = set()
        for entry in entries:
            if entry["name"] in seen:
                raise HTTPException(409, "项目中存在同名源码文件，请先消除冲突")
            seen.add(entry["name"])
            # Validate legacy upload paths before placing bytes in a working directory.
            ProjectFileService(self.db).source_file_scope(project, actor, entry["name"])
            artifact = (
                self.db.query(Artifact)
                .filter_by(artifact_id=entry["artifact_id"])
                .populate_existing()
                .with_for_update()
                .one()
                if lock
                else self.db.get(Artifact, entry["artifact_id"])
            )
            size += artifact.size_bytes
            if size > 30 * 1024 * 1024:
                raise HTTPException(413, "项目源码超过工作副本大小限制")
            result.append((entry["name"], get_storage().download_bytes(artifact.storage_key)))
        return result

    def read_bytes(self, project_id: str, actor: str, path: str) -> tuple[bytes, str]:
        project = self.authorized_project(project_id, actor, write=False)
        filters, _ = ProjectFileService(self.db).source_file_scope(project, actor, path)
        rows = self.db.query(Artifact).filter_by(**filters, deleted_at=None).limit(2).all()
        if not rows:
            raise HTTPException(404, "源码文件不存在")
        if len(rows) != 1:
            raise HTTPException(409, "项目中存在同名文件，请先消除冲突")
        data = get_storage().download_bytes(rows[0].storage_key)
        return data, rows[0].artifact_id

    def read(self, project_id: str, actor: str, path: str) -> dict:
        data, artifact_id = self.read_bytes(project_id, actor, path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(415, "该文件不是 UTF-8 文本") from exc
        return {
            "path": path,
            "content": text,
            "revision": hashlib.sha256(data).hexdigest(),
            "artifact_id": artifact_id,
        }

    def write_files(
        self,
        project_id: str,
        actor: str,
        files: list[tuple[str, bytes]],
        *,
        revisions: dict[str, str] | None = None,
    ) -> list[dict]:
        project = self.authorized_project(project_id, actor, write=True)
        pfs = ProjectFileService(self.db)
        storage = get_storage()
        staged, result = [], []
        commit_started = False
        seen = set()
        try:
            total = pfs.capacity_used(project)
            for path, data in files:
                if path in seen:
                    raise HTTPException(400, "源码包包含重复路径")
                seen.add(path)
                if path == "AGENTS.md":
                    from core.services.project_instructions import _decode

                    _decode(data)
                    project.instructions = None
                    project.extra_data = {**(project.extra_data or {}), "agents_file_managed": True}
                if not data or len(data) > 50 * 1024 * 1024:
                    raise HTTPException(413, "源码文件为空或超过大小限制")
                filters, values = pfs.source_file_scope(project, actor, path, create=True)
                rows = (
                    self.db.query(Artifact)
                    .filter_by(**filters, deleted_at=None)
                    .populate_existing()
                    .with_for_update()
                    .limit(2)
                    .all()
                )
                if len(rows) > 1:
                    raise HTTPException(409, "项目中存在同名文件，请先消除冲突")
                artifact = rows[0] if rows else None
                if revisions is not None:
                    old = storage.download_bytes(artifact.storage_key) if artifact else None
                    actual = hashlib.sha256(old).hexdigest() if old is not None else ""
                    if revisions.get(path) != actual:
                        raise HTTPException(409, "源码已被其他成员修改，请重新读取后再保存")
                total += len(data) - (artifact.size_bytes if artifact else 0)
                if total > pfs.capacity_limit():
                    raise HTTPException(413, "项目空间容量不足")
                if artifact is None:
                    artifact = Artifact(
                        artifact_id="pj_" + uuid.uuid4().hex[:16],
                        filename=path.split("/")[-1],
                        title=path.split("/")[-1],
                        type="code",
                        size_bytes=len(data),
                        mime_type=mimetypes.guess_type(path)[0] or "text/plain",
                        storage_key="",
                        **values,
                    )
                    self.db.add(artifact)
                revision = hashlib.sha256(data).hexdigest()
                key = f"project_sources/{project_id}/{artifact.artifact_id}/{uuid.uuid4().hex}"
                url = storage.upload_bytes(data, key)
                staged.append(key)
                artifact.storage_key, artifact.storage_url = key, url
                artifact.size_bytes = len(data)
                artifact.parsed_text = None
                artifact.summary = None
                self.db.flush()
                result.append(
                    {
                        "path": path,
                        "artifact_id": artifact.artifact_id,
                        "revision": revision,
                        "storage_key": key,
                        "download_url": f"/files/{artifact.artifact_id}",
                    }
                )
            commit_started = True
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            # A commit transport failure may have committed on the server.
            # Leave these immutable objects for storage GC until the outcome is known.
            for key in [] if commit_started else staged:
                try:
                    storage.delete(key)
                except Exception:
                    pass
            raise
