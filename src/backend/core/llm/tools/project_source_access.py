"""Live project authorization for tools; team bytes never use a personal mirror."""

from __future__ import annotations

from fastapi import HTTPException


def validate_scope(db, scope, actor, *, write=False):
    from core.services.project_source import ProjectSourceService

    project = ProjectSourceService(db).authorized_project(scope.project_id, actor, write=write)
    from core.services.project_file_service import ProjectFileService

    if not ProjectFileService(db).matches_scope(project, scope):
        raise HTTPException(409, "项目空间已变更，请重新打开项目会话后再操作")
    return project


def current_scope_error(scope, actor, *, write=False):
    if not scope:
        return None
    if scope.is_local:
        from core.config.local_mode import local_mode_enabled
        if not local_mode_enabled():
            return {"error": "本地项目只能在本机模式下操作", "status": 403}
        from core.services.local_project_workspace import validate_current_project
        try:
            validate_current_project(scope, actor, write=write)
        except (HTTPException, ValueError, OSError) as exc:
            return {"error": str(getattr(exc, "detail", exc)),
                    "status": getattr(exc, "status_code", 409)}
        return None
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            validate_scope(db, scope, actor or "", write=write)
    except HTTPException as exc:
        return {"error": exc.detail, "status": 403 if exc.status_code == 404 else exc.status_code}
    return None
