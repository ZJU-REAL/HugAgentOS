"""SQLAlchemy ORM models — chat sessions/messages."""

from datetime import datetime, timezone

from core.db.engine import Base
from core.db.model_extensions import (
    ChatSessionEditionFields,
    chat_session_edition_table_args,
)
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    case,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")
# BigInteger autoincrement PK: SQLite only auto-increments the INTEGER rowid, not
# BIGINT, so an unqualified BigInteger PK stays NULL on the no-Docker SQLite
# profile. Fall back to Integer on SQLite (still BIGINT on Postgres).
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


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
    deleted_at = Column(TIMESTAMP(timezone=True))
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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_message_at = Column(TIMESTAMP(timezone=True))
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
    tool_calls = Column(JSONType)
    usage = Column(JSONType)
    error = Column(JSONType)
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

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
    lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    updated_at = Column(
        TIMESTAMP(timezone=True),
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
def _assign_chat_sequence(_mapper, connection, target: ChatMessage) -> None:
    """Compatibility guard for legacy/direct message writers.

    New main-chat admission reserves its user/reply pair explicitly.  Existing
    background jobs and tests may still construct ChatMessage directly; this
    hook keeps the non-null/unique invariant without creating a second allocator.
    """

    if target.chat_seq is None:
        target.chat_seq = reserve_chat_sequences(connection, target.chat_id)


class ChatRun(Base):
    """Chat Run — decouples AI tasks from the HTTP connection lifecycle.

    Every message send creates a run; a background asyncio.Task runs the workflow,
    chunks are written to a Redis Stream, and the SSE endpoint pulls from the
    Stream. After a page refresh, playback resumes via follow_run + offset.
    """

    __tablename__ = "chat_runs"

    run_id = Column(String(64), primary_key=True)
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(String(64), nullable=False)
    message_id = Column(
        String(64), nullable=False
    )  # pre-allocated assistant message id
    user_message_id = Column(String(64))
    user_chat_seq = Column(BigInteger)
    assistant_chat_seq = Column(BigInteger)
    # Non-null only while this run owns the chat's main-writer slot.  A unique
    # (chat_id, writer_slot) constraint permits unlimited terminal NULL rows but
    # only one live "main" owner, on both PostgreSQL and SQLite.
    writer_slot = Column(String(20))
    status = Column(String(20), nullable=False, default="pending")
    request_payload = Column(
        JSONType
    )  # serialized ChatRequest (for the worker to rebuild the context)
    last_event_offset = Column(Integer, default=0, nullable=False)
    error_message = Column(Text)
    usage = Column(JSONType)
    # Durable run-journal state. Redis remains an event projection only; these
    # columns decide ownership, recovery and terminal CAS after a restart.
    run_phase = Column(String(40), nullable=False, default="accepted")
    lease_owner = Column(String(160))
    lease_expires_at = Column(TIMESTAMP(timezone=True))
    operation_seq = Column(Integer, nullable=False, default=0)
    snapshot_version = Column(Integer, nullable=False, default=0)
    recovery_snapshot = Column(JSONType)
    last_operation_safety = Column(String(40), nullable=False, default="replayable")
    failure_reason = Column(Text)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    started_at = Column(TIMESTAMP(timezone=True))
    completed_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'needs_attention', 'completed', 'failed', 'cancelled')",
            name="chat_runs_status_check",
        ),
        CheckConstraint(
            "writer_slot IS NULL OR writer_slot = 'main'",
            name="chat_runs_writer_slot_check",
        ),
        UniqueConstraint("chat_id", "writer_slot", name="uq_chat_runs_writer_slot"),
        Index("idx_chat_runs_chat_status", "chat_id", "status"),
        Index("idx_chat_runs_status_lease", "status", "lease_expires_at"),
        Index("idx_chat_runs_user_id", "user_id"),
        Index("idx_chat_runs_created_at", "created_at"),
    )


