"""Logical personal-file locations for already-authorized CE artifacts."""

from pathlib import PurePosixPath

from fastapi import HTTPException

from core.db import models


def artifact_location(db, artifact):
    if artifact is None:
        return {"kind": "unfiled", "root_name": None, "relative_path": None}
    if artifact.deleted_at is not None:
        raise HTTPException(404, "文件已移动或删除")
    folder_id = artifact.user_folder_id
    parts, visited = [], set()
    while folder_id:
        if folder_id in visited or len(visited) >= 128:
            raise HTTPException(409, "文件位置暂时无法解析")
        visited.add(folder_id)
        folder = db.get(models.UserFolder, folder_id)
        if folder is None or folder.deleted_at is not None or folder.user_id != artifact.user_id:
            raise HTTPException(404, "文件位置不可用")
        parts.append(folder.name)
        folder_id = folder.parent_folder_id
    name = str(artifact.filename or artifact.title).replace(chr(92), "/").rsplit("/", 1)[-1]
    return {
        "kind": "personal",
        "root_name": None,
        "relative_path": str(PurePosixPath(*reversed(parts), name)),
    }
