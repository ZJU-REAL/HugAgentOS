"""SQLAlchemy ORM models — call logs / audit."""

from datetime import datetime, timezone

from core.db.engine import Base
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
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")
# BigInteger autoincrement PK: SQLite only auto-increments the INTEGER rowid, not
# BIGINT, so an unqualified BigInteger PK stays NULL on the no-Docker SQLite
# profile. Fall back to Integer on SQLite (still BIGINT on Postgres).
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class ToolCallLog(Base):
    """Tool call log — one row per MCP / built-in tool execution."""

    __tablename__ = "tool_call_logs"

    id = Column(String(64), primary_key=True)
    # Query projection linkage. ToolEffectLedger is the recovery authority;
    # this nullable key lets the existing audit UI join back to that fact.
    effect_id = Column(String(64), unique=True, index=True)
    trace_id = Column(String(64))
    chat_id = Column(String(64), index=True)
    message_id = Column(String(64))
    user_id = Column(String(64), index=True)
    user_name = Column(String(255))
    tool_name = Column(String(128), nullable=False)
    tool_display_name = Column(String(255))
    tool_call_id = Column(String(64))
    mcp_server = Column(String(64))
    # Sandbox instance id (only set for sandbox tools bash / sandbox_put_artifact / sandbox_get_artifact):
    # links "which tool call → which sandbox instance → which user" together, supporting audit filtering by sandbox.
    sandbox_id = Column(String(128), index=True)
    tool_args = Column(JSONType)
    tool_result = Column(JSONType)
    result_truncated = Column(Boolean, default=False, nullable=False)
    status = Column(String(20), nullable=False, default="success")
    error_message = Column(Text)
    duration_ms = Column(Integer)
    source = Column(String(20), nullable=False, default="main_agent")
    subagent_log_id = Column(String(64), index=True)
    skill_log_id = Column(String(64), index=True)
    started_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'failed', 'timeout')",
            name="tool_call_logs_status_check",
        ),
        CheckConstraint(
            "source IN ('main_agent', 'subagent', 'skill', 'automation')",
            name="tool_call_logs_source_check",
        ),
        Index("idx_tool_call_logs_created_at", "created_at"),
        Index("idx_tool_call_logs_user_created", "user_id", "created_at"),
        Index("idx_tool_call_logs_chat_created", "chat_id", "created_at"),
        Index("idx_tool_call_logs_tool_name", "tool_name", "created_at"),
        Index("idx_tool_call_logs_status", "status", "created_at"),
        Index("idx_tool_call_logs_trace_id", "trace_id"),
        Index("idx_tool_call_logs_sandbox_created", "sandbox_id", "created_at"),
    )


class ToolEffectLedger(Base):
    """Append-only facts for tool intent, result/failure and unknown outcomes."""

    __tablename__ = "tool_effect_ledger"

    event_id = Column(BigIntPK, primary_key=True, autoincrement=True)
    effect_id = Column(String(64), nullable=False, index=True)
    # Pre-allocated before invocation; shared by the intent and terminal fact.
    result_id = Column(String(64), nullable=False)
    run_id = Column(String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), nullable=False)
    operation_seq = Column(Integer, nullable=False)
    event_type = Column(String(32), nullable=False)
    tool_name = Column(String(128), nullable=False)
    tool_call_id = Column(String(128))
    args_hash = Column(String(64), nullable=False)
    redacted_args = Column(JSONType)
    # Only the intent row carries this value. A unique constraint therefore
    # gives one authoritative effect per caller-provided stable key.
    idempotency_key = Column(String(128), unique=True)
    recovery_policy = Column(String(24), nullable=False)
    result_payload = Column(JSONType)
    error_message = Column(Text)
    # Only terminal facts carry this value, preventing Result/Failure/Unknown
    # races from creating two different outcomes for one effect.
    terminal_effect_id = Column(String(64), unique=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('intent', 'result', 'failure', 'unknown_outcome', 'recovery_claim')",
            name="tool_effect_ledger_event_type_check",
        ),
        CheckConstraint(
            "recovery_policy IN ('replay_safe', 'reconcile', 'never_replay')",
            name="tool_effect_ledger_policy_check",
        ),
        CheckConstraint("operation_seq > 0", name="tool_effect_ledger_seq_positive"),
        UniqueConstraint("run_id", "operation_seq", name="uq_tool_effect_run_operation_seq"),
        Index("idx_tool_effect_run_created", "run_id", "created_at"),
        Index("idx_tool_effect_pending", "effect_id", "terminal_effect_id"),
    )


class ToolEffectLease(Base):
    """Mutable execution claim; authoritative outcomes remain append-only above."""

    __tablename__ = "tool_effect_leases"

    effect_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), nullable=False)
    run_owner = Column(String(160), nullable=False)
    claim_owner = Column(String(160))
    lease_expires_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (Index("idx_tool_effect_leases_expiry", "lease_expires_at"),)


class ToolEffectReceipt(Base):
    """Adapter-side receipt committed atomically with a reconciled DB mutation."""

    __tablename__ = "tool_effect_receipts"

    effect_id = Column(String(64), primary_key=True)
    tool_name = Column(String(128), nullable=False)
    user_id = Column(String(64), nullable=False, index=True)
    result_payload = Column(JSONType, nullable=False)
    created_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (Index("idx_tool_effect_receipts_user_created", "user_id", "created_at"),)


class HarnessUsageCursor(Base):
    """Mutable allocator only; usage attempt facts remain append-only."""

    __tablename__ = "harness_usage_cursors"

    run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    next_attempt_seq = Column(Integer, nullable=False, default=0, server_default="0")


