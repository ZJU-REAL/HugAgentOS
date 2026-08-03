"""SQLAlchemy ORM models — GCE evolution evidence plane.

These tables hold *evidence*, not content.  Tool results, memory text and skill
bodies already live in their own tables; duplicating them here would widen the
privacy surface and make the event table unqueryable at volume.  What is stored
is the causal skeleton of a run plus references into the existing tables.

Note the join key: ``message_id``.  Every pre-existing log table already carries
it and the assistant message id is pre-allocated by the run executor, so history
can be reconstructed without back-filling a new column across the whole codebase.
"""

from datetime import datetime

from core.db.engine import Base
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class EvolutionEpisode(Base):
    """One task, viewed as a single causal unit.

    Assembled *after* the response closes — the response path only binds
    versions and appends events. Everything expensive happens here, off the
    user's latency budget.
    """

    __tablename__ = "evolution_episodes"

    episode_id = Column(String(64), primary_key=True)
    run_id = Column(String(64))
    message_id = Column(String(64), nullable=False)
    chat_id = Column(String(64))
    user_id = Column(String(64))
    tenant_id = Column(String(64), default="default")

    task_type = Column(String(64), default="chat")
    objective_hash = Column(String(64))
    objective_preview = Column(String(500), default="")

    # The exact asset versions this run was pinned to. Null / partial means the
    # episode cannot be used for counterfactual replay — replay's premise is
    # "everything else frozen", and without a bundle we do not know what
    # everything else was.
    asset_bundle_id = Column(String(64))
    asset_bundle = Column(JSONType)
    bundle_partial = Column(Boolean, default=False, nullable=False)

    # True for episodes reconstructed from historical logs (ticket 08). Usable
    # for pattern mining and attribution-rule tuning, never for replay.
    backfilled = Column(Boolean, default=False, nullable=False)

    outcome = Column(JSONType)
    quality_score = Column(Float)
    cost_usd = Column(Float)
    latency_ms = Column(Integer)
    risk_result = Column(String(32))

    # Governs whether this episode may feed cross-tenant / global learning.
    privacy_class = Column(String(16), default="tenant", nullable=False)

    event_count = Column(Integer, default=0, nullable=False)
    started_at = Column(TIMESTAMP(timezone=True))
    completed_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # One run yields exactly one episode; the assembler is idempotent and
        # this constraint is what enforces it under concurrency.
        UniqueConstraint("message_id", name="uq_evolution_episodes_message"),
        CheckConstraint(
            "privacy_class IN ('public', 'tenant', 'private')",
            name="evolution_episodes_privacy_check",
        ),
        Index("idx_evolution_episodes_tenant_task", "tenant_id", "task_type", "completed_at"),
        Index("idx_evolution_episodes_user", "user_id", "created_at"),
        Index("idx_evolution_episodes_chat", "chat_id"),
        Index("idx_evolution_episodes_bundle", "asset_bundle_id"),
        Index("idx_evolution_episodes_created", "created_at"),
    )


class EvolutionTraceEvent(Base):
    """Append-only observation stream, joined to an episode by message id."""

    __tablename__ = "evolution_trace_events"

    event_id = Column(String(64), primary_key=True)
    episode_id = Column(String(64), index=True)
    message_id = Column(String(64), nullable=False)
    run_id = Column(String(64))
    chat_id = Column(String(64))
    user_id = Column(String(64))

    seq = Column(Integer, nullable=False, default=0)
    event_type = Column(String(48), nullable=False)
    actor_type = Column(String(16), default="system")
    actor_id = Column(String(128), default="")

    asset_kind = Column(String(16), default="")
    asset_version_id = Column(String(128), default="")

    payload = Column(JSONType)
    # Set when the payload was a result body rather than evidence; the content
    # itself stays in whichever table already owns it.
    payload_ref = Column(String(256), default="")

    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("message_id", "seq", name="uq_evolution_events_msg_seq"),
        Index("idx_evolution_events_episode_seq", "episode_id", "seq"),
        Index("idx_evolution_events_type", "event_type", "created_at"),
        Index("idx_evolution_events_created", "created_at"),
    )


class EvolutionCandidate(Base):
    """A proposed change, in the shared IR envelope.

    Candidates are proposals and nothing more. Only the release controller may
    move a version pointer, and it will not look at a candidate that has not
    cleared the verification its risk tier demands.
    """

    __tablename__ = "evolution_candidates"

    candidate_id = Column(String(64), primary_key=True)
    target_kind = Column(String(16), nullable=False)
    target_asset_id = Column(String(128), nullable=False)
    base_version_id = Column(String(128))
    operation = Column(String(32), nullable=False)

    ir = Column(JSONType, nullable=False)
    hypothesis = Column(Text, default="")
    # Content hash of the change; de-duplicates repeated evidence proposing the
    # same edit so the review queue does not fill with copies.
    change_checksum = Column(String(64), nullable=False)

    evidence_refs = Column(JSONType, default=list)
    credit_decision = Column(JSONType)
    risk_tier = Column(String(16), default="medium", nullable=False)
    scope = Column(JSONType, default=dict)

    status = Column(String(24), nullable=False, default="draft")
    reject_reason = Column(Text)
    # Who proposed it. Recorded so the "a generator never approves its own
    # output" rule can be enforced rather than merely asserted.
    proposer = Column(String(64), default="system")
    approved_by = Column(String(64))
    approved_at = Column(TIMESTAMP(timezone=True))

    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "target_kind",
            "target_asset_id",
            "base_version_id",
            "change_checksum",
            name="uq_evolution_candidates_dedup",
        ),
        CheckConstraint(
            "status IN ('draft','replay_passed','shadow','canary','active','retired',"
            "'rejected','rolled_back')",
            name="evolution_candidates_status_check",
        ),
        CheckConstraint(
            "risk_tier IN ('low','medium','high')",
            name="evolution_candidates_risk_check",
        ),
        Index("idx_evolution_candidates_status", "status", "target_kind", "created_at"),
        Index("idx_evolution_candidates_asset", "target_kind", "target_asset_id"),
    )


