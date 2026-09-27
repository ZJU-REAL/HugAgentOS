"""Conversation branching without starting or replaying an execution."""

from uuid import UUID

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import created_response
from core.services.chat_fork_service import ChatForkError, ChatForkService
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/chats", tags=["Sessions"])


class ForkChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    through_message_id: str | None = Field(None, min_length=1, max_length=64)
    title: str | None = Field(None, min_length=1, max_length=500)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("title must not be blank")
        return value


@router.post("/{chat_id}/fork", status_code=201, summary="创建聊天分支")
def fork_chat(
    chat_id: str,
    request: ForkChatRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        session = ChatForkService(db).fork(
            chat_id,
            str(user.user_id),
            request.request_id,
            request.through_message_id,
            request.title,
        )
    except ChatForkError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
    return created_response(
        data={
            "chat_id": session.chat_id,
            "title": session.title,
            "user_id": session.user_id,
            "message_count": session.message_count,
            "project_id": session.project_id,
            "pinned": session.pinned,
            "favorite": session.favorite,
            "metadata": {
                k: v for k, v in (session.extra_data or {}).items() if k != "_fork_request"
            },
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
        }
    )
