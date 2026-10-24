"""Shared history operations; callers supply authenticated identity."""

import core.chat.service as chat_services_ChatService_module

from core.chat import inflight

from core.db.history_projection import tool_calls_for_history as _tool_calls_for_history

from core.infra.exceptions import ResourceNotFoundError

from core.infra.responses import paginated_response

from core.chat.compaction.usage_projection import get_chat_context_state

_WAKE_MESSAGE_PREFIXES = (
    "[系统] 进度播报：",
    "[系统] 你先前提交的批量作业",
)


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


def list_messages(
    chat_id, page=1, page_size=50, order="asc", cursor=False, before_seq=None, user=None, db=None
):
    """获取当前用户有权读取的会话消息列表；无权访问时返回 404。"""
    chat_service = chat_services_ChatService_module.ChatService(db)
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
    from core.chat.compaction.service import _live_run_ids

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
    response["data"].update(get_chat_context_state(chat_service, chat_id))
    from core.chat.compaction.progress import read_progress

    response["data"]["compaction_progress"] = read_progress(chat_service, chat_id)
    return response
