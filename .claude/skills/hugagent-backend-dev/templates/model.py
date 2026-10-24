"""ORM model template.

Replace ${Feature}, ${feature}, ${table_name} with actual names.

Where it goes:
  - Feature module  → features/${feature}/models.py   (loaded via INSTALLED_FEATURES by app and alembic)
  - Core domain     → core/db/models/<domain>.py and re-export in core/db/models/__init__.py
  - EE-only table   → edition_ee/db/models/… and add the table name to edition_ee/db/edition_tables.py::EE_ONLY_TABLES

Conventions enforced by tests (tests/test_utc_conventions.py, tests/test_db_import_order.py):
  - Timestamps: UTCDateTime(timezone=True) + default=utc_now (never datetime.utcnow / naive datetimes)
  - JSON: JSONType (JSON on SQLite, JSONB on PostgreSQL) — models must work on both
  - Soft delete via deleted_at; FKs declare ondelete
  - Model modules import only core.db / core.infra leaves, never services or routes
"""

from core.db.engine import Base
from core.db.models.column_types import JSONType
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text


class ${Feature}(Base):
    """${Feature} — one sentence on what a row represents and who owns it."""

    __tablename__ = "${table_name}"

    id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Optional link that must survive parent deletion → SET NULL
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
    )

    name = Column(String(200), nullable=False)
    description = Column(Text)
    position = Column(Integer, nullable=False, default=0)
    is_enabled = Column(Boolean, nullable=False, default=True)

    # Python attribute extra_data, DB column "metadata" (metadata is reserved on declarative models)
    extra_data = Column("metadata", JSONType, default=dict)

    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(UTCDateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("position >= 0", name="${table_name}_position_check"),
        Index("idx_${table_name}_user_id", "user_id"),
        Index("idx_${table_name}_updated_at", "updated_at"),
    )
