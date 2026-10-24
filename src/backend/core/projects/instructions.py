"""Project-root AGENTS.md storage shared by CE and EE.

Callers must authorize reads. Writes independently require project edit access.
File content is authoritative; the database field is only a legacy fallback.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session, attributes

from core.db.models import Project
from core.files.storage import get_storage

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

    def read(self, project: Project, *, remember_file: bool = True) -> dict:
        """Read fresh bytes, including external edits; never silently truncate."""
        try:
            with self.db.no_autoflush:
                return self._read(project, remember_file=remember_file)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "读取 AGENTS.md 状态失败，请稍后重试") from exc

    def _read(self, project: Project, *, remember_file: bool) -> dict:
        data = None
        try:
            if project.kind == "local":
                target = self._local_file(project)
                if target.exists():
                    data = _read_regular_file(target)
                    if data is None:
                        raise HTTPException(409, "AGENTS.md 必须是普通文件")
            else:
                from core.files.spaces.projects import source_key
                storage = get_storage()
                key = source_key(self.db, project, AGENTS_FILENAME)
                info = storage.stat_object(key)
                if info is not None:
                    if info.size > MAX_INSTRUCTIONS_BYTES:
                        raise HTTPException(413, "AGENTS.md 不能超过 32 KiB")
                    data = storage.download_bytes(key)
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
        from core.files.spaces.leases import space_lock
        lock = _local_write_lock(self._local_file(project)) if project.kind == "local" else space_lock(user_id, engine=self.db.get_bind())
        with lock:
            self._write_locked(project, user_id, text, expected_revision=expected_revision)

    def _write_locked(self, project: Project, user_id: str, text: str, *, expected_revision: str | None):
        from core.auth.edition import resolve_project_permission

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
        if resolve_project_permission(self.db, user_id, project) not in ("admin", "edit"):
            raise HTTPException(403, "需要项目编辑权限才能更新 AGENTS.md")
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
                self._write_file(project, data)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "保存 AGENTS.md 失败，项目指令未保存") from exc
        project.instructions = None
        project.extra_data = {**(project.extra_data or {}), "agents_file_managed": True}

    def _write_file(self, project: Project, data: bytes) -> None:
        from core.projects.files import ProjectFileService
        from core.files.spaces.projects import source_key
        storage = get_storage()
        key = source_key(self.db, project, AGENTS_FILENAME)
        current = storage.stat_object(key)
        files = ProjectFileService(self.db)
        if files.capacity_used(project) - (current.size if current else 0) + len(data) > files.capacity_limit():
            raise HTTPException(413, "项目容量不足，无法保存 AGENTS.md")
        from core.files.spaces.tree import ensure_parents
        ensure_parents(storage, key)
        if current and current.is_directory:
            raise HTTPException(409, "AGENTS.md 不能是目录")
        storage.upload_bytes(data, key)


def read_authorized_project_instructions(project_id: str, user_id: str) -> dict:
    """Read canonical rules in a fresh transaction with current view permission."""
    from core.auth.edition import resolve_project_permission
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        project = db.query(Project).filter(
            Project.project_id == project_id, Project.deleted_at.is_(None)
        ).first()
        if project is None or resolve_project_permission(db, user_id, project) not in ("admin", "edit", "view"):
            raise HTTPException(403, "项目不存在或无权读取项目指令")
        return ProjectInstructionsService(db).read(project)


def _read_regular_file(target: Path) -> bytes | None:
    """Up to the size limit plus one byte of a regular file; ``None`` for anything else.

    O_NOFOLLOW refuses a symlink swapped in after the path check.
    """
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        return stream.read(MAX_INSTRUCTIONS_BYTES + 1)


def _read_inherited_file(target: Path) -> str | None:
    """Best-effort read of one outer AGENTS.md; anything invalid is skipped."""
    try:
        data = _read_regular_file(target)
        text = _decode(data) if data is not None else ""
    except (OSError, HTTPException):
        return None
    return text if text.strip() else None


def _ancestor_directories(root: Path) -> list[Path]:
    """Directories strictly above ``root`` down from the nearest git root, else ``$HOME``."""
    home = Path.home().resolve()
    for directory in (root, *root.parents):
        if (directory / ".git").exists():
            bound = directory
            break
        if directory == home:
            bound = home  # never search above $HOME
            break
    else:
        # Outside $HOME without a git root: only the project folder itself.
        bound = root
    chain = [directory for directory in root.parents if directory == bound or bound in directory.parents]
    return list(reversed(chain))


def inherited_local_instructions(local_path: str) -> list[tuple[str, str]]:
    """Outer AGENTS.md files for a local project, outermost first.

    Global ``$HUGAGENT_HOME/AGENTS.md``, then each directory from the nearest git
    root (bounded by ``$HOME``) down to, but excluding, the project folder, whose
    own file stays owned by ``ProjectInstructionsService``. Read-only prompt input.
    """
    from core.config.runtime_env import local_data_dir

    raw = (local_path or "").strip()
    if not raw or not os.path.isabs(raw):
        return []
    try:
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            return []
        candidates = [local_data_dir().expanduser() / AGENTS_FILENAME]
        candidates += [directory / AGENTS_FILENAME for directory in _ancestor_directories(root)]
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    seen = {str(root / AGENTS_FILENAME)}
    for target in candidates:
        try:
            key = str(target.parent.resolve() / target.name)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        text = _read_inherited_file(target)
        if text is not None:
            out.append((str(target), text))
    return out
