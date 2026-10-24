"""Manual context compaction for one chat (the CLI's ``/compact``)."""

from typing import Any, Dict

import core.auth.backend as auth_backend
import core.chat.context as chat_context
import core.chat.operations.session_context as chat_session_context
import core.runs.runtime as runtime
from core.auth.backend import UserContext
from core.chat.compaction.manual import compact_chat_now, manual_budget_inputs
from core.chat.operations.admission_errors import chat_busy_http_exception
from core.chat.sequencer import ActiveRunRef, ChatBusyError
from core.chat.service import ChatService
from core.db.threads import run_in_session
from core.infra.exceptions import BadRequestError, ResourceNotFoundError, ServiceUnavailableError
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

router = APIRouter()


def _prepare(db: Session, chat_id: str, auth_user_id: str) -> Dict[str, Any]:
    """Owner check + live-run check; returns the coordinator's budget inputs."""
    user_id = chat_context.resolve_db_user_id(db, auth_user_id)
    chat_service = ChatService(db)
    if chat_service.get_session(chat_id, user_id) is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    # Same query as GET /{chat_id}/active-run: while a run writes the history,
    # its own mid-/post-turn compaction owns the checkpoint.
    run = runtime.get_active_run_for_chat(chat_id, user_id)
    if run is not None:
        raise chat_busy_http_exception(
            ChatBusyError(ActiveRunRef(run.run_id, run.message_id or "", run.status))
        )
    return manual_budget_inputs(chat_service, chat_id)


@router.post("/{chat_id}/compact", summary="立即压缩会话上下文")
async def compact_chat(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
):
    """把这段会话较早的历史压缩成摘要，下一轮从压缩后的上下文继续。

    与自动压缩走同一条流水线（快照水位 + 租约 + CAS 发布检查点）。历史全部落在
    保留原文的尾部时没有可压缩的内容，返回 ``compacted=false``；会话有进行中的
    run 时返回 409。摘要要调用模型，可能耗时数十秒。
    """
    budget_inputs = await run_in_session(
        _prepare, chat_id, chat_session_context.authenticated_user_id(user)
    )
    result = await compact_chat_now(chat_id, budget_inputs)
    if result.outcome == "disabled":
        raise BadRequestError("上下文压缩已被管理员关闭，无法手动压缩")
    if result.outcome == "busy":
        raise HTTPException(
            status_code=409,
            detail={"code": "compaction_busy", "message": "这段会话正在压缩，请稍后再试"},
        )
    if result.outcome == "failed":
        raise ServiceUnavailableError("上下文压缩失败：摘要模型未返回结果或检查点写入失败，请稍后重试")
    return success_response(
        data={
            "compacted": result.outcome == "compacted",
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
        }
    )
