"""Task evaluation reads: the reminder list and one chat's existing evaluations."""

import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.chat.operations.session_context as chat_session_context
import core.chat.service as chat_service_module
import core.db.engine as db_engine
from core.auth.backend import UserContext
from core.auth.capabilities import require_capability
from core.chat import evaluation
from core.infra.exceptions import ResourceNotFoundError
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/evaluations/pending", summary="待评价对话（温馨提醒）")
def list_pending_evaluations(
    limit: int = Query(evaluation.REMINDER_LIMIT, ge=1, le=20),
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """最近还没评价的对话：只含提醒期（交付后 3 个工作日）内的回答。

    超过提醒期的回答不再提醒：简单任务视为默认采纳，复杂任务进入事后抽检。需要「会话评价」权限。
    """
    db_user_id = chat_context.resolve_db_user_id(db, chat_session_context.authenticated_user_id(user))
    require_capability(db, db_user_id, "can_use_evaluation", "未开通会话评价")
    return success_response(data=evaluation.pending_evaluations(db, db_user_id, limit=limit))


@router.get("/{chat_id}/feedback", summary="会话内的回答评价")
def list_chat_feedback(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """当前用户对这个会话各条回答的评价，按 message_id 索引；``excluded`` 是定时任务 / 循环任务的回答（不参与评价）。"""
    db_user_id = chat_context.resolve_db_user_id(db, chat_session_context.authenticated_user_id(user))
    if chat_service_module.ChatService(db).get_session_with_access(chat_id, db_user_id) is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    return success_response(data={
        "items": evaluation.chat_feedback(db, chat_id, db_user_id),
        "excluded": evaluation.excluded_messages(db, chat_id),
    })
