"""Resolve a host directory to the caller's local project (terminal client).

Shared by the EE and CE project routes: match an existing ``kind=local`` project
by the realpath of its bound folder, otherwise create one through the regular
``create_local`` path. Callers gate on ``local_mode_enabled()``.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from core.db.models import Project
from core.projects.project_names import next_available_folder_name

_MAX_NAME = 120


def _canonical(path: str) -> str:
    raw = (path or "").strip()
    if not raw or not os.path.isabs(raw):
        raise HTTPException(422, "path 必须是绝对路径")
    real = os.path.realpath(raw)
    if not os.path.isdir(real):
        raise HTTPException(422, "path 必须是已存在的目录")
    return real


def _local_projects(db: Session, user_id: str):
    """The caller's live local projects."""
    return db.query(Project).filter(
        Project.kind == "local",
        Project.owner_user_id == user_id,
        Project.deleted_at.is_(None),
    )


def _find(db: Session, user_id: str, real: str) -> Optional[Project]:
    rows = _local_projects(db, user_id).order_by(desc(Project.last_activity_at)).all()
    for project in rows:
        stored = str(((project.extra_data or {}).get("local") or {}).get("path") or "").strip()
        if stored and os.path.isabs(stored) and os.path.realpath(stored) == real:
            return project
    return None


def _unique_name(db: Session, user_id: str, wanted: str) -> str:
    # Leave room for a " (N)" suffix within the 120-character name limit.
    base = wanted.strip()[: _MAX_NAME - 8].strip() or "project"
    existing = _local_projects(db, user_id).with_entities(Project.name).all()
    return next_available_folder_name(base, existing)


def resolve_local_project(
    db: Session,
    service,
    user_id: str,
    path: str,
    *,
    create: bool = True,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``{project_id, name, local_path, created}`` for ``path``."""
    real = _canonical(path)
    project = _find(db, user_id, real)
    created = False
    if project is None:
        if not create:
            raise HTTPException(404, "该目录尚未绑定本地项目")
        from core.projects.environment.service import ProjectEnvironmentService

        environment_service = ProjectEnvironmentService(db)
        environment = environment_service.validate(user_id, {})
        with environment_service.creation_metadata(user_id, environment):
            project = service.create_local(
                user_id,
                _unique_name(db, user_id, name or os.path.basename(real)),
                real,
            )
        created = True
    local = (project.extra_data or {}).get("local") or {}
    return {
        "project_id": project.project_id,
        "name": project.name,
        "local_path": local.get("path") or real,
        "created": created,
    }
