"""The one visibility rule for a project that a request names."""

from fastapi import HTTPException
from sqlalchemy.orm import Session


def require_visible_project(db: Session, user_id: str, project_id: str):
    """Return the live project the user may see; otherwise 404 so existence never leaks."""
    from core.auth.edition import resolve_project_permission
    from core.db.models import Project

    project = (
        db.query(Project)
        .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if project is None or resolve_project_permission(db, user_id, project) == "none":
        raise HTTPException(status_code=404, detail="项目不存在或你无权访问")
    return project
