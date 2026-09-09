"""Live project authorization for tools; team bytes never use a personal mirror."""

from __future__ import annotations

import hashlib

from core.llm.tools._state import ReadEntry
from fastapi import HTTPException


def validate_scope(db, scope, actor, *, write=False):
    from core.services.project_source import ProjectSourceService

    project = ProjectSourceService(db).authorized_project(scope.project_id, actor, write=write)
    from core.services.project_file_service import ProjectFileService

    if not ProjectFileService(db).matches_scope(project, scope):
        raise HTTPException(409, "项目空间已变更，请重新打开项目会话后再操作")
    return project


def current_scope_error(scope, actor, *, write=False):
    if not scope or scope.is_local:
        return None
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            validate_scope(db, scope, actor or "", write=write)
    except HTTPException as exc:
        return {"error": exc.detail, "status": 403 if exc.status_code == 404 else exc.status_code}
    return None


def relative_path(scope, actor, path):
    from core.llm.tools.myspace_vfs import myspace_rel

    from .project_working_copy import directory

    prefix = directory(scope.project_id) + "/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    rel = myspace_rel(path, actor, scope)
    prefix = scope.folder_name + "/"
    if not rel or not rel.startswith(prefix):
        raise HTTPException(400, "文件路径必须位于当前项目文件夹内")
    return rel[len(prefix) :]


def read_team_bytes(scope, actor, path):
    from core.db.engine import SessionLocal
    from core.services.project_source import ProjectSourceService

    with SessionLocal() as db:
        validate_scope(db, scope, actor)
        return ProjectSourceService(db).read_bytes(
            scope.project_id,
            actor,
            relative_path(scope, actor, path),
        )[0]


def write_team_text(
    scope,
    actor,
    path,
    physical,
    state,
    *,
    content=None,
    old_string=None,
    new_string=None,
    replace_all=False,
):
    from core.db.engine import SessionLocal
    from core.services.project_source import ProjectSourceService

    with SessionLocal() as db:
        validate_scope(db, scope, actor, write=True)
        service = ProjectSourceService(db)
        rel = relative_path(scope, actor, path)
        try:
            before, _ = service.read_bytes(scope.project_id, actor, rel)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            before = None
        entry = state.get(path) or state.get(physical)
        if before is not None:
            if entry is None or entry.offset is not None or entry.parsed_doc:
                raise HTTPException(409, "覆盖文件前必须先完整 Read 当前源码")
            revision = hashlib.sha256(before).hexdigest()
            if entry.sha256 != revision:
                raise HTTPException(409, "源码已被其他成员修改，请重新 Read")
        else:
            revision = ""
        if old_string is not None:
            if before is None:
                raise HTTPException(404, "源码文件不存在")
            text = before.decode("utf-8")
            count = text.count(old_string)
            if not count or (count > 1 and not replace_all):
                raise HTTPException(409, "old_string 未找到或不能唯一匹配，请重新 Read")
            content = text.replace(old_string, new_string, -1 if replace_all else 1)
        data = content.encode("utf-8")
        saved = service.write_files(
            scope.project_id, actor, [(rel, data)], revisions={rel: revision}
        )[0]
        entry = ReadEntry(content=data, sha256=saved["revision"], offset=None, limit=None)
        state.record(path, entry)
        state.record(physical, entry)
        return {
            "ok": True,
            "persistent": True,
            "file_path": path,
            "file_id": saved["artifact_id"],
            "size": len(data),
            "type": "update" if before is not None else "create",
            "note": "源码已保存到团队项目。发布时使用当前项目源码。",
        }


def is_team_source_path(scope, actor, path):
    if not scope or scope.kind != "team":
        return False
    from ._paths import is_myspace_physical, to_physical_path
    from .project_working_copy import directory

    return path.startswith(directory(scope.project_id) + "/") or is_myspace_physical(
        to_physical_path(path, actor), actor
    )
