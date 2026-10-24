"""ORM models for chat runs, their operation journal and the steering queue."""
from __future__ import annotations
from core.db.utc_datetime import UTCDateTime
from datetime import datetime, timezone
from core.db.engine import Base
from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import deferred
from core.db.models.column_types import BigIntPK, JSONType

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
    lease_expires_at = Column(UTCDateTime(timezone=True))
    operation_seq = Column(Integer, nullable=False, default=0)
    snapshot_version = Column(Integer, nullable=False, default=0)
    # Holds the whole in-flight run payload (megabytes on long chats); loading it
    # with every ChatRun row decodes that JSON again, so it is fetched on access.
    recovery_snapshot = deferred(Column(JSONType))
    last_operation_safety = Column(String(40), nullable=False, default="replayable")
    failure_reason = Column(Text)
    updated_at = Column(
        UTCDateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    created_at = Column(
        UTCDateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    started_at = Column(UTCDateTime(timezone=True))
    completed_at = Column(UTCDateTime(timezone=True))

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
        UTCDateTime(timezone=True),
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
    lease_expires_at = Column(UTCDateTime(timezone=True))
    delivery_attempt = Column(Integer, nullable=False, default=0)
    superseded_by = Column(String(64))
    applied_run_id = Column(String(64))
    applied_source_run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    applied_operation_seq = Column(Integer)
    accepted_at = Column(
        UTCDateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    claimed_at = Column(UTCDateTime(timezone=True))
    applied_at = Column(UTCDateTime(timezone=True))
    cancelled_at = Column(UTCDateTime(timezone=True))
    updated_at = Column(
        UTCDateTime(timezone=True),
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
