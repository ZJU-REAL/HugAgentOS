"""UI history reads omit replay payloads and page by stable chat sequence."""

from sqlalchemy import func
from core.db.models import ChatMessage


def display_query(db, chat_id):
    fields = [
        getattr(ChatMessage, key)
        for key in (
            "message_id",
            "chat_id",
            "chat_seq",
            "role",
            "content",
            "model",
            "thinking",
            "extra_data",
            "error",
            "created_at",
        )
    ]
    return db.query(
        *fields,
        ChatMessage.tool_calls_display.is_not(None).label("display_is_bounded"),
        func.coalesce(
            ChatMessage.tool_calls_display,
            ChatMessage.tool_calls,
        ).label("tool_calls"),
    ).filter(ChatMessage.chat_id == chat_id, ChatMessage.role != "system")


def history_page(db, chat_id, *, limit=30, before_seq=None):
    query = display_query(db, chat_id)
    if before_seq is not None:
        query = query.filter(ChatMessage.chat_seq < before_seq)
    rows = query.order_by(ChatMessage.chat_seq.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more, rows[0].chat_seq if rows else None
