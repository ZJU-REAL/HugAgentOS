"""SQLAlchemy ORM models — automation / batch / distillation."""

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


class ScheduledTask(Base):
    """Automation — scheduled tasks table"""
    __tablename__ = "scheduled_tasks"

    task_id           = Column(String(64), primary_key=True)
    user_id           = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)

    # Task content — either prompt or plan, one of the two
    task_type         = Column(String(20), nullable=False)              # "prompt" | "plan"
    prompt            = Column(Text)
    plan_id           = Column(String(64), ForeignKey("plans.plan_id", ondelete="SET NULL"))

    # Scheduling config
    cron_expression   = Column(String(100), nullable=False)
    recurring         = Column(Boolean, nullable=False, default=True)
    timezone          = Column(String(50), nullable=False, default="Asia/Shanghai")
    schedule_type     = Column(String(20), nullable=False, default="recurring")  # "recurring" | "once" | "manual"

    # Execution capability config
    enabled_mcp_ids   = Column(JSONType, default=list)
    enabled_skill_ids = Column(JSONType, default=list)
    enabled_kb_ids    = Column(JSONType, default=list)
    enabled_agent_ids = Column(JSONType, default=list)

    # Status
    status            = Column(String(20), nullable=False, default="active")
    next_run_at       = Column(TIMESTAMP(timezone=True))
    last_run_at       = Column(TIMESTAMP(timezone=True))
    run_count         = Column(Integer, default=0)
    max_runs          = Column(Integer)

    # Failure tracking
    consecutive_failures = Column(Integer, default=0)
    max_failures      = Column(Integer, default=3)
    last_error        = Column(Text)

    # Metadata
    name              = Column(String(200))
    description       = Column(Text, default="")
    extra_data        = Column("metadata", JSONType, default={})
    sidebar_activated = Column(Boolean, default=False, nullable=False)
    created_at        = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at        = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user        = relationship("UserShadow")
    plan        = relationship("Plan")
    run_history = relationship("ScheduledTaskRun", back_populates="task",
                               cascade="all, delete-orphan",
                               order_by="ScheduledTaskRun.started_at.desc()")

    __table_args__ = (
        CheckConstraint("task_type IN ('prompt', 'plan', 'loop')", name="scheduled_tasks_type_check"),
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
    )


class ScheduledTaskRun(Base):
    """Automation — execution records table"""
    __tablename__ = "scheduled_task_runs"

    run_id          = Column(String(64), primary_key=True)
    task_id         = Column(String(64), ForeignKey("scheduled_tasks.task_id", ondelete="CASCADE"), nullable=False)
    status          = Column(String(20), nullable=False, default="running")
    chat_id         = Column(String(64))
    result_summary  = Column(Text)
    error_message   = Column(Text)
    started_at      = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at    = Column(TIMESTAMP(timezone=True))
    duration_ms     = Column(Integer)
    usage           = Column(JSONType, default={})

    task = relationship("ScheduledTask", back_populates="run_history")

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed')",
            name="scheduled_task_runs_status_check",
        ),
        Index("idx_scheduled_task_runs_task_id", "task_id"),
        Index("idx_scheduled_task_runs_started_at", "started_at"),
    )


class DistillationRun(Base):
    """Distillation task queue + audit. At most one row per chat_id (UNIQUE)."""
    __tablename__ = "distillation_runs"

    run_id        = Column(String(64), primary_key=True)
    chat_id       = Column(String(64), nullable=False)
    trigger       = Column(String(16), nullable=False)  # daily_cron | manual
    status        = Column(String(32), nullable=False, default="queued")
    skip_reason   = Column(String(64))
    draft_id      = Column(String(64))
    cost_usd      = Column(Numeric(8, 4), default=0)
    error_message = Column(Text)
    started_at    = Column(TIMESTAMP(timezone=True))
    finished_at   = Column(TIMESTAMP(timezone=True))
    created_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("chat_id", name="distillation_runs_chat_id_unique"),
        CheckConstraint(
            "trigger IN ('daily_cron', 'manual')",
            name="distillation_runs_trigger_check",
        ),
        CheckConstraint(
            "status IN ('queued','running','skipped','completed_distilled','failed_parse','failed_budget','failed_model')",
            name="distillation_runs_status_check",
        ),
        Index("idx_distillation_runs_status", "status", "created_at"),
        Index("idx_distillation_runs_created_at", "created_at"),
    )


