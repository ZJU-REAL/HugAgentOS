"""SQLAlchemy ORM models — Projects."""

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


class Project(Base):
    """Project (personal / team) — Claude-style workspace.

    Personal project: ``kind='personal'`` + ``owner_user_id`` is the owner.
    Team project: ``kind='team'`` + ``team_id`` is the owning team; ``owner_user_id``
    records the creator. Team project visibility / write permission follows
    ``TeamMember.role`` + ``file_permission`` (owner/admin are always admin; member is
    two-tiered editor/viewer per file_permission).
    """
    __tablename__ = "projects"

    project_id       = Column(String(64), primary_key=True)
    name             = Column(String(120), nullable=False)
    description      = Column(Text)
    kind             = Column(String(16), nullable=False)  # 'personal' | 'team'
    owner_user_id    = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False)
    team_id          = Column(String(64), ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=True)
    # Project ↔ folder strict 1:1 linkage (mutually exclusive):
    # - personal project: linked_folder_id points to user_folders (personal folder)
    # - team project: linked_team_folder_id points to team_folders (team folder)
    # The service layer guarantees only one is non-null on write, and that it matches kind.
    linked_folder_id      = Column(String(64), ForeignKey("user_folders.folder_id", ondelete="SET NULL"), nullable=True)
    linked_team_folder_id = Column(String(64), ForeignKey("team_folders.folder_id", ondelete="SET NULL"), nullable=True)
    instructions     = Column(Text)
    icon_color       = Column(String(20))
    pinned           = Column(Boolean, nullable=False, default=False)
    extra_data       = Column("metadata", JSONType, default={})
    created_at       = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at       = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    last_activity_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    deleted_at       = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(kind = 'personal' AND team_id IS NULL) OR (kind = 'team' AND team_id IS NOT NULL)",
            name="ck_projects_kind_team",
        ),
        CheckConstraint("kind IN ('personal','team')", name="ck_projects_kind_enum"),
        CheckConstraint("length(name) >= 1 AND length(name) <= 120", name="ck_projects_name_length"),
        Index("idx_projects_owner", "owner_user_id"),
        Index("idx_projects_team", "team_id"),
        Index("idx_projects_last_activity", "last_activity_at"),
        Index("idx_projects_linked_user_folder", "linked_folder_id"),
        Index("idx_projects_linked_team_folder", "linked_team_folder_id"),
    )


class ProjectFavorite(Base):
    """Per-user independent star (does not affect others' view of the project)."""
    __tablename__ = "project_favorites"

    project_id = Column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"), primary_key=True)
    user_id    = Column(String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("idx_project_favorites_user", "user_id"),
    )
