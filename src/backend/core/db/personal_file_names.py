"""One live personal file per directory/name, including writers outside HTTP."""
from fastapi import HTTPException
from sqlalchemy import Index, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

INDEX_NAMES = ("uq_personal_file_folder_name", "uq_personal_file_root_name")


def personal_file_indexes(*, enterprise=False):
    scope = ""
    return tuple(
        Index(
            name, *columns, unique=True,
            postgresql_where=text(scope + predicate),
            sqlite_where=text(scope + predicate),
        )
        for name, columns, predicate in (
            (INDEX_NAMES[0], ("user_id", "user_folder_id", "filename"),
             "user_folder_id IS NOT NULL AND deleted_at IS NULL"),
            (INDEX_NAMES[1], ("user_id", "filename"),
             "user_folder_id IS NULL AND deleted_at IS NULL"),
        )
    )


def conflict():
    return HTTPException(409, "目标文件夹已存在同名文件；请修改原文件，不能新建同名文件。")


def validate_filename(filename):
    if (not filename or filename in (".", "..") or
            any(c in filename for c in ("/", "\\", "\x00")) or
            len(filename.encode("utf-8")) > 255):
        raise HTTPException(400, "文件名必须是有效的单个路径名称，UTF-8 长度不能超过 255 字节")


def check_name(db, user_id, folder_id, filename, exclude_id=None, exclude_folder=None):
    from core.db.models import Artifact, UserFolder
    from core.services.artifact_edition import personal_artifact_predicates

    validate_filename(filename)
    with db.no_autoflush:
        query = db.query(Artifact.artifact_id).filter(
            Artifact.user_id == user_id, Artifact.user_folder_id == folder_id,
            Artifact.filename == filename, Artifact.deleted_at.is_(None),
            *personal_artifact_predicates(Artifact),
        )
        if exclude_id:
            query = query.filter(Artifact.artifact_id != exclude_id)
        if query.first():
            raise conflict()
        folders = db.query(UserFolder.folder_id).filter(
            UserFolder.user_id == user_id, UserFolder.parent_folder_id == folder_id,
            UserFolder.name == filename, UserFolder.deleted_at.is_(None),
        )
        if exclude_folder:
            folders = folders.filter(UserFolder.folder_id != exclude_folder)
        if folders.first():
            raise conflict()


@event.listens_for(Session, "before_flush")
def validate_personal_filenames(db, flush_context, instances):
    from core.db.models import Artifact, UserFolder, UserShadow
    from core.services.artifact_edition import is_personal_artifact
    from core.services.folder_batch import lock_folder_owner

    candidates = []
    for row in db.new.union(db.dirty):
        if isinstance(row, UserFolder):
            fields = ("user_id", "parent_folder_id", "name", "deleted_at")
        elif isinstance(row, Artifact) and is_personal_artifact(row):
            fields = ("user_id", "user_folder_id", "filename", "deleted_at")
        else:
            continue
        if row.deleted_at is None and (row in db.new or any(
            inspect(row).attrs[key].history.has_changes() for key in fields
        )):
            candidates.append(row)
    seen = set()
    with db.no_autoflush:
        # The same owner lock is used by folder creation, including batch uploads.
        for owner in sorted({row.user_id for row in candidates}):
            if db.query(UserShadow).filter(UserShadow.user_id == owner).first():
                lock_folder_owner(db, UserShadow, "user_id", owner)
        for row in candidates:
            folder = isinstance(row, UserFolder)
            key = (row.user_id, row.parent_folder_id if folder else row.user_folder_id,
                   row.name if folder else row.filename)
            if key in seen:
                raise conflict()
            seen.add(key)
            check_name(db, *key, exclude_id=None if folder else row.artifact_id,
                       exclude_folder=row.folder_id if folder else None)


@event.listens_for(Engine, "handle_error", retval=True)
def translate_personal_name_conflict(context):
    error = context.original_exception
    constraint = getattr(getattr(error, "diag", None), "constraint_name", None)
    if constraint in INDEX_NAMES or str(error) in (
        "UNIQUE constraint failed: artifacts.user_id, artifacts.user_folder_id, artifacts.filename",
        "UNIQUE constraint failed: artifacts.user_id, artifacts.filename",
    ):
        return conflict()
