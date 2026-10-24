"""ORM models for chat sessions, messages and compaction state, plus sequence allocation."""
from __future__ import annotations
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from datetime import datetime, timezone
from core.db.engine import Base
from core.db.edition import ChatSessionEditionFields, chat_session_edition_table_args
from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, Computed, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, case, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import deferred, relationship
from core.db.models.column_types import JSONType

class ChatSession(ChatSessionEditionFields, Base):
    """Chat session table."""

    __tablename__ = "chat_sessions"

    chat_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(500), nullable=False, default="新对话")
    message_count = Column(Integer, default=0)
    # Per-chat durable message allocator. ``message_count`` cannot be reused for
    # this purpose because messages may be deleted and internal checkpoints are
    # intentionally excluded from the visible count.
    next_message_seq = Column(BigInteger, nullable=False, default=1, server_default="1")
    pinned = Column(Boolean, default=False)
    favorite = Column(Boolean, default=False)
    archived = Column(Boolean, default=False)
    deleted_at = Column(UTCDateTime(timezone=True))
    extra_data = Column("metadata", JSONType, default={})
    # Project mode: the chat is mounted on a specific project (NULL = ordinary chat)
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Inbound channel-bot origin (NULL = ordinary web chat). Channel messages upsert
    # by (channel_id, external_conversation_id) to reuse the same session, running
    # under the owner's identity.
    #   p2p   → external_conversation_id = speaker's open_id (one session per DM peer)
    #   group → external_conversation_id = group chat_id (the whole group shares one session)
    # See internal design docs.
    channel_id = Column(String(64), nullable=True)
    external_conversation_id = Column(String(128), nullable=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(
        UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    last_message_at = Column(UTCDateTime(timezone=True))
    # Relationships
    user = relationship("UserShadow", back_populates="chat_sessions")
    messages = relationship(
        "ChatMessage", back_populates="session", cascade="all, delete-orphan"
    )
    artifacts = relationship("Artifact", back_populates="session")

    __table_args__ = (
        *chat_session_edition_table_args(),
        CheckConstraint("length(title) >= 1", name="chat_sessions_title_length"),
        CheckConstraint("message_count >= 0", name="chat_sessions_message_count_check"),
        Index("idx_chat_sessions_user_id", "user_id"),
        Index("idx_chat_sessions_updated_at", "updated_at"),
        Index("idx_chat_sessions_user_updated", "user_id", "updated_at"),
        Index(
            "idx_chat_sessions_pinned",
            "user_id",
            "pinned",
            "updated_at",
            postgresql_where=Column("pinned") == True,
        ),
        Index(
            "idx_chat_sessions_favorite",
            "user_id",
            "favorite",
            "updated_at",
            postgresql_where=Column("favorite") == True,
        ),
        Index(
            "idx_chat_sessions_deleted",
            "deleted_at",
            postgresql_where=Column("deleted_at").isnot(None),
        ),
        Index("idx_chat_sessions_metadata_gin", "metadata", postgresql_using="gin"),
        Index("idx_chat_sessions_last_message_at", "last_message_at"),
        # Inbound channel message → locate/reuse the session: (channel_id, external_conversation_id)
        Index(
            "idx_chat_sessions_channel_conv",
            "channel_id",
            "external_conversation_id",
            postgresql_where=Column("channel_id").isnot(None),
        ),
    )


class ChatMessage(Base):
    """Chat message table."""

    __tablename__ = "chat_messages"

    message_id = Column(String(64), primary_key=True)
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stable replay order. Repository writes allocate eagerly; the before-flush
    # fallback below covers direct ORM writes so create_all and migrated schemas
    # enforce the same non-null invariant.
    role = Column(String(20), nullable=False)
    chat_seq = Column(BigInteger, nullable=False)
    content = Column(Text, nullable=False)
    model = Column(String(100))
    # 思考过程，与正文分开存：``[{"content": str}]``。历史上思考是以 <think>…</think>
    # 拼进 content 落库的，结果思考把正文顶出 content 的 10 万字上限、截断切掉闭合
    # 标签后整段漏成正文。NULL = 老消息，思考仍内联在 content 里。
    # 展示顺序不在这里——它记在 ``metadata.segments``（见下）。
    thinking = Column(JSONType)
    tool_calls = Column(JSONType)
    # Nullable derived preview; NULL safely falls back to the canonical result.
    from core.db.history_projection import display_default
    tool_calls_display = deferred(Column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        default=display_default, onupdate=display_default,
    ))
    # 模型回放用的规范记录：按发生顺序的每一步（模型响应块 / 工具结果），见
    # ``core/llm/runtime/model_steps.py``。上面三列是给界面看的投影，模型上下文从这一列恢复。
    # NULL = 该行写入时还没有这份记录，回放退回 segments / 历史摘要。
    model_steps = Column(JSONType)
    usage = Column(JSONType)
    error = Column(JSONType)
    # ``metadata.segments`` 记这条消息的展示顺序，形如
    # ``[{"type":"text","text":str} | {"type":"thinking","index":int}
    #    | {"type":"tool","index":int}]``：顺序在实时流式的那一刻就已确定，原样记下，
    # 刷新后照着渲染。正文片段内联，思考与工具卡片按下标引用上面两列，长推理不存两份。
    # 缺失 = 段落表上线之前的老消息，展示退化成「正文 + 工具卡片」，不做顺序反推。
    extra_data = Column("metadata", JSONType, default={})
    usage_is_history = Column(
        Boolean, Computed("coalesce(CAST(metadata ->> 'forked_history' AS BOOLEAN), false)", persisted=True)
    )
    recorded_run_id = Column(
        String(64), Computed("metadata ->> 'run_id'", persisted=True)
    )
    usage_provider_id = Column(
        String(255), Computed("nullif(trim(metadata ->> 'model_provider_id'), '')", persisted=True)
    )

    created_at = Column(UTCDateTime(timezone=True), default=utc_now)

    # Relationships
    session = relationship("ChatSession", back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="chat_messages_role_check",
        ),
        CheckConstraint(
            "length(content) <= 100000", name="chat_messages_content_length"
        ),
        Index("idx_chat_messages_chat_id", "chat_id"),
        UniqueConstraint("chat_id", "chat_seq", name="uq_chat_messages_chat_seq"),
        Index("idx_chat_messages_chat_seq", "chat_id", "chat_seq"),
        Index("idx_chat_messages_chat_created", "chat_id", "created_at"),
        Index("idx_chat_messages_role", "chat_id", "role"),
        Index("idx_chat_messages_created_at", "created_at"),
        Index("idx_messages_usage", "role", "usage_is_history", "created_at", "message_id"),
        Index(
            "idx_chat_messages_tool_calls_gin",
            "tool_calls",
            postgresql_using="gin",
            postgresql_where=Column("tool_calls").isnot(None),
        ),
    )