class HarnessUsageAttempt(Base):
    """One immutable physical model, tool or hook attempt."""

    __tablename__ = "harness_usage_attempts"

    attempt_id = Column(String(64), primary_key=True)
    run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    attempt_seq = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)
    operation_name = Column(String(160), nullable=False)
    provider = Column(String(64), nullable=False, default="")
    model = Column(String(160), nullable=False, default="")
    effect_id = Column(String(64), index=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False)
    retry_of = Column(Integer)
    attempt_metadata = Column(JSONType)
    created_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("kind IN ('model', 'tool', 'hook')", name="harness_usage_kind_check"),
        CheckConstraint(
            "status IN ('success', 'failed', 'timeout', 'cancelled')",
            name="harness_usage_status_check",
        ),
        CheckConstraint("attempt_seq > 0", name="harness_usage_seq_positive"),
        UniqueConstraint("run_id", "attempt_seq", name="uq_harness_usage_run_seq"),
        Index("idx_harness_usage_run_kind", "run_id", "kind", "attempt_seq"),
    )


class HarnessEventCursor(Base):
    """Mutable per-run allocator for the append-only event stream."""

    __tablename__ = "harness_event_cursors"

    run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    next_event_seq = Column(Integer, nullable=False, default=0, server_default="0")


class HarnessEventLog(Base):
    """Append-only neutral lifecycle event."""

    __tablename__ = "harness_event_log"

    event_id = Column(String(64), primary_key=True)
    run_id = Column(
        String(64), ForeignKey("chat_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    event_seq = Column(Integer, nullable=False)
    event_type = Column(String(80), nullable=False)
    phase = Column(String(40), nullable=False)
    payload = Column(JSONType, nullable=False, default=dict)
    created_at = Column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("event_seq > 0", name="harness_event_seq_positive"),
        UniqueConstraint("run_id", "event_seq", name="uq_harness_event_run_seq"),
        Index("idx_harness_event_run_type", "run_id", "event_type", "event_seq"),
    )


class SubAgentCallLog(Base):
    """Sub-agent call log — a full execution record of one sub-agent / plan step."""

    __tablename__ = "subagent_call_logs"

    id = Column(String(64), primary_key=True)
    trace_id = Column(String(64))
    chat_id = Column(String(64), index=True)
    message_id = Column(String(64))
    user_id = Column(String(64), index=True)
    user_name = Column(String(255))
    subagent_id = Column(String(64))
    subagent_name = Column(String(128), nullable=False)
    subagent_type = Column(String(32))  # plan_mode / report_generator / user_agent ...
    plan_id = Column(String(64))
    step_id = Column(String(64))
    step_index = Column(Integer)
    step_title = Column(String(500))
    model = Column(String(128))
    input_messages = Column(JSONType)
    output_content = Column(Text)
    intermediate_steps = Column(JSONType)
    token_usage = Column(JSONType)
    tool_calls_count = Column(Integer, default=0)
    skill_calls_count = Column(Integer, default=0)
    status = Column(String(20), nullable=False, default="running")
    error_message = Column(Text)
    duration_ms = Column(Integer)
    parent_subagent_log_id = Column(String(64), index=True)
    started_at = Column(TIMESTAMP(timezone=True))
    completed_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'cancelled')",
            name="subagent_call_logs_status_check",
        ),
        Index("idx_subagent_logs_created_at", "created_at"),
        Index("idx_subagent_logs_user_created", "user_id", "created_at"),
        Index("idx_subagent_logs_chat_created", "chat_id", "created_at"),
        Index("idx_subagent_logs_subagent_name", "subagent_name", "created_at"),
        Index("idx_subagent_logs_status", "status", "created_at"),
        Index("idx_subagent_logs_plan_id", "plan_id"),
        Index("idx_subagent_logs_trace_id", "trace_id"),
    )


class SkillCallLog(Base):
    """Skill call log — records all three trigger types: view / run_script / auto_load."""

    __tablename__ = "skill_call_logs"

    id = Column(String(64), primary_key=True)
    trace_id = Column(String(64))
    chat_id = Column(String(64), index=True)
    message_id = Column(String(64))
    user_id = Column(String(64), index=True)
    user_name = Column(String(255))
    skill_id = Column(String(128), nullable=False)
    skill_name = Column(String(255))
    skill_version = Column(String(50))
    skill_source = Column(String(20))  # filesystem / database
    invocation_type = Column(String(20), nullable=False, default="auto_load")
    script_name = Column(String(255))
    script_language = Column(String(32))
    script_args = Column(JSONType)
    script_stdin = Column(Text)
    script_stdout = Column(Text)
    script_stderr = Column(Text)
    output_truncated = Column(Boolean, default=False, nullable=False)
    exit_code = Column(Integer)
    status = Column(String(20), nullable=False, default="success")
    error_message = Column(Text)
    duration_ms = Column(Integer)
    source = Column(String(20), nullable=False, default="main_agent")
    subagent_log_id = Column(String(64), index=True)
    started_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "invocation_type IN ('view', 'run_script', 'auto_load')",
            name="skill_call_logs_invocation_check",
        ),
        CheckConstraint(
            "status IN ('success', 'failed', 'timeout')",
            name="skill_call_logs_status_check",
        ),
        CheckConstraint(
            "source IN ('main_agent', 'subagent', 'automation')",
            name="skill_call_logs_source_check",
        ),
        Index("idx_skill_call_logs_created_at", "created_at"),
        Index("idx_skill_call_logs_user_created", "user_id", "created_at"),
        Index("idx_skill_call_logs_chat_created", "chat_id", "created_at"),
        Index("idx_skill_call_logs_skill_name", "skill_name", "created_at"),
        Index("idx_skill_call_logs_invocation", "invocation_type", "created_at"),
        Index("idx_skill_call_logs_status", "status", "created_at"),
        Index("idx_skill_call_logs_trace_id", "trace_id"),
    )
