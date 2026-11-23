"""Assistant feedback endpoints."""

import core.chat.operations.schemas as chat_models
import core.chat.operations.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.chat.service as chat_services_ChatService_module
from core.chat import evaluation
from core.auth.backend import UserContext
from core.db.models import ChatMessage, MessageFeedback
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
    """对某条助手回答提交评价：采纳 / 需修改 / 不满意（可附原因维度与说明）。

    rating 接受 ``adopt`` / ``revise`` / ``reject``，旧值 ``like`` / ``dislike`` 仍可用（分别视为采纳、
    不满意）。原因维度只在需修改、不满意时保存。同一用户对同一回答重复提交会覆盖原记录。
    """
    verdict = evaluation.normalize_verdict(body.rating)
    if verdict is None:
        raise HTTPException(status_code=400, detail="rating must be one of adopt / revise / reject")
    reasons = [] if verdict == evaluation.ADOPT else evaluation.clean_reasons(body.reasons)
    comment = (body.comment or "").strip()[: evaluation.COMMENT_MAX] or None

    db_user_id = chat_session_context.authenticated_user_id(user)
    message = (
        db.query(ChatMessage.chat_id, ChatMessage.role).filter(ChatMessage.message_id == message_id).first()
    )
    if message is None or message.role != "assistant":
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)
    if not evaluation.message_evaluable(db, message_id):
        raise HTTPException(status_code=400, detail="定时任务和循环任务的回答不参与评价")

    existing = (
        db.query(MessageFeedback)
        .filter(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == db_user_id,
        )
        .first()
    )
    if existing:
        existing.rating = evaluation.legacy_rating(verdict)
        existing.verdict = verdict
        existing.reasons = reasons
        existing.comment = comment
        existing.updated_at = utc_now()
        db.commit()
        db.refresh(existing)
        record = existing
    else:
        record = MessageFeedback(
            message_id=message_id,
            chat_id=message.chat_id,
            user_id=db_user_id,
            rating=evaluation.legacy_rating(verdict),
            verdict=verdict,
            reasons=reasons,
            comment=comment,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

    return {"ok": True, "rating": record.rating, **evaluation.feedback_to_dict(record)}
