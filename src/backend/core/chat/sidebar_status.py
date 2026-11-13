"""Lightweight, user-owned latest run verdicts for navigation reminders.

The query uses journal verdicts, never transport EOF or absence from a live list.
Only ordinary chat runs belong here; module/background work has its own surface.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.db.models import ChatMessage, ChatReadState, ChatRun, ChatSession
from core.db.reserved_chats import ordinary_chat_filter

BACKGROUND_KINDS = ("automation_plan", "automation_prompt", "internal_job_agent", "legacy_chat_stream")


def sidebar_run_statuses(db: Session, user_id: str) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    kind = func.coalesce(ChatRun.request_payload["kind"].as_string(), "chat")
    ranked = select(
        ChatRun.chat_id, ChatRun.run_id, ChatRun.message_id, ChatRun.status,
        ChatRun.created_at, ChatRun.updated_at, ChatRun.assistant_chat_seq,
        func.row_number().over(partition_by=ChatRun.chat_id,
            order_by=(ChatRun.created_at.desc(), ChatRun.run_id.desc())).label("position"),
    ).join(ChatSession, ChatSession.chat_id == ChatRun.chat_id).where(
        ChatRun.user_id == user_id, ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None),
        ordinary_chat_filter(ChatRun.chat_id), kind.not_in(BACKGROUND_KINDS),
        or_(ChatRun.updated_at >= since, ChatRun.status.in_(("pending", "running", "needs_attention"))),
    ).subquery()
    # The run's reply sequence decides read state. Runs written before the reply sequence
    # was reserved on the run fall back to the reply message's own sequence.
    reply_seq = func.coalesce(ranked.c.assistant_chat_seq, ChatMessage.chat_seq)
    rows = db.execute(
        select(ranked, reply_seq.label("message_seq"),
               func.coalesce(ChatReadState.read_seq, 0).label("read_seq"))
        .outerjoin(ChatMessage, (ChatMessage.chat_id == ranked.c.chat_id)
                   & (ChatMessage.message_id == ranked.c.message_id))
        .outerjoin(ChatReadState, (ChatReadState.chat_id == ranked.c.chat_id)
                   & (ChatReadState.user_id == user_id))
        .where(ranked.c.position == 1)
    ).mappings()
    return [{"chat_id": row["chat_id"], "run_id": row["run_id"],
             "message_id": row["message_id"], "status": row["status"],
             "created_at": row["created_at"].isoformat(),
             "updated_at": row["updated_at"].isoformat(),
             "message_seq": row["message_seq"], "read_seq": int(row["read_seq"]),
             # Server read state: the user read this run's result on some client.
             "read": row["message_seq"] is not None and row["message_seq"] <= row["read_seq"]}
            for row in rows]
