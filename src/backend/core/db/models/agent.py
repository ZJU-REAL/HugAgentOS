"""SQLAlchemy ORM models — user agents / plans."""

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


class UserAgent(Base):
    """Custom sub-agent (admin-created or user-created)."""
    __tablename__ = "user_agents"

    agent_id        = Column(String(64), primary_key=True)
    owner_type      = Column(String(10), nullable=False)               # "admin" | "user" | "team"
    user_id         = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"))
    # Team sub-agent: when owner_type='team' it hangs under a team, visible and usable by all members, managed by the team owner/admin.
    team_id         = Column(String(64), ForeignKey("teams.team_id", ondelete="CASCADE"))
    name            = Column(String(255), nullable=False)
    avatar          = Column(Text)
    description     = Column(Text, default="")

    # Core config
    system_prompt       = Column(Text, nullable=False, default="")
    welcome_message     = Column(Text, default="")
    suggested_questions = Column(JSONType, default=list)

    # Capability bindings
    mcp_server_ids  = Column(JSONType, default=list)
    skill_ids       = Column(JSONType, default=list)
    kb_ids          = Column(JSONType, default=list)
    # Bound plugins (install_id list): a plugin is a "skills + MCP" bundle unit, expanded
    # at runtime into its component skills/tools. Complementary to skill_ids/mcp_server_ids —
    # those two only store "loose" non-plugin capabilities.
    plugin_ids      = Column(JSONType, default=list)

    # Model config
    model_provider_id = Column(String(64), ForeignKey("model_providers.provider_id", ondelete="SET NULL"))
    temperature     = Column(Numeric(3, 2))
    max_tokens      = Column(Integer)

    # Runtime controls
    max_iters       = Column(Integer, default=10)
    timeout         = Column(Integer, default=120)
    is_enabled      = Column(Boolean, default=True)
    sort_order      = Column(Integer, default=0)

    # Advanced config
    extra_config    = Column(JSONType, default=dict)

    # Sub-agent marketplace origin: non-empty means this agent was "install-cloned" from a
    # marketplace listing (value = marketplace slug). Used for the "installed" badge in the
    # marketplace list and to prevent duplicate installs; user-created / export-imported agents leave this column empty.
    source_market_slug = Column(String(128))

    # Metadata
    created_at      = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at      = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by      = Column(String(64))

    # Relationships
    user            = relationship("UserShadow", foreign_keys=[user_id], back_populates="user_agents")
    model_provider  = relationship("ModelProvider")

    __table_args__ = (
        CheckConstraint("owner_type IN ('admin', 'user', 'team')", name="user_agents_owner_type_check"),
        # team_id exists only under the team scope; must be empty for all other scopes
        CheckConstraint(
            "(owner_type <> 'team' AND team_id IS NULL) OR (owner_type = 'team' AND team_id IS NOT NULL)",
            name="user_agents_team_scope_check",
        ),
        Index("idx_user_agents_owner_type", "owner_type"),
        Index("idx_user_agents_user_id", "user_id"),
        Index("idx_user_agents_team_id", "team_id"),
        Index("idx_user_agents_is_enabled", "is_enabled"),
        Index("idx_user_agents_sort_order", "sort_order"),
        Index("idx_user_agents_updated_at", "updated_at"),
        Index("idx_user_agents_source_market_slug", "source_market_slug"),
    )


class AgentMarketSubmission(Base):
    """Record of a user-created sub-agent's "apply for listing on the sub-agent marketplace" (pending admin review).

    Mirrors the skill marketplace's ``MarketplaceSubmission``, but snapshots a
    "sub-agent" rather than skill files: on submission, the source ``UserAgent`` is
    content-snapshotted (prompt/welcome message/suggested questions/model config/
    capability bindings); review and listing are both based on the snapshot — later
    edits or deletion of the original agent do not affect listed content.
    ``status='approved'`` records appear in the sub-agent marketplace list
    (source=community) and can be "install-cloned" by everyone as a private sub-agent;
    admins can reject at any time (including delisting already-listed items). Direct
    admin uploads to the marketplace are distinguished by the sentinel owner
    ``__admin_upload__``.
    """
    __tablename__ = "agent_market_submissions"

    submission_id  = Column(String(64), primary_key=True)
    # Marketplace slug once listed (derived from name at submission, globally unique, never colliding with the preset marketplace catalog)
    slug           = Column(String(128), nullable=False, unique=True)
    agent_id       = Column(String(64), nullable=False)    # source UserAgent.agent_id
    owner_user_id  = Column(String(64), nullable=False)    # applicant (or __admin_upload__)
    submitter_name = Column(String(255), default="")       # applicant display name (denormalized for display)

    # Marketplace display metadata
    name           = Column(String(255), nullable=False)
    avatar         = Column(Text)
    description    = Column(Text, default="")
    summary        = Column(Text, default="")
    category       = Column(String(64), default="通用助手")
    tags           = Column(JSONType, default=list)
    version        = Column(String(50), default="1.0.0")
    note           = Column(Text, default="")              # application note (for the admin)

    # Sub-agent content snapshot (decoupled from the source UserAgent)
    system_prompt        = Column(Text, default="")
    welcome_message      = Column(Text, default="")
    suggested_questions  = Column(JSONType, default=list)
    # {temperature, max_tokens, max_iters, timeout}
    model_config_snapshot = Column(JSONType, default=dict)
    # {skill_ids, mcp_server_ids, plugin_ids, kb_ids}
    bindings_snapshot     = Column(JSONType, default=dict)

    status         = Column(String(16), nullable=False, default="pending")
    review_note    = Column(Text)                          # rejection reason / review note
    reviewed_at    = Column(TIMESTAMP(timezone=True))
    created_at     = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at     = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="agent_market_submissions_status_check",
        ),
        Index("idx_agent_market_submissions_status", "status", "created_at"),
        Index("idx_agent_market_submissions_owner", "owner_user_id"),
    )


