"""Scheduled task and run models; loaded independently of scheduler enablement."""
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now

from datetime import datetime, timezone

from core.db.models import Base, Plan, UserShadow
from sqlalchemy import (
    JSON,
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
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")


class ScheduledTask(Base):
    """Automation — scheduled tasks table"""

    __tablename__ = "scheduled_tasks"

    task_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False
    )

    # Task content — either prompt or plan, one of the two
    task_type = Column(String(20), nullable=False)  # "prompt" | "plan"
    prompt = Column(Text)
    plan_id = Column(String(64), ForeignKey("plans.plan_id", ondelete="SET NULL"))

    # Scheduling config
    cron_expression = Column(String(100), nullable=False)
    recurring = Column(Boolean, nullable=False, default=True)
    timezone = Column(String(50), nullable=False, default="Asia/Shanghai")
    schedule_type = Column(
        String(20), nullable=False, default="recurring"
    )  # "recurring" | "once" | "manual"

    # Execution capability config
    enabled_mcp_ids = Column(JSONType, default=list)
    enabled_skill_ids = Column(JSONType, default=list)
    enabled_kb_ids = Column(JSONType, default=list)
    enabled_agent_ids = Column(JSONType, default=list)

    # Status
    status = Column(String(20), nullable=False, default="active")
    next_run_at = Column(UTCDateTime(timezone=True))
    last_run_at = Column(UTCDateTime(timezone=True))
    # 用户点「立即执行」时写入，调度器取走后清空（见 AutomationService.request_manual_trigger）。
    manual_trigger_at = Column(UTCDateTime(timezone=True))
    run_count = Column(Integer, default=0)
    max_runs = Column(Integer)

    # Failure tracking
    consecutive_failures = Column(Integer, default=0)
    max_failures = Column(Integer, default=3)
    last_error = Column(Text)

    # Metadata
    name = Column(String(200))
    description = Column(Text, default="")
    extra_data = Column("metadata", JSONType, default={})
    sidebar_activated = Column(Boolean, default=False, nullable=False)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    # Relationships
    user = relationship(UserShadow)
    plan = relationship(Plan)
    run_history = relationship(
        "ScheduledTaskRun",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ScheduledTaskRun.started_at.desc()",
    )

    __table_args__ = (
        CheckConstraint(
            "task_type IN ('prompt', 'plan', 'loop')", name="scheduled_tasks_type_check"
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'disabled', 'completed', 'expired')",
            name="scheduled_tasks_status_check",
        ),
        CheckConstraint(
            "schedule_type IN ('recurring', 'once', 'manual')",
            name="scheduled_tasks_schedule_type_check",
        ),
        Index("idx_scheduled_tasks_user_id", "user_id"),
        Index("idx_scheduled_tasks_status", "status"),
        Index("idx_scheduled_tasks_user_status", "user_id", "status"),
        Index(
            "idx_scheduled_tasks_manual_trigger",
            "manual_trigger_at",
            postgresql_where=text("manual_trigger_at IS NOT NULL"),
        ),
    )


class ScheduledTaskRun(Base):
    """Automation — execution records table"""

    __tablename__ = "scheduled_task_runs"

    run_id = Column(String(64), primary_key=True)
    task_id = Column(
        String(64), ForeignKey("scheduled_tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    status = Column(String(20), nullable=False, default="running")
    lease_token = Column(String(64), nullable=True)
    chat_id = Column(String(64))
    result_summary = Column(Text)
    error_message = Column(Text)
    started_at = Column(UTCDateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(UTCDateTime(timezone=True))
    duration_ms = Column(Integer)
    usage = Column(JSONType, default={})

    task = relationship("ScheduledTask", back_populates="run_history")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed')",
            name="scheduled_task_runs_status_check",
        ),
        Index("idx_scheduled_task_runs_task_id", "task_id"),
        Index("idx_scheduled_task_runs_started_at", "started_at"),
    )