class ChatRunOperation(Base):
    """Append-only, per-run operation journal used for recovery decisions."""

    __tablename__ = "chat_run_operations"

    operation_id = Column(BigIntPK, primary_key=True, autoincrement=True)
    run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    operation_seq = Column(Integer, nullable=False)
    operation_type = Column(String(64), nullable=False)
    phase = Column(String(40), nullable=False)
    safety = Column(String(40), nullable=False)
    owner = Column(String(160))
    snapshot_version = Column(Integer, nullable=False, default=0)
    payload = Column(JSONType)
    created_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("run_id", "operation_seq", name="uq_chat_run_operation_seq"),
        CheckConstraint("operation_seq > 0", name="chat_run_operations_seq_positive"),
        Index("idx_chat_run_operations_run_seq", "run_id", "operation_seq"),
    )


class ChatSteerQueueItem(Base):
    """Durable user input queued around a ChatRun safe boundary."""

    __tablename__ = "chat_steer_queue"

    queue_id = Column(String(64), primary_key=True)
    steer_id = Column(String(64), nullable=False)
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(String(64), nullable=False)
    target_run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    steer_seq = Column(BigInteger, nullable=False)
    target_operation_seq = Column(Integer)
    delivery_mode = Column(String(20), nullable=False, default="steer")
    status = Column(String(20), nullable=False, default="accepted")
    message = Column(Text, nullable=False)
    lease_owner = Column(String(160))
    lease_expires_at = Column(TIMESTAMP(timezone=True))
    delivery_attempt = Column(Integer, nullable=False, default=0)
    superseded_by = Column(String(64))
    applied_run_id = Column(String(64))
    applied_source_run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    applied_operation_seq = Column(Integer)
    accepted_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    claimed_at = Column(TIMESTAMP(timezone=True))
    applied_at = Column(TIMESTAMP(timezone=True))
    cancelled_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "delivery_mode IN ('steer', 'follow_up', 'next_run')",
            name="chat_steer_queue_delivery_mode_check",
        ),
        CheckConstraint(
            "status IN ('accepted', 'claimed', 'applied', 'cancelled', 'superseded')",
            name="chat_steer_queue_status_check",
        ),
        CheckConstraint("steer_seq > 0", name="chat_steer_queue_seq_positive"),
        CheckConstraint(
            "delivery_attempt >= 0", name="chat_steer_queue_attempt_nonnegative"
        ),
        CheckConstraint(
            "length(message) BETWEEN 1 AND 10000",
            name="chat_steer_queue_message_length",
        ),
        UniqueConstraint("chat_id", "steer_id", name="uq_chat_steer_queue_chat_steer"),
        UniqueConstraint("chat_id", "steer_seq", name="uq_chat_steer_queue_chat_seq"),
        Index(
            "idx_chat_steer_queue_run_status_seq",
            "target_run_id",
            "status",
            "steer_seq",
        ),
        Index(
            "idx_chat_steer_queue_chat_status_seq",
            "chat_id",
            "status",
            "steer_seq",
        ),
        Index(
            "idx_chat_steer_queue_applied_source_status_seq",
            "applied_source_run_id",
            "status",
            "steer_seq",
        ),
        Index("idx_chat_steer_queue_lease", "status", "lease_expires_at"),
    )


class MessageFeedback(Base):
    """Message feedback table - stores like/dislike ratings and optional comments."""

    __tablename__ = "message_feedback"

    feedback_id = Column(BigIntPK, primary_key=True, autoincrement=True)
    message_id = Column(
        String(64),
        ForeignKey("chat_messages.message_id", ondelete="CASCADE"),
        nullable=False,
    )
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    rating = Column(String(10), nullable=False)  # 'like' or 'dislike'
    comment = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "rating IN ('like', 'dislike')", name="message_feedback_rating_check"
        ),
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

    chat_id = Column(String(64), primary_key=True)
    snapshot_id = Column(String(64), nullable=False, unique=True)
    sandbox_id = Column(
        String(64), nullable=False
    )  # source sandbox id parked at the time, for debugging / reconciliation
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(
        TIMESTAMP(timezone=True), nullable=False
    )  # created_at + SNAPSHOT_RETENTION_DAYS
    size_bytes = Column(BigInteger)  # for metrics, nullable
    extra = Column(
        "metadata", JSONType, default=dict
    )  # reserved: image uri / pool kind / notes

    __table_args__ = (
        Index("idx_chat_sandbox_snapshots_expires", "expires_at"),
        Index("idx_chat_sandbox_snapshots_created", "created_at"),
    )
