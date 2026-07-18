"""SQLAlchemy ORM models — site hosting (build sites in chat, hosted by the platform at /site/<slug>/)."""

from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, BigInteger, Text, TIMESTAMP,
    ForeignKey, CheckConstraint, Index, JSON, PrimaryKeyConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from core.db.engine import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Site(Base):
    """User site — a static website generated in chat, files stored under ``sites/<site_id>/v<version>/``.

    slug is globally unique (hosting URL is ``/site/<slug>/``); on soft delete the
    service rewrites the slug to ``<slug>--del-<ts>`` to release the original address.
    Version directories are immutable; publishing a new version only increments
    ``current_version``, and the history list is recorded in ``metadata.versions``.
    """

    __tablename__ = "sites"

    site_id = Column(String(64), primary_key=True)
    slug = Column(String(80), nullable=False, unique=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    chat_id = Column(
        String(64),
        ForeignKey("chat_sessions.chat_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Authorization scope when visibility=team: members of this team can access
    team_id = Column(
        String(64),
        ForeignKey("teams.team_id", ondelete="SET NULL"),
        nullable=True,
    )
    # The source-code project (personal project) this site corresponds to. Non-null → the
    # site can be "re-edited": building/editing both happen inside this project folder, and
    # publish_site takes files from the project folder. Old sites are empty (source only in
    # serve storage, unrecoverable) → not editable. On project deletion SET NULL, reverting
    # to not-editable but the hosted artifacts remain.
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
    )
    title = Column(String(200), nullable=False)
    description = Column(Text)
    # public=anyone with the link; private=owner only; team=members of team_id (all validated via session cookie)
    visibility = Column(String(16), nullable=False, default="public")
    entry_file = Column(String(200), nullable=False, default="index.html")
    current_version = Column(Integer, nullable=False, default=1)
    file_count = Column(Integer, nullable=False, default=0)
    total_size_bytes = Column(BigInteger, nullable=False, default=0)
    # HTML page view count (asset files not counted)
    view_count = Column(BigInteger, nullable=False, default=0)
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
    deleted_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "visibility IN ('public', 'private', 'team')", name="sites_visibility_check"
        ),
        CheckConstraint("current_version >= 1", name="sites_version_check"),
        Index("idx_sites_user_id", "user_id"),
        Index("idx_sites_team_id", "team_id"),
        Index("idx_sites_project_id", "project_id"),
        Index("idx_sites_updated_at", "updated_at"),
    )


class SiteKV(Base):
    """Site-level KV store (a minimal subset matching D1) — in-site JS reads/writes via /site/<slug>/__api/kv."""

    __tablename__ = "site_kv"

    site_id = Column(
        String(64), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False
    )
    k = Column(String(64), nullable=False)
    v = Column(Text, nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (PrimaryKeyConstraint("site_id", "k"),)


class SiteSubmission(Base):
    """Site form collection — in-site JS POSTs to /site/<slug>/__api/forms/<form_key>, exportable as an artifact."""

    __tablename__ = "site_submissions"

    id = Column(String(64), primary_key=True)
    site_id = Column(
        String(64), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False
    )
    form_key = Column(String(64), nullable=False)
    payload = Column(JSONType, nullable=False)
    client_ip = Column(String(45))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("idx_site_submissions_site_created", "site_id", "created_at"),
    )