class Plan(Base):
    """Plan mode - plans table"""
    __tablename__ = "plans"

    plan_id         = Column(String(64), primary_key=True)
    user_id         = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    title           = Column(String(500), nullable=False)
    description     = Column(Text, default="")
    task_input      = Column(Text, nullable=False)
    status          = Column(String(20), nullable=False, default="draft")
    total_steps     = Column(Integer, default=0)
    completed_steps = Column(Integer, default=0)
    result_summary  = Column(Text)
    extra_data      = Column("metadata", JSONType, default={})
    created_at      = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at      = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    steps = relationship("PlanStep", back_populates="plan",
                         cascade="all, delete-orphan", order_by="PlanStep.step_order")

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'running', 'completed', 'failed', 'cancelled')",
            name="plans_status_check",
        ),
        Index("idx_plans_user_id", "user_id"),
        Index("idx_plans_status", "status"),
    )


class PlanStep(Base):
    """Plan mode - steps table"""
    __tablename__ = "plan_steps"

    step_id         = Column(String(64), primary_key=True)
    plan_id         = Column(String(64), ForeignKey("plans.plan_id", ondelete="CASCADE"), nullable=False)
    step_order      = Column(Integer, nullable=False)
    title           = Column(String(500), nullable=False)
    description     = Column(Text, default="")
    expected_tools  = Column(JSONType, default=list)
    expected_skills = Column(JSONType, default=list)
    expected_agents = Column(JSONType, default=list)
    status          = Column(String(20), nullable=False, default="pending")
    result_summary  = Column(Text)
    tool_calls_log  = Column(JSONType, default=list)
    ai_output       = Column(Text)
    error_message   = Column(Text)
    started_at      = Column(TIMESTAMP(timezone=True))
    completed_at    = Column(TIMESTAMP(timezone=True))

    plan = relationship("Plan", back_populates="steps")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'skipped')",
            name="plan_steps_status_check",
        ),
        Index("idx_plan_steps_plan_id", "plan_id"),
    )


class AgentLoop(Base):
    """Autonomous loop main table — one long-running autonomous loop instance.

    State (filesystem-as-memory): PROGRESS.md/handoffs.md live in the persistent
    sandbox; the DB stores lineage/budget/audit indexes. See
    internal design docs (§3.1). CE table (not in EE_ONLY_TABLES).
    """
    __tablename__ = "agent_loops"

    loop_id           = Column(String(64), primary_key=True)
    user_id           = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    chat_id           = Column(String(64))
    title             = Column(String(500), default="")
    # goal_spec: {objective, acceptance_criteria[], verify_cmd, score_regex, target_score, maximize}
    goal_spec         = Column(JSONType, default=dict)
    # budget: {max_iters, max_wall_clock_s, max_tokens, max_subagents}
    budget            = Column(JSONType, default=dict)
    workspace_session = Column(String(128))
    status            = Column(String(24), nullable=False, default="created")
    iteration_count   = Column(Integer, default=0)
    tokens_spent      = Column(BigInteger, default=0)
    final_score       = Column(Numeric)
    result_summary    = Column(Text)
    extra_data        = Column("metadata", JSONType, default=dict)
    created_at        = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at        = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    iterations = relationship("LoopIteration", back_populates="loop",
                              cascade="all, delete-orphan", order_by="LoopIteration.seq")

    __table_args__ = (
        CheckConstraint(
            "status IN ('created','running','paused','awaiting_human','completed',"
            "'budget_exhausted','failed','cancelled')",
            name="agent_loops_status_check",
        ),
        Index("idx_agent_loops_user_id", "user_id"),
        Index("idx_agent_loops_status", "status"),
    )


class LoopIteration(Base):
    """Per-iteration audit trail of an autonomous loop (decision log). See §3.2."""
    __tablename__ = "loop_iterations"

    iteration_id     = Column(String(64), primary_key=True)
    loop_id          = Column(String(64), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False)
    seq              = Column(Integer, nullable=False)
    run_id           = Column(String(64))
    verdict          = Column(String(20))
    score            = Column(Numeric)
    evidence         = Column(Text)          # environment evidence (verify output)
    reasoning        = Column(Text)          # evaluator feedback / rationale
    handoff_summary  = Column(Text)
    tool_calls       = Column(Integer, default=0)
    tokens           = Column(Integer, default=0)
    decided_by       = Column(String(20))    # environment / llm / fallback
    created_at       = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    loop = relationship("AgentLoop", back_populates="iterations")

    __table_args__ = (
        Index("idx_loop_iterations_loop_id", "loop_id"),
    )
