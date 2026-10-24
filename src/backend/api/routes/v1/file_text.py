"""Authenticated UTF-8 editing endpoints mounted in the file router."""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import success_response
from core.files.artifacts.text import ArtifactTextService

router = APIRouter()


class TextSaveRequest(BaseModel):
    content: str = Field(max_length=2 * 1024 * 1024)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("/{file_id}/text")
def read_text(
    file_id: str,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return success_response(ArtifactTextService(db).read(file_id, str(user.user_id)))


@router.put("/{file_id}/text")
def save_text(
    file_id: str,
    body: TextSaveRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(
        ArtifactTextService(db).save(file_id, str(user.user_id), body.content, body.revision)
    )
