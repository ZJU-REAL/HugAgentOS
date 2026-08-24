"""SQLAlchemy ORM models — memory."""

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


class ProfileMemory(Base):
    """L1 Profile record memory (bounded markdown, frozen and injected at session start).

    Primary key is (user_id, workspace_id) — the same natural person has isolated memory across workspaces.
    """

    __tablename__ = "profile_memory"

    user_id = Column(String(64), primary_key=True)
    workspace_id = Column(String(64), primary_key=True, default="default")
    content_md = Column(Text, nullable=False, default="")
    revision = Column(Integer, nullable=False, default=0)
    effect_receipts = Column(JSONType, nullable=False, default=dict)
    last_compacted_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (Index("idx_profile_memory_updated_at", "updated_at"),)


class MemoryOutbox(Base):
    """Durable state machine for post-response memory effects.

    The unique tuple is the admission idempotency boundary: replaying the same
    message, layer and normalized candidate resolves to this row.  External
    L2/L3 writers additionally persist this row id as an effect receipt so a
    crash after the remote effect but before acknowledgement does not apply it
    twice.
    """

    __tablename__ = "memory_outbox"

    id = Column(String(64), primary_key=True)
    parent_id = Column(String(64))
    message_id = Column(String(128), nullable=False)
    scope_key = Column(String(64))
    job_kind = Column(String(32), nullable=False)
    layer = Column(String(32), nullable=False)
    candidate_hash = Column(String(64), nullable=False)
    payload_json = Column(JSONType, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(128))
    lease_expires_at = Column(TIMESTAMP(timezone=True))
    next_attempt_at = Column(TIMESTAMP(timezone=True))
    last_error = Column(Text)
    result_json = Column(JSONType)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    completed_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "layer",
            "candidate_hash",
            name="uq_memory_outbox_candidate",
        ),
        CheckConstraint(
            "status IN ('pending','processing','succeeded','retry','quarantined')",
            name="memory_outbox_status_check",
        ),
        Index("idx_memory_outbox_due", "status", "next_attempt_at", "created_at"),
        Index("idx_memory_outbox_lease", "status", "lease_expires_at"),
        Index("idx_memory_outbox_message", "message_id", "created_at"),
        Index(
            "idx_memory_outbox_scope_lane",
            "scope_key",
            "layer",
            "status",
            "created_at",
        ),
    )


class MemoryRefShadow(Base):
    """Stable local identity for memories whose real home is an external store.

    L2/L3 memories live in the vector / graph store, and those stores rewrite
    their own ids whenever a memory is merged, split or updated.  That is fine
    for retrieval but fatal for attribution: months later we still need to
    answer "which memory was injected into that run", and the id recorded at
    the time may no longer resolve.

    This table pins a content-derived ``ref_id`` that survives id churn, so an
    Episode can reference a memory by something that stays valid.  It stores a
    hash plus a short sanitized preview — never the full memory text, which
    already lives in the memory store and would only widen the privacy surface
    if duplicated here.
    """

    __tablename__ = "memory_ref_shadow"

    # Derived from (layer, user, workspace, content_hash) — deterministic, so the
    # same content always resolves to the same ref no matter how often the
    # external store renumbers it.
    ref_id = Column(String(64), primary_key=True)
    layer = Column(String(16), nullable=False)
    user_id = Column(String(64), nullable=False)
    workspace_id = Column(String(64), nullable=False, default="default")
    content_hash = Column(String(64), nullable=False)
    # Most recently observed external id; informational only, never a join key.
    external_id = Column(String(128))
    content_preview = Column(String(200), default="")
    first_seen_episode_id = Column(String(64))
    first_seen_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    last_seen_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    seen_count = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "layer IN ('profile', 'fact', 'graph')", name="memory_ref_shadow_layer_check"
        ),
        Index("idx_memory_ref_shadow_user", "user_id", "workspace_id"),
        Index("idx_memory_ref_shadow_hash", "content_hash"),
        Index("idx_memory_ref_shadow_external", "external_id"),
        Index("idx_memory_ref_shadow_last_seen", "last_seen_at"),
    )


class MemorySanitizerRule(Base):
    """Sensitive-word rules appended / disabled at runtime (defaults are hardcoded in memory_sanitizer.py)."""

    __tablename__ = "memory_sanitizer_rules"

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    rule_type = Column(
        String(32), nullable=False
    )  # redact | classified | disable_redact | disable_classified
    name = Column(String(64))  # redact rule name; used as target name when disable_redact
    pattern = Column(Text, nullable=False)  # redact regex or classified word
    description = Column(Text)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    created_by = Column(String(64))

    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('redact','classified','disable_redact','disable_classified')",
            name="memory_sanitizer_rules_type_check",
        ),
        Index("idx_memory_sanitizer_rules_enabled", "enabled"),
    )
