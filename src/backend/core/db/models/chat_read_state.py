"""Per-user read position in a chat, owned by the server that owns the chat.

The position is a chat sequence number (``chat_messages.chat_seq``): a result is read
when its message's sequence is at or below ``read_seq``. It only moves forward, so
concurrent clients and late requests can never make a read result unread again.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, Index, String

from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now


class ChatReadState(Base):
    """How far one user has read one chat."""

    __tablename__ = "chat_read_states"

    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        primary_key=True,
    )
    read_seq = Column(BigInteger, nullable=False, default=0, server_default="0")
    # The message the user last acknowledged (informational; read_seq decides).
    read_message_id = Column(String(64), nullable=True)
    read_at = Column(UTCDateTime(timezone=True), default=utc_now)

    __table_args__ = (
        CheckConstraint("read_seq >= 0", name="chat_read_states_seq_nonnegative"),
        Index("idx_chat_read_states_chat", "chat_id"),
    )
