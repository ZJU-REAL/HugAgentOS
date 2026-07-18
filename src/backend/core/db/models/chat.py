"""SQLAlchemy ORM models — chat sessions/messages."""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, Text, TIMESTAMP,
    ForeignKey, CheckConstraint, UniqueConstraint, Index, Numeric, JSON
)
from sqlalchemy.dialects.postgresql import JSONB, INET
from sqlalchemy.orm import relationship, mapped_column
from core.db.engine import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")
# BigInteger autoincrement PK: SQLite only auto-increments the INTEGER rowid, not
# BIGINT, so an unqualified BigInteger PK stays NULL on the no-Docker SQLite
# profile. Fall back to Integer on SQLite (still BIGINT on Postgres).
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class ChatSession(Base):
    """Chat session table."""
    __tablename__ = "chat_sessions"

    chat_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=False, default="新对话")
    message_count = Column(Integer, default=0)
    pinned = Column(Boolean, default=False)
    favorite = Column(Boolean, default=False)
    archived = Column(Boolean, default=False)
    deleted_at = Column(TIMESTAMP(timezone=True))
    extra_data = Column("metadata", JSONType, default={})
    # Project mode: the chat is mounted on a specific project (NULL = ordinary chat)
    project_id = Column(String(64), ForeignKey("projects.project_id", ondelete="SET NULL"), nullable=True)
    # Sharing scope of a chat within a team project (only effective when
    # project.kind='team'; personal-project / non-project chats are always treated
    # as private):
    #   'private'    — visible only to the owner (default)
    #   'team_read'  — project members can read, but cannot send messages / rename / delete
    #   'team_edit'  — project members can keep sending messages and renaming; delete
    #                  is still owner / project admin only
    share_scope = Column(
        String(16), nullable=False, default="private", server_default="private"
    )
    # Inbound channel-bot origin (NULL = ordinary web chat). Channel messages upsert
    # by (channel_id, external_conversation_id) to reuse the same session, running
    # under the owner's identity.
    #   p2p   → external_conversation_id = speaker's open_id (one session per DM peer)
    #   group → external_conversation_id = group chat_id (the whole group shares one session)
    # See internal design docs.
    channel_id = Column(String(64), nullable=True)
    external_conversation_id = Column(String(128), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    last_message_at = Column(TIMESTAMP(timezone=True))

    # Relationships
    user = relationship("UserShadow", back_populates="chat_sessions")
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="session")

    __table_args__ = (
        CheckConstraint("length(title) >= 1", name="chat_sessions_title_length"),
        CheckConstraint("message_count >= 0", name="chat_sessions_message_count_check"),
        CheckConstraint(
            "share_scope IN ('private','team_read','team_edit')",
            name="ck_chat_sessions_share_scope",
        ),
        Index("idx_chat_sessions_user_id", "user_id"),
        Index("idx_chat_sessions_updated_at", "updated_at"),
        Index("idx_chat_sessions_user_updated", "user_id", "updated_at"),
        Index("idx_chat_sessions_pinned", "user_id", "pinned", "updated_at", postgresql_where=Column("pinned") == True),
        Index("idx_chat_sessions_favorite", "user_id", "favorite", "updated_at", postgresql_where=Column("favorite") == True),
        Index("idx_chat_sessions_deleted", "deleted_at", postgresql_where=Column("deleted_at").isnot(None)),
        Index("idx_chat_sessions_metadata_gin", "metadata", postgresql_using="gin"),
        Index("idx_chat_sessions_last_message_at", "last_message_at"),
        # Inbound channel message → locate/reuse the session: (channel_id, external_conversation_id)
        Index(
            "idx_chat_sessions_channel_conv",
            "channel_id", "external_conversation_id",
            postgresql_where=Column("channel_id").isnot(None),
        ),
        # Team-project shared chat listing query: composite index on (project_id, share_scope)
        Index(
            "idx_chat_sessions_project_share",
            "project_id", "share_scope",
            postgresql_where=Column("project_id").isnot(None),
        ),
    )


class ChatSessionUserState(Base):
    """Per-user state of a chat × user pair (pin / favorite).

    Purpose: once a chat is shared within a team project, each member's pin /
    favorite are independent of each other.
    - Team-project chats: reads and writes always go through this table (the owner
      does too).
    - Non-project chats / personal-project chats: keep reading/writing the legacy
      ``ChatSession.pinned/favorite`` fields; this table is unused, leaving
      existing behavior untouched.
    """
    __tablename__ = "chat_session_user_states"

    chat_id      = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"), primary_key=True)
    user_id      = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), primary_key=True)
    pinned       = Column(Boolean, nullable=False, default=False, server_default="false")
    favorite     = Column(Boolean, nullable=False, default=False, server_default="false")
    last_seen_at = Column(TIMESTAMP(timezone=True))
    created_at   = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at   = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_csus_user", "user_id"),
        Index(
            "idx_csus_user_pinned",
            "user_id", "pinned",
            postgresql_where=Column("pinned") == True,
        ),
        Index(
            "idx_csus_user_favorite",
            "user_id", "favorite",
            postgresql_where=Column("favorite") == True,
        ),
    )


