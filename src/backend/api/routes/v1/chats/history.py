"""History projections, context usage, tool results and follow-up endpoints."""

from typing import Optional

import core.auth.backend as auth_backend
import core.db.engine as db_engine
import core.services as chat_services
from core.auth.backend import UserContext
from core.chat import inflight
from core.chat.display_bounds import bound_result_for_display
from core.db.history_projection import tool_calls_for_history as _tool_calls_for_history
from core.infra.exceptions import ResourceNotFoundError
from core.infra.responses import paginated_response, success_response
from core.services.compaction_service import get_compaction_context_state
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

router = APIRouter()


def _message_to_dict(m, live_run_ids: frozenset = frozenset()) -> dict:
    """Convert a ChatMessage ORM object to API response dict."""
    tool_calls = (
        m.tool_calls
        if getattr(m, "display_is_bounded", False)
        else _tool_calls_for_history(m.tool_calls)
    )
    owner = inflight.marker(m.extra_data)
    return {
        "message_id": m.message_id,
        "chat_id": m.chat_id,
        "chat_seq": m.chat_seq,
        "role": m.role,
        "content": m.content,
        "model": m.model,
        # 思考与正文分列存储；带 offset 的块由前端按原位插回正文（老消息为 None，
        # 思考仍内联在 content 里，走旧解析）。
        "thinking": m.thinking,
        "tool_calls": tool_calls,
        "metadata": m.extra_data or {},
        "error": m.error,
        # ``{run_id, phase, event_offset}`` of the live run still writing this row, else null.
        "in_flight": owner if owner and owner["run_id"] in live_run_ids else None,
        "created_at": m.created_at.isoformat(),
    }


def _is_internal_message(m) -> bool:
    """这条消息是写给模型的内部指令，不该出现在用户看到的聊天记录里。

    后台作业的唤醒轮（进度播报 / 终态交付）是以 user 角色落库的系统指令——模型必须看见它
    （历史另走 ``load_session_history``，不经过本接口），但用户看到的应该只是助手那句
    转述，而不是「请只用一两句话把上面的进度转述给用户」这种提示词本身。
    """
    extra = getattr(m, "extra_data", None) or {}
    if isinstance(extra, dict) and extra.get("hidden_in_chat"):
        return True
    content = getattr(m, "content", None)
    return (
        getattr(m, "role", "") == "user"
        and isinstance(content, str)
        and content.startswith(_WAKE_MESSAGE_PREFIXES)
    )


_WAKE_MESSAGE_PREFIXES = (
    "[系统] 进度播报：",
    "[系统] 你先前提交的批量作业",
)


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
    chat_service = chat_services.ChatService(db)
    user_id = str(user.user_id)

    pair = chat_service.get_session_with_access(chat_id, user_id)
    if pair is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    from core.db.history_page import history_page

    if cursor is True:
        messages, has_more, next_before = history_page(
            db,
            chat_id,
            limit=page_size,
            before_seq=before_seq if isinstance(before_seq, int) else None,
        )
        total = 0
    else:
        messages, total = chat_service.message_repo.list_by_chat(
            chat_id,
            page,
            page_size,
            newest_first=(order.lower() == "desc"),
            display_only=True,
        )
    from core.services.compaction_service import _live_run_ids

    live = _live_run_ids(chat_service, messages)
    items = [_message_to_dict(m, live) for m in messages if not _is_internal_message(m)]
    response = paginated_response(
        items=items,
        page=page,
        page_size=page_size,
        total_items=total,
        message="Messages retrieved successfully",
    )
    if cursor is True:
        response["data"]["pagination"] = {"page_size": page_size, "has_next": has_more}
        response["data"]["next_before_seq"] = next_before
    # Keep the complete transcript visible, while exposing the active
    # checkpoint baseline separately so context-usage estimates do not keep
    # counting messages that the model now sees only through the summary.
    response["data"]["context_compaction"] = get_compaction_context_state(chat_service, chat_id)
    from core.llm.context_usage import latest_persisted_context_usage

    response["data"]["context_usage"] = latest_persisted_context_usage(chat_service, chat_id)
    return response


@router.get("/{chat_id}/context-usage", summary="获取会话上下文占用快照")
def get_context_usage(
    chat_id: str,
    user: UserContext = Depends(auth_backend.get_current_user),
    db: Session = Depends(db_engine.get_db),
):
    """Return the latest provider measurement and active compaction baseline."""
    chat_service = chat_services.ChatService(db)
    if chat_service.get_session_with_access(chat_id, str(user.user_id)) is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    from core.llm.context_usage import latest_persisted_context_usage

    return success_response(
        data={
            "context_usage": latest_persisted_context_usage(chat_service, chat_id),
            "context_compaction": get_compaction_context_state(chat_service, chat_id),
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
    chat_service = chat_services.ChatService(db)
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
    chat_service = chat_services.ChatService(db)

    session = chat_service.get_session(chat_id, user.user_id)
    if not session:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)

    msg = chat_service.message_repo.get_by_id(message_id)
    if not msg or msg.chat_id != chat_id:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)

    questions = (msg.extra_data or {}).get("follow_up_questions", [])
    return success_response(data={"follow_up_questions": questions})