class PersonaDistillJob(Base):
    """Persona-level distillation job (colleague skill / personal skill).

    Unlike DistillationRun (single conversation, cron-driven): one job
    aggregates a user's multiple conversations + memories, executed in two
    stages — map (per-conversation summaries) → reduce (synthesize SKILL.md).
    In colleague mode the output is mirrored into admin_skill_drafts for
    review; in personal mode it lands directly as AdminSkill(owner=the user)
    after the user's own confirmation.
    """
    __tablename__ = "persona_distill_jobs"

    job_id               = Column(String(64), primary_key=True)          # pdj_<16hex>
    kind                 = Column(String(16), nullable=False)            # colleague | personal
    target_user_id       = Column(String(64), nullable=False)            # the user being distilled
    requested_by         = Column(String(64), nullable=False)            # initiator (config_admin or user_id)
    scope                = Column(JSONType, default=dict)                # chat_ids / date range / memory switch / hint
    status               = Column(String(16), nullable=False, default="queued")
    progress_done        = Column(Integer, nullable=False, default=0)    # conversations completed in the map stage
    progress_total       = Column(Integer, nullable=False, default=0)
    intermediate         = Column(JSONType, default=list)                # list of conversation summaries (map output)
    result_skill_content = Column(Text)                                  # full SKILL.md text (reduce output)
    result_meta          = Column(JSONType, default=dict)                # proposed_skill_id/display_name/...
    result_draft_id      = Column(String(64))                            # mirrored draft in colleague mode
    saved_skill_id       = Column(String(100))                           # AdminSkill.skill_id after confirmed persistence
    cost_usd             = Column(Numeric(8, 4), default=0)
    error                = Column(Text)
    created_at           = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    started_at           = Column(TIMESTAMP(timezone=True))
    finished_at          = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('colleague', 'personal')",
            name="persona_distill_jobs_kind_check",
        ),
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="persona_distill_jobs_status_check",
        ),
        Index("idx_persona_distill_jobs_target", "target_user_id", "created_at"),
        Index("idx_persona_distill_jobs_status", "status"),
    )


# ─── Memory Layering (L1 Profile / Audit / Sanitizer rules) ────────────────


class BatchPlan(Base):
    """Batch execution plan (generated by the batch_plan MCP tool; executed by BatchOrchestrator after user confirmation)."""
    __tablename__ = "batch_plans"

    plan_id          = Column(String(64), primary_key=True)
    user_id          = Column(String(64), nullable=False)
    chat_id          = Column(String(64))                                # optional associated conversation
    source_type      = Column(String(20), nullable=False)                # xlsx | word_files | text_list
    items            = Column(JSONType, nullable=False, default=list)    # list[dict]
    placeholder_keys = Column(JSONType, default=list)                    # placeholders available to the template
    instruction      = Column(Text)                                      # the user's original batch goal
    prompt_template  = Column(Text, nullable=False)
    max_retries      = Column(Integer, nullable=False, default=2)
    status           = Column(String(20), nullable=False, default="pending")
    progress         = Column(JSONType, default=dict)                    # {done, success, failed}
    created_at       = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at       = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at       = Column(TIMESTAMP(timezone=True))                  # +24h, cleaned up periodically

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','confirmed','running','done','failed','cancelled')",
            name="batch_plans_status_check",
        ),
        CheckConstraint(
            "source_type IN ('xlsx','word_files','text_list')",
            name="batch_plans_source_type_check",
        ),
        Index("idx_batch_plans_user_id", "user_id"),
        Index("idx_batch_plans_chat_id", "chat_id"),
        Index("idx_batch_plans_expires_at", "expires_at"),
    )
