"""Ontology acceptance and assistant feedback endpoints."""

import api.routes.v1.chats.models as chat_models
import api.routes.v1.chats.session_context as chat_session_context
import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.db.engine as db_engine
import core.services as chat_services
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


@router.post(
    "/messages/{message_id}/ontology-revision/accept",
    summary="采用领域本体优化稿",
)
def accept_ontology_revision(
    message_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Atomically replace one assistant message with its reviewed candidate answer."""
    chat_service = chat_services.ChatService(db)
    target = chat_service.get_message_by_id(message_id)
    if not target or target.role != "assistant":
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)
    db_user_id = chat_context.resolve_db_user_id(
        db, chat_session_context._authenticated_user_id(user)
    )
    access = chat_service.get_session_with_access(target.chat_id, db_user_id)
    if access is None:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)
    if access[1] not in ("admin", "edit"):
        raise HTTPException(status_code=403, detail="只读共享会话不可替换消息正文")
    updated = chat_service.accept_ontology_revision(message_id)
    if updated is None:
        raise HTTPException(status_code=409, detail="当前消息没有可采用的本体优化稿")
    return success_response(
        data={"message_id": message_id, "content": updated.content},
        message="已采用本体优化稿",
    )


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

    db_user_id = chat_session_context._authenticated_user_id(user)

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

    if body.rating == "dislike" and (body.comment or "").strip() and record.chat_id:
        try:
            from core.services.ontology_evolution_service import OntologyEvolutionService

            OntologyEvolutionService(db).ingest_user_correction(
                user_id=db_user_id,
                chat_id=record.chat_id,
                message_id=message_id,
                feedback_id=str(record.feedback_id),
                comment=(body.comment or "").strip(),
            )
        except Exception:  # noqa: BLE001 - feedback persistence must remain available
            logger.warning("ontology user-correction ingestion failed", exc_info=True)

    return {"ok": True, "feedback_id": record.feedback_id, "rating": record.rating}
