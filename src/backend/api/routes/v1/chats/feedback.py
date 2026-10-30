"""Assistant feedback endpoints."""

import core.chat.operations.schemas as chat_models
import core.chat.operations.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.chat.service as chat_services_ChatService_module
from core.auth.backend import UserContext
from core.db.models import MessageFeedback
from core.infra.exceptions import ResourceNotFoundError
from core.infra.logging import get_logger
from core.infra.responses import success_response
from core.infra.time import utc_now
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

router = APIRouter()

logger = get_logger(__name__)


@router.post("/messages/{message_id}/feedback", summary="消息反馈")
def submit_feedback(
    message_id: str,
    body: chat_models.FeedbackRequest,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """对某条助手消息提交点赞/点踩反馈（可附评论）。

    rating 仅接受 'like' / 'dislike'；同一用户对同一消息重复提交会覆盖原记录。
    """
    if body.rating not in ("like", "dislike"):
        raise HTTPException(status_code=400, detail="rating must be 'like' or 'dislike'")

    db_user_id = chat_session_context.authenticated_user_id(user)

    existing = (
        db.query(MessageFeedback)
        .filter(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == db_user_id,
        )
        .first()
    )
    if existing:
        existing.rating = body.rating
        existing.comment = body.comment
        existing.updated_at = utc_now()
        db.commit()
        db.refresh(existing)
        record = existing
    else:
        record = MessageFeedback(
            message_id=message_id,
            chat_id=body.chat_id or "",
            user_id=db_user_id,
            rating=body.rating,
            comment=body.comment,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

    return {"ok": True, "feedback_id": record.feedback_id, "rating": record.rating}
