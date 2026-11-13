"""Server-side read positions for chats (sidebar unread reminders).

Rule: a chat result is unread while its message's ``chat_seq`` is above the user's
``chat_read_states.read_seq``. Marking read is an upsert that only moves forward.

A chat without a row has read nothing (``read_seq`` 0). Chats that existed when this
table was introduced were seeded at their latest message (``seed_read_states``: run
by the ``chatread01`` / ``ce_0044`` migrations and by the SQLite schema bootstrap), so
the upgrade does not suddenly mark every old chat unread; chats created afterwards
start unread until the user reads their result on any client.
"""
from __future__ import annotations

from sqlalchemy import exists, func, literal, select
from sqlalchemy.orm import Session

from core.db.models import ChatMessage, ChatReadState, ChatRun, ChatSession
from core.infra.exceptions import ResourceNotFoundError
from core.infra.time import utc_now

TABLE = ChatReadState.__table__


def read_seqs(db: Session, user_id: str, chat_ids) -> dict[str, int]:
    """Read positions for the given chats (missing chats read nothing: 0)."""
    ids = [chat_id for chat_id in dict.fromkeys(chat_ids) if chat_id]
    if not ids:
        return {}
    rows = db.execute(
        select(ChatReadState.chat_id, ChatReadState.read_seq).where(
            ChatReadState.user_id == user_id, ChatReadState.chat_id.in_(ids)
        )
    ).all()
    found = {chat_id: int(seq or 0) for chat_id, seq in rows}
    return {chat_id: found.get(chat_id, 0) for chat_id in ids}


def _insert(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def advance(db: Session, user_id: str, chat_id: str, seq: int, message_id: str | None = None) -> dict:
    """Move the read position forward to ``seq``; an older position is ignored.

    One statement, so two clients racing (or an old request arriving late) settle on the
    larger position whatever order they commit in. The caller commits.
    """
    seq = max(0, int(seq))
    insert = _insert(db)
    statement = insert(TABLE).values(
        user_id=user_id, chat_id=chat_id, read_seq=seq, read_message_id=message_id, read_at=utc_now()
    )
    excluded = statement.excluded
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[TABLE.c.user_id, TABLE.c.chat_id],
            set_={
                "read_seq": excluded.read_seq,
                "read_message_id": excluded.read_message_id,
                "read_at": excluded.read_at,
            },
            where=TABLE.c.read_seq < excluded.read_seq,
        )
    )
    row = db.execute(
        select(ChatReadState.read_seq, ChatReadState.read_message_id, ChatReadState.read_at).where(
            ChatReadState.user_id == user_id, ChatReadState.chat_id == chat_id
        )
    ).one()
    return {
        "chat_id": chat_id,
        "read_seq": int(row.read_seq),
        "read_message_id": row.read_message_id,
        "read_at": row.read_at.isoformat() if row.read_at else None,
    }


def message_seq(db: Session, chat_id: str, message_id: str) -> int | None:
    """The chat sequence of a message, or of a run's reserved reply that is not written yet."""
    seq = db.scalar(
        select(ChatMessage.chat_seq).where(
            ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id
        )
    )
    if seq is None:
        seq = db.scalar(
            select(ChatRun.assistant_chat_seq).where(
                ChatRun.chat_id == chat_id, ChatRun.message_id == message_id
            )
        )
    return int(seq) if seq is not None else None


def mark_chat_read(db: Session, user_id: str, chat_id: str, message_id: str) -> dict:
    """Mark ``chat_id`` read up to ``message_id`` for a user who can read the chat."""
    from core.chat.service import ChatService

    if ChatService(db).get_session_with_access(chat_id, user_id) is None:
        raise ResourceNotFoundError(resource_type="chat_session", resource_id=chat_id)
    seq = message_seq(db, chat_id, message_id)
    if seq is None:
        raise ResourceNotFoundError(resource_type="chat_message", resource_id=message_id)
    result = advance(db, user_id, chat_id, seq, message_id)
    db.commit()
    return result


def seed_read_states(connection) -> int:
    """Treat every existing chat as read up to its latest message (owner only, idempotent).

    Only chats without a row are seeded, so re-running it never moves a position.
    """
    sessions = ChatSession.__table__
    messages = ChatMessage.__table__
    latest = (
        select(func.coalesce(func.max(messages.c.chat_seq), 0))
        .where(messages.c.chat_id == sessions.c.chat_id)
        .scalar_subquery()
    )
    missing = ~exists().where(
        TABLE.c.user_id == sessions.c.user_id, TABLE.c.chat_id == sessions.c.chat_id
    )
    now = utc_now()
    source = select(
        sessions.c.user_id, sessions.c.chat_id, latest, literal(now, TABLE.c.read_at.type)
    ).where(sessions.c.deleted_at.is_(None), missing)
    result = connection.execute(
        TABLE.insert().from_select(["user_id", "chat_id", "read_seq", "read_at"], source)
    )
    return int(result.rowcount or 0)
