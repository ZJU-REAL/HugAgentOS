"""SQLAlchemy ORM models for ontology-governed agent execution."""

from datetime import datetime

from core.db.engine import Base
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

JSONType = JSON().with_variant(JSONB(), "postgresql")


class OntologyPack(Base):
    """A versioned Domain Pack and its currently active version pointer."""

    __tablename__ = "ontology_packs"

    pack_id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(128), nullable=False, default="general")
    description = Column(Text, nullable=False, default="")
    is_enabled = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    active_version_id = Column(String(64), nullable=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    deleted_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_ontology_packs_enabled", "is_enabled", "is_default"),
        Index("idx_ontology_packs_domain", "domain"),
        Index("idx_ontology_packs_deleted", "deleted_at"),
    )


class OntologyPackVersion(Base):
    """Domain Pack version; drafts are mutable, while published versions are immutable."""

    __tablename__ = "ontology_pack_versions"

    version_id = Column(String(64), primary_key=True)
    pack_id = Column(
        String(64),
        ForeignKey("ontology_packs.pack_id", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(String(32), nullable=False)
    content = Column(JSONType, nullable=False, default=dict)
    checksum = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="draft")
    validation_report = Column(JSONType, nullable=False, default=dict)
    created_by = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    activated_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ontology_pack_versions_status_check",
        ),
        UniqueConstraint("pack_id", "version", name="uq_ontology_pack_version"),
        Index("idx_ontology_pack_versions_pack", "pack_id", "created_at"),
        Index("idx_ontology_pack_versions_status", "status"),
        Index(
            "uq_ontology_pack_versions_working_draft",
            "pack_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),
    )


class OntologyEnforcementEvent(Base):
    """Append-only evidence emitted by the deterministic gate and reviewers."""

    __tablename__ = "ontology_enforcement_events"

    event_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="SET NULL"),
        nullable=True,
    )
    pack_id = Column(
        String(64),
        ForeignKey("ontology_packs.pack_id", ondelete="SET NULL"),
        nullable=True,
    )
    version_id = Column(
        String(64),
        ForeignKey("ontology_pack_versions.version_id", ondelete="SET NULL"),
        nullable=True,
    )
    rule_id = Column(String(128), nullable=True)
    stage = Column(String(24), nullable=False)
    event_type = Column(String(32), nullable=False)
    decision = Column(String(24), nullable=False)
    mode = Column(String(16), nullable=False, default="enforce")
    target = Column(String(255), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    details = Column(JSONType, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "stage IN ('tool', 'checkpoint', 'output', 'evolution', 'build')",
            name="ontology_enforcement_events_stage_check",
        ),
        CheckConstraint(
            "decision IN ('pass', 'log', 'deny', 'revise', 'escalate', 'error')",
            name="ontology_enforcement_events_decision_check",
        ),
        CheckConstraint(
            "mode IN ('log', 'enforce')",
            name="ontology_enforcement_events_mode_check",
        ),
        Index("idx_ontology_events_chat", "chat_id", "created_at"),
        Index("idx_ontology_events_rule", "rule_id", "created_at"),
        Index("idx_ontology_events_pack", "pack_id", "created_at"),
        Index("idx_ontology_events_decision", "decision", "created_at"),
    )


class OntologyReviewRun(Base):
    """Auditable L-b/L-c reviewer verdict with evidence and latency."""

    __tablename__ = "ontology_review_runs"

    review_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="SET NULL"),
        nullable=True,
    )
    pack_version_ids = Column(JSONType, nullable=False, default=list)
    level = Column(String(16), nullable=False)
    subject_type = Column(String(32), nullable=False, default="final_answer")
    verdict = Column(String(16), nullable=False)
    evidence = Column(JSONType, nullable=False, default=list)
    feedback = Column(Text, nullable=False, default="")
    reviewers = Column(JSONType, nullable=False, default=list)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "level IN ('checkpoint', 'committee')",
            name="ontology_review_runs_level_check",
        ),
        CheckConstraint(
            "verdict IN ('pass', 'revise', 'escalate', 'error')",
            name="ontology_review_runs_verdict_check",
        ),
        Index("idx_ontology_reviews_chat", "chat_id", "created_at"),
        Index("idx_ontology_reviews_verdict", "verdict", "created_at"),
    )


class OntologyDraft(Base):
    """Human-in-the-loop ontology evolution candidate; never auto-activates."""

    __tablename__ = "ontology_drafts"

    draft_id = Column(String(64), primary_key=True)
    pack_id = Column(
        String(64),
        ForeignKey("ontology_packs.pack_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type = Column(String(24), nullable=False)
    candidate_type = Column(String(24), nullable=False)
    proposal = Column(JSONType, nullable=False, default=dict)
    evidence = Column(JSONType, nullable=False, default=list)
    source_event_ids = Column(JSONType, nullable=False, default=list)
    value_score = Column(Integer, nullable=False, default=0)
    review_status = Column(String(16), nullable=False, default="pending")
    reviewer_id = Column(String(64), nullable=True)
    reviewer_comment = Column(Text, nullable=True)
    reviewed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('enforcement', 'review', 'user_correction', 'document')",
            name="ontology_drafts_source_type_check",
        ),
        CheckConstraint(
            "candidate_type IN ('term', 'relation', 'constraint', 'false_positive')",
            name="ontology_drafts_candidate_type_check",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ontology_drafts_status_check",
        ),
        CheckConstraint(
            "value_score >= 0 AND value_score <= 100",
            name="ontology_drafts_value_score_check",
        ),
        Index("idx_ontology_drafts_status", "review_status", "value_score", "created_at"),
        Index("idx_ontology_drafts_pack", "pack_id", "created_at"),
    )
