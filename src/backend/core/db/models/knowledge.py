"""SQLAlchemy ORM models — knowledge base / capability catalog."""
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now

from datetime import datetime, timezone

from core.db.engine import Base
from sqlalchemy import (
    JSON,
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














class CatalogOverride(Base):
    """Catalog override table - user customizations for skills/agents/MCPs."""

    __tablename__ = "catalog_overrides"

    override_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(String(20), nullable=False)
    item_id = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    config_data = Column("config", JSONType, default={})
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    # Relationships
    user = relationship("UserShadow", back_populates="catalog_overrides")

    __table_args__ = (
        CheckConstraint("kind IN ('skill', 'agent', 'mcp')", name="catalog_overrides_kind_check"),
        UniqueConstraint(
            "user_id", "kind", "item_id", name="catalog_overrides_unique_user_kind_item"
        ),
        Index("idx_catalog_overrides_user_id", "user_id"),
        Index("idx_catalog_overrides_kind", "kind", "enabled"),
    )