class ChatCompactionState(Base):
    """Single authoritative compaction lineage and short-lived writer lease."""

    __tablename__ = "chat_compaction_states"

    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        primary_key=True,
    )
    active_checkpoint_id = Column(
        String(64),
        ForeignKey("chat_messages.message_id", ondelete="SET NULL"),
        nullable=True,
    )
    checkpoint_version = Column(Integer, nullable=False, default=0, server_default="0")
    covered_seq = Column(BigInteger, nullable=False, default=0, server_default="0")
    lease_owner = Column(String(160), nullable=True)
    lease_expires_at = Column(UTCDateTime(timezone=True), nullable=True)
    updated_at = Column(
        UTCDateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "checkpoint_version >= 0", name="chat_compaction_version_nonnegative"
        ),
        CheckConstraint(
            "covered_seq >= 0", name="chat_compaction_covered_seq_nonnegative"
        ),
        Index("idx_chat_compaction_lease", "lease_expires_at"),
    )


def reserve_chat_sequences(
    executor,
    chat_id: str,
    count: int = 1,
    *,
    owner_user_id: str | None = None,
) -> int:
    """Atomically reserve ``count`` sequence numbers and return the first one.

    ``executor`` may be an ORM Session or a SQLAlchemy Connection.  Keeping the
    primitive at the model boundary lets both the public ChatSequencer and the
    defensive before-insert hook share exactly the same allocator.
    """

    if count < 1:
        raise ValueError("count must be positive")
    table = ChatSession.__table__
    message_table = ChatMessage.__table__
    statement = update(table).where(table.c.chat_id == chat_id)
    if owner_user_id is not None:
        statement = statement.where(table.c.user_id == owner_user_id)
    max_next = (
        select(func.coalesce(func.max(message_table.c.chat_seq), 0) + 1)
        .where(message_table.c.chat_id == chat_id)
        .scalar_subquery()
    )
    current = func.coalesce(table.c.next_message_seq, 1)
    first = case((current >= max_next, current), else_=max_next)
    next_value = executor.execute(
        statement.values(next_message_seq=first + count).returning(table.c.next_message_seq)
    ).scalar_one_or_none()
    if next_value is None:
        if owner_user_id is not None:
            raise PermissionError(
                f"chat session {chat_id} does not exist or belongs to another user"
            )
        raise ValueError(f"chat session {chat_id} does not exist")
    return int(next_value) - count


def reserve_chat_message_sequences(
    executor, chat_id: str, count: int = 1
) -> int | None:
    """Compatibility name used by repository writers; returns the first slot."""
    try:
        return reserve_chat_sequences(executor, chat_id, count=count)
    except ValueError:
        return None


@event.listens_for(ChatMessage, "before_insert")
def assign_chat_sequence(_mapper, connection, target: ChatMessage) -> None:
    """Compatibility guard for legacy/direct message writers.

    New main-chat admission reserves its user/reply pair explicitly.  Existing
    background jobs and tests may still construct ChatMessage directly; this
    hook keeps the non-null/unique invariant without creating a second allocator.
    """

    if target.chat_seq is None:
        target.chat_seq = reserve_chat_sequences(connection, target.chat_id)
