"""SQLAlchemy ORM models — Projects."""

from datetime import datetime, timezone

from core.db.engine import Base
from core.db.model_extensions import ProjectEditionFields, project_edition_table_args
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


class Project(ProjectEditionFields, Base):
    """Project workspace; edition extensions add optional organization scope."""

    __tablename__ = "projects"

    project_id = Column(String(64), primary_key=True)
    name = Column(String(120), nullable=False)
    description = Column(Text)
    kind = Column(String(16), nullable=False)
    owner_user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False
    )
    linked_folder_id = Column(
        String(64), ForeignKey("user_folders.folder_id", ondelete="SET NULL"), nullable=True
    )
    instructions = Column(Text)
    icon_color = Column(String(20))
    pinned = Column(Boolean, nullable=False, default=False)
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    last_activity_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        *project_edition_table_args(),
        CheckConstraint(
            "length(name) >= 1 AND length(name) <= 120", name="ck_projects_name_length"
        ),
        Index("idx_projects_owner", "owner_user_id"),
        Index("idx_projects_last_activity", "last_activity_at"),
        Index("idx_projects_linked_user_folder", "linked_folder_id"),
    )


class ProjectFavorite(Base):
    """Per-user independent star (does not affect others' view of the project)."""

    __tablename__ = "project_favorites"

    project_id = Column(
        String(64), ForeignKey("projects.project_id", ondelete="CASCADE"), primary_key=True
    )
    user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), primary_key=True
    )
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    __table_args__ = (Index("idx_project_favorites_user", "user_id"),)
