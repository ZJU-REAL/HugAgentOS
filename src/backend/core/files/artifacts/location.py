"""Logical UI locations for already-authorized artifacts; never expose storage keys."""

from pathlib import PurePosixPath
from fastapi import HTTPException
from core.files.edition import artifact_location_scope, artifact_location_root


def artifact_location(db, artifact):
    if artifact is None:
        return {"kind": "unfiled", "root_name": None, "relative_path": None}
    if artifact.deleted_at is not None:
        raise HTTPException(404, "文件已移动或删除")
    kind, folder_id, folder_model, owner_field, owner_id = artifact_location_scope(artifact)
    parts, visited = [], set()
    while folder_id:
        if folder_id in visited or len(visited) >= 128 or folder_model is None:
            raise HTTPException(409, "文件位置暂时无法解析")
        visited.add(folder_id)
        folder = db.get(folder_model, folder_id)
        if (
            folder is None
            or folder.deleted_at is not None
            or getattr(folder, owner_field) != owner_id
        ):
            raise HTTPException(404, "文件位置不可用")
        parts.append(folder.name)
        folder_id = folder.parent_folder_id
    root_name = artifact_location_root(db, artifact)
    # A filename is a display label, never interpreted as an absolute path.
    name = str(artifact.filename or artifact.title).replace(chr(92), "/").rsplit("/", 1)[-1]
    return {
        "kind": kind,
        "root_name": root_name,
        "relative_path": str(PurePosixPath(*reversed(parts), name)),
    }
