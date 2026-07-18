"""SQLAlchemy ORM models — artifacts / content blocks."""

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


class Artifact(Base):
    """Artifact table - AI-generated files (reports, charts, etc.)."""
    __tablename__ = "artifacts"

    artifact_id = Column(String(64), primary_key=True)
    chat_id = Column(String(64), ForeignKey("chat_sessions.chat_id", ondelete="SET NULL"))
    user_id = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    # Team ownership (NULL = personal file; non-NULL means a team file, which can be further located to a specific folder via team_folder_id)
    team_id = Column(String(64), ForeignKey("teams.team_id", ondelete="SET NULL"), nullable=True)
    team_folder_id = Column(
        String(64),
        ForeignKey("team_folders.folder_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Personal folder ownership (used only when team_id is NULL, i.e. personal files; mutually exclusive with team_folder_id, guaranteed by the service layer)
    user_folder_id = Column(
        String(64),
        ForeignKey("user_folders.folder_id", ondelete="SET NULL"),
        nullable=True,
    )
    type = Column(String(50), nullable=False)
    title = Column(String(500), nullable=False)
    filename = Column(String(500), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    mime_type = Column(String(100), nullable=False)
    storage_key = Column(Text, nullable=False)
    storage_url = Column(Text)
    extra_data = Column("metadata", JSONType, default={})
    # Lazy caches for cross-turn file reading (populated by core/content/artifact_reader.py)
    parsed_text = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    parsed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    parse_error = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

    # Relationships
    user = relationship("UserShadow", back_populates="artifacts")
    session = relationship("ChatSession", back_populates="artifacts")

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="artifacts_size_check"),
        CheckConstraint("type IN ('report', 'chart', 'document', 'code', 'other')", name="artifacts_type_check"),
        Index("idx_artifacts_user_id", "user_id"),
        Index("idx_artifacts_chat_id", "chat_id"),
        Index("idx_artifacts_type", "type"),
        Index("idx_artifacts_created_at", "created_at"),
        Index("idx_artifacts_user_created", "user_id", "created_at"),
        Index("idx_artifacts_deleted", "deleted_at", postgresql_where=Column("deleted_at").isnot(None)),
        Index("idx_artifacts_team_folder", "team_id", "team_folder_id", "created_at"),
        Index("idx_artifacts_team_folder_id", "team_folder_id"),
        Index("idx_artifacts_user_folder", "user_id", "user_folder_id", "created_at"),
        Index("idx_artifacts_user_folder_id", "user_folder_id"),
    )


class ContentBlock(Base):
    """Content blocks for editable frontend sections (feature updates / capability center)."""
    __tablename__ = "content_blocks"

    id = Column(String(64), primary_key=True)          # e.g. 'docs_updates', 'docs_capabilities'
    payload = Column(JSONType, nullable=False, default=[])
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64), nullable=True)
