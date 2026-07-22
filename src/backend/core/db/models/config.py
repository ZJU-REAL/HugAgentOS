"""SQLAlchemy ORM models — model / system configuration."""

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


class ModelProvider(Base):
    """Model provider — an OpenAI-compatible model endpoint."""

    __tablename__ = "model_providers"

    provider_id = Column(String(64), primary_key=True)
    display_name = Column(String(255), nullable=False)
    provider_type = Column(String(20), nullable=False)  # chat / embedding / reranker
    # Vendor / protocol (see core/llm/providers/registry.py). No CHECK constraint: vendors keep
    # being added; validity is checked by the application-layer registry, avoiding a migration to
    # change the constraint for every new vendor.
    provider = Column(String(32), nullable=False, server_default="openai_compatible")
    base_url = Column(Text, nullable=False)
    api_key = Column(Text, nullable=False)
    model_name = Column(String(255), nullable=False)
    extra_config = Column(JSONType, default={})
    # Outbound-gateway "model group": when multiple providers set the same gateway_group, they are
    # synced into the same LiteLLM model_name (= group alias) as multiple deployments, activating
    # load balancing (routing_strategy) and failover.
    # Empty (NULL) = keep the display_name single-upstream 1:1 registration (backward compatible, old behavior unchanged).
    gateway_group = Column(String(255))
    # In-pool weight (simple-shuffle does weighted round-robin by weight); priority reserves primary/backup semantics (lower value first), not yet used in dispatch.
    weight = Column(Integer, nullable=False, server_default="1")
    priority = Column(Integer, nullable=False, server_default="0")
    is_active = Column(Boolean, default=True, nullable=False)
    last_tested_at = Column(TIMESTAMP(timezone=True))
    last_test_status = Column(String(20))  # success / failure / null
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    role_assignments = relationship(
        "ModelRoleAssignment", back_populates="provider", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('chat', 'embedding', 'reranker')",
            name="model_providers_type_check",
        ),
        CheckConstraint(
            "last_test_status IS NULL OR last_test_status IN ('success', 'failure')",
            name="model_providers_test_status_check",
        ),
        Index("idx_model_providers_type", "provider_type"),
        Index("idx_model_providers_active", "is_active"),
        Index("idx_model_providers_provider", "provider"),
        Index("idx_model_providers_gateway_group", "gateway_group"),
    )


class SystemConfig(Base):
    """Key-value store for external service configurations (DB query, KB, industry, file parser)."""

    __tablename__ = "system_configs"

    config_key = Column(String(100), primary_key=True)
    config_value = Column(Text)
    display_name = Column(String(255), nullable=False)
    description = Column(Text)
    group_key = Column(String(50), nullable=False)
    is_secret = Column(Boolean, default=False, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64))

    __table_args__ = (Index("idx_system_configs_group_key", "group_key"),)


class ModelRoleAssignment(Base):
    """Role → provider mapping.  Each role_key can have at most one provider."""

    __tablename__ = "model_role_assignments"

    role_key = Column(String(50), primary_key=True)
    provider_id = Column(
        String(64),
        ForeignKey("model_providers.provider_id", ondelete="CASCADE"),
        nullable=False,
    )
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64))

    # Relationships
    provider = relationship("ModelProvider", back_populates="role_assignments")

    __table_args__ = (Index("idx_model_role_assignments_provider", "provider_id"),)
