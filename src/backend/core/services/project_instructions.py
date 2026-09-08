"""Project-root AGENTS.md storage shared by CE and EE.

Callers must authorize reads. Writes independently require project edit access.
File content is authoritative; the database field is only a legacy fallback.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session, attributes

from core.db.models import Artifact, Project
from core.storage import get_storage

AGENTS_FILENAME = "AGENTS.md"
MAX_INSTRUCTIONS_BYTES = 32 * 1024


def _decode(data: bytes) -> str:
    if len(data) > MAX_INSTRUCTIONS_BYTES:
        raise HTTPException(413, "AGENTS.md 不能超过 32 KiB，请精简项目根指令")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "AGENTS.md 必须使用 UTF-8 编码") from exc
    if "\x00" in text:
        raise HTTPException(422, "AGENTS.md 不能包含空字符")
    return text


@contextmanager
def _local_write_lock(target: Path):
    """Serialize desktop writers across threads/processes, including SQLite."""
    directory = Path(tempfile.gettempdir()) / "hugagent-instruction-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256(str(target).encode()).hexdigest()
    # Keep the lock inode stable: unlinking it would allow separate concurrent locks.
    with (directory / key).open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class ProjectInstructionsService:
    def __init__(self, db: Session):
        self.db = db

    def _local_file(self, project: Project) -> Path:
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            raise HTTPException(403, "本地项目指令仅在本机模式下可用")
        raw = ((project.extra_data or {}).get("local") or {}).get("path", "")
        if not raw or not os.path.isabs(raw):
            raise HTTPException(409, "本地项目文件夹不存在或不可访问")
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(409, "本地项目文件夹不存在或不可访问")
        target = root / AGENTS_FILENAME
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise HTTPException(409, "AGENTS.md 必须是项目根目录内的普通文件，不能是符号链接")
        return target

    def _artifact(self, project: Project):
        query = self.db.query(Artifact).filter(
            Artifact.filename == AGENTS_FILENAME, Artifact.deleted_at.is_(None)
        )
        from core.services.project_file_service import ProjectFileService

        filters, _ = ProjectFileService(self.db).instruction_file_scope(project, project.owner_user_id)
        query = query.filter_by(**filters)
        matches = query.limit(2).all()
        if len(matches) > 1:
            raise HTTPException(409, "项目根目录有多个 AGENTS.md，请保留一个后重试")
        return matches[0] if matches else None

    def read(self, project: Project, *, remember_file: bool = True) -> dict:
        """Read fresh bytes, including external edits; never silently truncate."""
        try:
            with self.db.no_autoflush:
                return self._read(project, remember_file=remember_file)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "同步 AGENTS.md 状态失败，请稍后重试") from exc

    def _read(self, project: Project, *, remember_file: bool) -> dict:
        data = None
        try:
            if project.kind == "local":
                target = self._local_file(project)
                if target.exists():
                    # O_NOFOLLOW prevents a symlink swapped in after the path check.
                    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise HTTPException(409, "AGENTS.md 必须是普通文件")
                        data = stream.read(MAX_INSTRUCTIONS_BYTES + 1)
            else:
                artifact = self._artifact(project)
                if artifact is not None:
                    if artifact.size_bytes > MAX_INSTRUCTIONS_BYTES:
                        raise HTTPException(413, "AGENTS.md 不能超过 32 KiB")
                    data = get_storage().download_bytes(artifact.storage_key)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "读取 AGENTS.md 失败，请检查文件或存储后重试") from exc

        if data is not None:
            text, source = _decode(data), "AGENTS.md"
            if remember_file and not (project.extra_data or {}).get("agents_file_managed"):
                self._remember_file(project)
        elif (project.extra_data or {}).get("agents_file_managed"):
            text, source = "", "missing"
        else:
            text, source = project.instructions or "", "legacy"
        revision = hashlib.sha256(source.encode() + b"\0" + text.encode("utf-8")).hexdigest()
        return {
            "instructions": text,
            "instructions_source": source,
            "instructions_revision": revision,
        }

    def _remember_file(self, project: Project) -> None:
        # A read must not commit unrelated pending ORM changes. Use a fresh short
        # transaction and merge metadata loaded under lock, not the reader's snapshot.
        with Session(bind=self.db.get_bind()) as marker_db:
            if marker_db.bind.dialect.name == "sqlite":
                marker_db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            latest = marker_db.query(Project).filter(
                Project.project_id == project.project_id
            ).with_for_update().first()
            if latest is None or latest.deleted_at is not None:
                raise HTTPException(404, "项目不存在")
            latest.instructions = None
            latest.extra_data = {**(latest.extra_data or {}), "agents_file_managed": True}
            metadata = dict(latest.extra_data)
            marker_db.commit()
        # Keep this reader coherent without marking the synchronized values dirty.
        state = attributes.instance_state(project)
        if not state.attrs.extra_data.history.has_changes():
            attributes.set_committed_value(project, "extra_data", metadata)
        if not state.attrs.instructions.history.has_changes():
            attributes.set_committed_value(project, "instructions", None)

    def write(
        self, project: Project, user_id: str, text: str, *, expected_revision: str | None = None
    ) -> None:
        """Write the root file; participate in the caller's database transaction."""
        lock = _local_write_lock(self._local_file(project)) if project.kind == "local" else nullcontext()
        with lock:
            self._write_locked(project, user_id, text, expected_revision=expected_revision)

    def _write_locked(self, project: Project, user_id: str, text: str, *, expected_revision: str | None):
        from core.auth.permissions_iface import resolve_project_permission

        if resolve_project_permission(self.db, user_id, project) not in ("admin", "edit"):
            raise HTTPException(403, "需要项目编辑权限才能更新 AGENTS.md")
        data = text.encode("utf-8")
        _decode(data)
        # SQLite has no row locks; acquire its write transaction before refreshing.
        if self.db.get_bind().dialect.name == "sqlite":
            connection = self.db.connection()
            if not connection.connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        # Preserve validated changes from a combined project PATCH. Production
        # sessions disable autoflush; refreshing must not discard their pending fields.
        self.db.flush([project])
        # Serialize service writes to this project (PostgreSQL); read again under lock.
        self.db.query(Project).filter(Project.project_id == project.project_id).with_for_update().populate_existing().first()
        current = self.read(project, remember_file=False)
        if expected_revision is not None and expected_revision != current["instructions_revision"]:
            raise HTTPException(409, "AGENTS.md 已发生变化，请重新打开编辑器并合并修改")
        try:
            if project.kind == "local":
                target = self._local_file(project)
                fd, temporary = tempfile.mkstemp(prefix=".agents-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if target.exists():
                        os.chmod(temporary, target.stat().st_mode & 0o777)
                    # Recheck before replacement so a normal external edit is not lost.
                    if self.read(project, remember_file=False)["instructions_revision"] != current["instructions_revision"]:
                        raise HTTPException(409, "AGENTS.md 已发生变化，请重新打开编辑器并合并修改")
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            else:
                self._write_artifact(project, user_id, data)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "保存 AGENTS.md 失败，项目指令未保存") from exc
        project.instructions = None
        project.extra_data = {**(project.extra_data or {}), "agents_file_managed": True}

    def _write_artifact(self, project: Project, user_id: str, data: bytes) -> None:
        from core.services.project_file_service import ProjectFileService

        artifact = self._artifact(project)
        # Artifact schema requires a positive size. A newline represents cleared instructions.
        content = data or b"\n"
        files = ProjectFileService(self.db)
        if files.capacity_used(project) - (artifact.size_bytes if artifact else 0) + len(content) > files.capacity_limit():
            raise HTTPException(413, "项目容量不足，无法保存 AGENTS.md")
        artifact_id = artifact.artifact_id if artifact else f"pj_{uuid.uuid4().hex[:16]}"
        key = f"project-instructions/{project.project_id}/{uuid.uuid4().hex}/AGENTS.md"
        url = get_storage().upload_bytes(content, key)
        if artifact is None:
            _, fields = files.instruction_file_scope(project, user_id)
            fields.update(
                artifact_id=artifact_id,
                type="document", title=AGENTS_FILENAME, filename=AGENTS_FILENAME,
            )
            artifact = Artifact(**fields)
            self.db.add(artifact)
        artifact.storage_key = key
        artifact.storage_url = url
        artifact.size_bytes = len(content)
        artifact.mime_type = "text/markdown"
        artifact.parsed_text = None
        artifact.summary = None
        artifact.parsed_at = None
        artifact.parse_error = None
        artifact.updated_at = datetime.utcnow()


def read_authorized_project_instructions(project_id: str, user_id: str) -> dict:
    """Read canonical rules in a fresh transaction with current view permission."""
    from core.auth.permissions_iface import resolve_project_permission
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        project = db.query(Project).filter(
            Project.project_id == project_id, Project.deleted_at.is_(None)
        ).first()
        if project is None or resolve_project_permission(db, user_id, project) not in ("admin", "edit", "view"):
            raise HTTPException(403, "项目不存在或无权读取项目指令")
        return ProjectInstructionsService(db).read(project)