class ChatMessage(Base):
    """Chat message table."""
    __tablename__ = "chat_messages"

    message_id = Column(String(64), primary_key=True)
    chat_id = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    model = Column(String(100))
    tool_calls = Column(JSONType)
    usage = Column(JSONType)
    error = Column(JSONType)
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    # Relationships
    session = relationship("ChatSession", back_populates="messages")

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="chat_messages_role_check"),
        CheckConstraint("length(content) <= 100000", name="chat_messages_content_length"),
        Index("idx_chat_messages_chat_id", "chat_id"),
        Index("idx_chat_messages_chat_created", "chat_id", "created_at"),
        Index("idx_chat_messages_role", "chat_id", "role"),
        Index("idx_chat_messages_created_at", "created_at"),
        Index("idx_chat_messages_tool_calls_gin", "tool_calls", postgresql_using="gin", postgresql_where=Column("tool_calls").isnot(None)),
    )


class ChatRun(Base):
    """Chat Run — decouples AI tasks from the HTTP connection lifecycle.

    Every message send creates a run; a background asyncio.Task runs the workflow,
    chunks are written to a Redis Stream, and the SSE endpoint pulls from the
    Stream. After a page refresh, playback resumes via follow_run + offset.
    """
    __tablename__ = "chat_runs"

    run_id            = Column(String(64), primary_key=True)
    chat_id           = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"), nullable=False)
    user_id           = Column(String(64), nullable=False)
    message_id        = Column(String(64), nullable=False)  # pre-allocated assistant message id
    status            = Column(String(20), nullable=False, default="pending")
    request_payload   = Column(JSONType)        # serialized ChatRequest (for the worker to rebuild the context)
    last_event_offset = Column(Integer, default=0, nullable=False)
    error_message     = Column(Text)
    usage             = Column(JSONType)
    created_at        = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    started_at        = Column(TIMESTAMP(timezone=True))
    completed_at      = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="chat_runs_status_check",
        ),
        Index("idx_chat_runs_chat_status", "chat_id", "status"),
        Index("idx_chat_runs_user_id", "user_id"),
        Index("idx_chat_runs_created_at", "created_at"),
    )


class MessageFeedback(Base):
    """Message feedback table - stores like/dislike ratings and optional comments."""
    __tablename__ = "message_feedback"

    feedback_id = Column(BigIntPK, primary_key=True, autoincrement=True)
    message_id = Column(String(64), ForeignKey("chat_messages.message_id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="SET NULL"), nullable=True)
    rating = Column(String(10), nullable=False)   # 'like' or 'dislike'
    comment = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("rating IN ('like', 'dislike')", name="message_feedback_rating_check"),
        Index("idx_message_feedback_message_id", "message_id"),
        Index("idx_message_feedback_chat_id", "chat_id"),
        Index("idx_message_feedback_user_id", "user_id"),
    )


class ChatSandboxSnapshot(Base):
    """Per-chat opensandbox snapshot pointer.

    A chat keeps at most 1 snapshot at any time (the latest one). The background
    idle worker upserts on snapshot+kill; the next time that chat reconnects,
    _get_or_create_session prefers this snapshot when starting a new sandbox,
    restoring the filesystem state. The GC worker periodically scans rows with
    expires_at < now and deletes remote + DB together. Design in
    internal design docs.
    """
    __tablename__ = "chat_sandbox_snapshots"

    chat_id     = Column(String(64), primary_key=True)
    snapshot_id = Column(String(64), nullable=False, unique=True)
    sandbox_id  = Column(String(64), nullable=False)              # source sandbox id parked at the time, for debugging / reconciliation
    created_at  = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at  = Column(TIMESTAMP(timezone=True), nullable=False)  # created_at + SNAPSHOT_RETENTION_DAYS
    size_bytes  = Column(BigInteger)                              # for metrics, nullable
    extra       = Column("metadata", JSONType, default=dict)      # reserved: image uri / pool kind / notes

    __table_args__ = (
        Index("idx_chat_sandbox_snapshots_expires", "expires_at"),
        Index("idx_chat_sandbox_snapshots_created", "created_at"),
    )