class EvolutionRelease(Base):
    """The release record for one candidate.

    At most one release may be in flight per asset — concurrent releases on the
    same asset would make any measured effect impossible to attribute, and would
    leave rollback with no single target to return to.
    """

    __tablename__ = "evolution_releases"

    release_id = Column(String(64), primary_key=True)
    candidate_id = Column(String(64), nullable=False, index=True)
    target_kind = Column(String(16), nullable=False)
    target_asset_id = Column(String(128), nullable=False)

    stage = Column(String(24), nullable=False, default="draft")
    traffic_percent = Column(Integer, default=0, nullable=False)
    scope_filter = Column(JSONType, default=dict)
    guardrails = Column(JSONType, default=dict)

    # Recorded at promotion time, not derived at rollback time: the whole point
    # is to know where to return to before anything goes wrong.
    rollback_version_id = Column(String(128))
    rolled_back_at = Column(TIMESTAMP(timezone=True))
    rollback_reason = Column(Text)
    # Distinguishes "the guardrails worked" from "the evaluation missed it" —
    # collapsing the two hides evaluation quality problems.
    rollback_kind = Column(String(16))

    approved_by = Column(String(64))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "stage IN ('draft','replay_passed','shadow','canary','active','retired',"
            "'rejected','rolled_back')",
            name="evolution_releases_stage_check",
        ),
        CheckConstraint(
            "traffic_percent >= 0 AND traffic_percent <= 100",
            name="evolution_releases_traffic_check",
        ),
        CheckConstraint(
            "rollback_kind IS NULL OR rollback_kind IN ('guardrail','manual')",
            name="evolution_releases_rollback_kind_check",
        ),
        Index("idx_evolution_releases_stage", "stage", "updated_at"),
        Index("idx_evolution_releases_asset", "target_kind", "target_asset_id", "stage"),
    )


class EvolutionEvaluation(Base):
    """One evaluation run against a candidate (replay / counterfactual / shadow)."""

    __tablename__ = "evolution_evaluations"

    evaluation_id = Column(String(64), primary_key=True)
    candidate_id = Column(String(64), nullable=False, index=True)
    eval_type = Column(String(24), nullable=False)

    # Frozen so a conclusion stays reproducible; a dataset edited after the fact
    # invalidates every result computed from it.
    dataset_snapshot = Column(JSONType)
    baseline_metrics = Column(JSONType)
    candidate_metrics = Column(JSONType)
    guardrails = Column(JSONType)
    verdict = Column(String(16))
    # Paired statistics: the same tasks under both arms, which is what makes a
    # few-percentage-point difference detectable at these sample sizes.
    effect_size = Column(Float)
    p_value = Column(Float)
    sample_size = Column(Integer)

    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_evolution_evaluations_candidate", "candidate_id", "eval_type", "created_at"),
    )


class EvolutionAgentProfile(Base):
    """A published orchestration profile — see :mod:`core.evolution.agent_profile`.

    Stored as one JSON payload rather than a column per knob. The profile's
    shape is itself under evolution (a new intervention signal, a new routing
    dimension), and a schema migration per knob would make the asset harder to
    change than the behaviour it governs. Resolution reads whole rows and
    validates them in Python, so an unreadable payload is rejected rather than
    partially applied.
    """

    __tablename__ = "evolution_agent_profiles"

    profile_id = Column(String(128), primary_key=True)
    version = Column(String(32), nullable=False, default="v1")
    # Denormalised out of the payload purely so resolution can filter in SQL.
    task_types = Column(JSONType, default=list)
    scope = Column(JSONType, default=dict)

    payload = Column(JSONType, nullable=False)
    # The candidate this profile was materialised from, so "why does this
    # profile exist?" has an answer and a rollback has a target.
    candidate_id = Column(String(64), index=True)
    is_active = Column(Boolean, default=False, nullable=False)

    created_by = Column(String(64), default="evolution")
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_evolution_profiles_active", "is_active", "updated_at"),
    )


class EvolutionMemoryOp(Base):
    """One applied change to the memory store, with its undo.

    Memory operations are the only part of evolution that mutates a store the
    user thinks of as *theirs*, so every applied op keeps the previous state
    inline. Recomputing "what it was before" from a later snapshot is not good
    enough: by then the store may have merged or renumbered the row, and an undo
    that cannot find its target is not an undo.
    """

    __tablename__ = "evolution_memory_ops"

    op_id = Column(String(64), primary_key=True)
    candidate_id = Column(String(64), index=True)
    user_id = Column(String(64), index=True)
    workspace_id = Column(String(64), default="default")

    operation = Column(String(24), nullable=False)
    # Content-derived, so it survives the store renumbering its own ids.
    memory_ref = Column(String(64), nullable=False)
    external_id = Column(String(128))

    before_state = Column(JSONType)
    after_state = Column(JSONType)
    reason = Column(Text, default="")

    status = Column(String(24), nullable=False, default="applied")
    applied_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    reverted_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("idx_evolution_memory_ops_user", "user_id", "status", "applied_at"),
    )
