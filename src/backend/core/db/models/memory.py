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
    last_compacted_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (Index("idx_profile_memory_updated_at", "updated_at"),)


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
