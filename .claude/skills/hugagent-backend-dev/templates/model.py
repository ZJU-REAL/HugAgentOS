"""ORM model template.

Replace ${Feature}, ${feature}, ${table_name} with actual names.
Copy relevant parts into core/db/models/<领域>.py (并在包 __init__.py re-export).
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Boolean, Text, TIMESTAMP,
    ForeignKey, CheckConstraint, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from core.db.engine import Base


class ${Feature}(Base):
    """${Feature} table."""
    __tablename__ = "${table_name}"

    # --- Primary key ---
    id = Column(String(64), primary_key=True)

    # --- Foreign key (to users_shadow) ---
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- Business fields ---
    name = Column(String(200), nullable=False)
    description = Column(Text)
    status = Column(String(20), nullable=False, default="active")
    count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    # --- Flexible JSONB ---
    extra_data = Column("metadata", JSONB, default={})

    # --- Soft delete ---
    deleted_at = Column(TIMESTAMP(timezone=True))

    # --- Timestamps (required) ---
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # --- Relationships ---
    user = relationship("UserShadow", back_populates="${feature}s")

    # --- Indexes & Constraints ---
    __table_args__ = (
        CheckConstraint("count >= 0", name="${table_name}_count_check"),
        Index("idx_${table_name}_user_id", "user_id"),
        Index("idx_${table_name}_updated_at", "updated_at"),
        Index(
            "idx_${table_name}_user_updated",
            "user_id", "updated_at",
        ),
        Index(
            "idx_${table_name}_deleted",
            "deleted_at",
            postgresql_where=Column("deleted_at").isnot(None),
        ),
        Index(
            "idx_${table_name}_metadata_gin",
            "metadata",
            postgresql_using="gin",
        ),
    )
