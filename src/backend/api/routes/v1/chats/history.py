"""History projections, context usage, tool results and follow-up endpoints."""

from typing import Optional

import core.auth.backend as auth_backend
import core.db.engine as db_engine
import core.chat.service as chat_services_ChatService_module
from core.auth.backend import UserContext
from core.infra.text_bounds import bound_result_for_display
from core.infra.exceptions import ResourceNotFoundError
from core.infra.responses import success_response
from core.chat.compaction.usage_projection import get_chat_context_state
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/{chat_id}/messages", summary="获取会话消息列表")
def list_messages(
    chat_id: str,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    order: str = Query(
        "asc",
        description="asc=第 1 页是最早的一批（默认，兼容老前端）；desc=第 1 页是最近的一批",
    ),
    cursor: bool = Query(False, description="Use stable sequence pagination without a total count"),
    before_seq: Optional[int] = Query(None, ge=1),
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """获取当前用户有权读取的会话消息列表；无权访问时返回 404。"""
    from core.chat.operations.history import list_messages as operation

    return operation(
        chat_id=chat_id,
        page=page,
        page_size=page_size,
        order=order,
        cursor=cursor,
        before_seq=before_seq,
        user=user,
        db=db,
    )


@router.get("/{chat_id}/context-usage", summary="获取会话上下文占用快照")
def get_context_usage(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Return the latest provider measurement and active compaction baseline."""
    chat_service = chat_services_ChatService_module.ChatService(db)
    if chat_service.get_session_with_access(chat_id, str(user.user_id)) is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    from core.chat.compaction.progress import read_progress

    return success_response(
        data={
            **get_chat_context_state(chat_service, chat_id),
            "compaction_progress": read_progress(chat_service, chat_id),
        }
    )


@router.get(
    "/{chat_id}/messages/{message_id}/tool-calls/{tool_id}",
    summary="按需获取单个工具调用的完整结果",
)
def get_tool_call_result(
    chat_id: str,
    message_id: str,
    tool_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """返回某条消息里某个工具调用的完整结果。

    历史列表接口只下发梗概（见 ``_tool_calls_for_history``）——一页 100 条消息、每条
    好几张工具卡，把完整结果一起搬进浏览器正是"打开长对话就卡死"的来源。用户真正
    展开哪张卡，前端才来这里取哪一张。仍然过一遍 ``bound_result_for_display`` 的宽档
    上限：单张卡也不该把 5MB 一次性塞进标签页。
    """
    chat_service = chat_services_ChatService_module.ChatService(db)
    if chat_service.get_session_with_access(chat_id, str(user.user_id)) is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    msg = chat_service.message_repo.get_by_id(message_id)
    if msg is None or msg.chat_id != chat_id:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)

    for item in msg.tool_calls or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("tool_id") or "") != tool_id:
            continue
        result, truncated = bound_result_for_display(item.get("result"))
        return success_response(
            data={
                "tool_id": tool_id,
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "result": result,
                "truncated": truncated,
            }
        )

    raise ResourceNotFoundError(resource_type="tool_call", resource_id=tool_id)


@router.get("/{chat_id}/messages/{message_id}/followups", summary="获取追问问题")
def get_followups(
    chat_id: str,
    message_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Return follow-up questions stored in a message's extra_data."""
    chat_service = chat_services_ChatService_module.ChatService(db)

    session = chat_service.get_session(chat_id, user.user_id)
    if not session:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    msg = chat_service.message_repo.get_by_id(message_id)
    if not msg or msg.chat_id != chat_id:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)

    questions = (msg.extra_data or {}).get("follow_up_questions", [])
    return success_response(data={"follow_up_questions": questions})
