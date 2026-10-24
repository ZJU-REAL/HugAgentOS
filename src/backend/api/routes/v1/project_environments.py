"""Project environment configuration and authorized resource options."""

from core.auth.backend import UserContext, get_current_user
from core.auth.edition import ProjectAccess, require_project_access
from core.db.engine import get_db
from core.infra.responses import success_response
from core.projects.environment.config import ProjectEnvironment
from core.projects.environment.service import ProjectEnvironmentService
from core.projects.environment_resources import environment_resources
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

router = APIRouter(tags=["projects"])


@router.get("/v1/project-environments/options")
def environment_options(
    user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    graph = environment_resources(db, str(user.user_id))
    return success_response(
        data={
            field: [item for item in items.values() if item.get("selectable", True)]
            for field, items in graph.items()
        }
    )


@router.post("/v1/project-environments/preview")
def preview_environment(
    body: ProjectEnvironment,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(
        data=ProjectEnvironmentService(db).preview(str(user.user_id), body.model_dump())
    )


@router.get("/v1/projects/{project_id}/environment")
def get_environment(
    access: ProjectAccess = Depends(require_project_access("view")), db: Session = Depends(get_db)
):
    return success_response(
        data=ProjectEnvironmentService(db).describe(access.project, access.user_id)
    )


@router.put("/v1/projects/{project_id}/environment")
def update_environment(
    body: ProjectEnvironment,
    access: ProjectAccess = Depends(require_project_access("admin")),
    db: Session = Depends(get_db),
):
    service = ProjectEnvironmentService(db)
    service.save(access.project, access.user_id, body.model_dump())
    return success_response(data=service.describe(access.project, access.user_id))
