"""Desktop source-project endpoints, separate from cloud site management."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from core.auth.backend import UserContext, get_current_user
from core.infra.responses import success_response
from core.services import local_site_sources as sources

router = APIRouter(prefix="/v1/local/site-sources", tags=["local"])


class PrepareBody(BaseModel):
    title: str = Field("站点", max_length=120)
    project_id: str = Field("", max_length=64)


@router.post("/prepare")
def prepare(body: PrepareBody, user: UserContext = Depends(get_current_user)):
    return success_response(
        data=sources.prepare_project(str(user.user_id), body.title, body.project_id)
    )


@router.get("")
def list_sources(user: UserContext = Depends(get_current_user)):
    return success_response(data={"items": sources.list_sources(str(user.user_id))})


@router.post("/{site_id}/edit")
def edit(site_id: str, user: UserContext = Depends(get_current_user)):
    return success_response(data=sources.open_editor(str(user.user_id), site_id))
