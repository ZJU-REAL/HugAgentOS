"""SQLAlchemy ORM models — call logs / audit."""

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


class ToolCallLog(Base):
    """Tool call log — one row per MCP / built-in tool execution."""
    __tablename__ = "tool_call_logs"

    id               = Column(String(64), primary_key=True)
    trace_id         = Column(String(64))
    chat_id          = Column(String(64), index=True)
    message_id       = Column(String(64))
    user_id          = Column(String(64), index=True)
    user_name        = Column(String(255))
    tool_name        = Column(String(128), nullable=False)
    tool_display_name= Column(String(255))
    tool_call_id     = Column(String(64))
    mcp_server       = Column(String(64))
    # Sandbox instance id (only set for sandbox tools bash / sandbox_put_artifact / sandbox_get_artifact):
    # links "which tool call → which sandbox instance → which user" together, supporting audit filtering by sandbox.
    sandbox_id       = Column(String(128), index=True)
    tool_args        = Column(JSONType)
    tool_result      = Column(JSONType)
    result_truncated = Column(Boolean, default=False, nullable=False)
    status           = Column(String(20), nullable=False, default="success")
    error_message    = Column(Text)
    duration_ms      = Column(Integer)
    source           = Column(String(20), nullable=False, default="main_agent")
    subagent_log_id  = Column(String(64), index=True)
    skill_log_id     = Column(String(64), index=True)
    started_at       = Column(TIMESTAMP(timezone=True))
    created_at       = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

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


class SubAgentCallLog(Base):
    """Sub-agent call log — a full execution record of one sub-agent / plan step."""
    __tablename__ = "subagent_call_logs"

    id                     = Column(String(64), primary_key=True)
    trace_id               = Column(String(64))
    chat_id                = Column(String(64), index=True)
    message_id             = Column(String(64))
    user_id                = Column(String(64), index=True)
    user_name              = Column(String(255))
    subagent_id            = Column(String(64))
    subagent_name          = Column(String(128), nullable=False)
    subagent_type          = Column(String(32))  # plan_mode / report_generator / user_agent ...
    plan_id                = Column(String(64))
    step_id                = Column(String(64))
    step_index             = Column(Integer)
    step_title             = Column(String(500))
    model                  = Column(String(128))
    input_messages         = Column(JSONType)
    output_content         = Column(Text)
    intermediate_steps     = Column(JSONType)
    token_usage            = Column(JSONType)
    tool_calls_count       = Column(Integer, default=0)
    skill_calls_count      = Column(Integer, default=0)
    status                 = Column(String(20), nullable=False, default="running")
    error_message          = Column(Text)
    duration_ms            = Column(Integer)
    parent_subagent_log_id = Column(String(64), index=True)
    started_at             = Column(TIMESTAMP(timezone=True))
    completed_at           = Column(TIMESTAMP(timezone=True))
    created_at             = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

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

    id                = Column(String(64), primary_key=True)
    trace_id          = Column(String(64))
    chat_id           = Column(String(64), index=True)
    message_id        = Column(String(64))
    user_id           = Column(String(64), index=True)
    user_name         = Column(String(255))
    skill_id          = Column(String(128), nullable=False)
    skill_name        = Column(String(255))
    skill_version     = Column(String(50))
    skill_source      = Column(String(20))          # filesystem / database
    invocation_type   = Column(String(20), nullable=False, default="auto_load")
    script_name       = Column(String(255))
    script_language   = Column(String(32))
    script_args       = Column(JSONType)
    script_stdin      = Column(Text)
    script_stdout     = Column(Text)
    script_stderr     = Column(Text)
    output_truncated  = Column(Boolean, default=False, nullable=False)
    exit_code         = Column(Integer)
    status            = Column(String(20), nullable=False, default="success")
    error_message     = Column(Text)
    duration_ms       = Column(Integer)
    source            = Column(String(20), nullable=False, default="main_agent")
    subagent_log_id   = Column(String(64), index=True)
    started_at        = Column(TIMESTAMP(timezone=True))
    created_at        = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

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


class AuditLog(Base):
    """Audit log table - record all critical operations."""
    __tablename__ = "audit_logs"

    log_id = Column(BigIntPK, primary_key=True, autoincrement=True)
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="SET NULL"))
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50))
    resource_id = Column(String(64))
    # Sandbox instance id (only set for sandbox operations sandbox.bash.exec / sandbox.artifact.*):
    # lets the security management → audit log distinguish and filter by "sandbox instance", i.e. "who ran what in which sandbox".
    sandbox_id = Column(String(128), index=True)
    details = Column(JSONType, default={})
    ip_address = Column(INETType)
    user_agent = Column(Text)
    trace_id = Column(String(64))
    status = Column(String(20), default="success")
    error_code = Column(Integer)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('success', 'failure', 'error')", name="audit_logs_status_check"),
        Index("idx_audit_logs_user_id", "user_id"),
        Index("idx_audit_logs_action", "action"),
        Index("idx_audit_logs_resource", "resource_type", "resource_id"),
        Index("idx_audit_logs_sandbox_created", "sandbox_id", "created_at"),
        Index("idx_audit_logs_created_at", "created_at"),
        Index("idx_audit_logs_user_created", "user_id", "created_at"),
        Index("idx_audit_logs_trace_id", "trace_id"),
        Index("idx_audit_logs_status", "status", "created_at", postgresql_where=Column("status") != "success"),
    )
