"""SQLAlchemy ORM models — model / system configuration."""

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
    role_assignments = relationship("ModelRoleAssignment", back_populates="provider", cascade="all, delete-orphan")

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

    __table_args__ = (
        Index("idx_system_configs_group_key", "group_key"),
    )


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

    __table_args__ = (
        Index("idx_model_role_assignments_provider", "provider_id"),
    )


class ModelPricing(Base):
    """Model pricing configuration for token billing."""
    __tablename__ = "model_pricing"

    pricing_id   = Column(String(64), primary_key=True)
    model_name   = Column(String(255), nullable=False, unique=True)
    display_name = Column(String(255))
    input_price  = Column(Numeric(12, 6), nullable=False, default=0)
    output_price = Column(Numeric(12, 6), nullable=False, default=0)
    currency     = Column(String(10), nullable=False, default="CNY")
    is_active    = Column(Boolean, default=True, nullable=False)
    created_at   = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at   = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_model_pricing_model_name", "model_name"),
        Index("idx_model_pricing_active", "is_active"),
    )


class GatewayVirtualKey(Base):
    """**Mirror** of the outbound model gateway's virtual keys (EE-only).

    The source of truth is LiteLLM Proxy's own DB (the key secret + budget/rate-limit
    enforcement all live there); this table stores displayable/auditable metadata:
    - ``litellm_token``: the hashed token returned by LiteLLM ``/key/generate``, used as the
      management handle for subsequent block/unblock/delete/update (not plaintext, safe to persist).
    - ``litellm_key_enc``: the **encrypted** ciphertext (Fernet) of the plaintext ``sk-...``, for
      admins to copy the plaintext afterwards; decryption only via the ADMIN-authenticated reveal
      endpoint (``core/infra/crypto.py``). The LiteLLM side stores only the hash and cannot recover
      the plaintext, so this table carries the encryption. This column is empty for pre-existing legacy keys.
    When the license lapses (DEAD_MODES), the control plane batch-calls LiteLLM ``/key/block`` per
    this table to ban keys, unbanning on renewal — see ``core/services/litellm_gateway_service.py``.
    """
    __tablename__ = "gateway_virtual_keys"

    key_id        = Column(String(64), primary_key=True)
    key_alias     = Column(String(255), nullable=False, unique=True)
    litellm_token = Column(Text)  # hashed token management handle; may be empty on issuance failure / for historical data
    # **Encrypted** ciphertext (Fernet, core/infra/crypto.py) of the plaintext sk- key. Stored only to let admins copy the plaintext afterwards;
    # written on issuance, decrypted and returned on reveal. Empty for pre-existing legacy keys (no plaintext to copy, can only re-issue).
    litellm_key_enc = Column(Text)
    display_name  = Column(String(255), nullable=False)
    owner         = Column(String(255))  # issuance target (third-party / external system name), free text
    allowed_models = Column("allowed_models", JSONType, default=list)  # model alias whitelist
    max_budget    = Column(Numeric(12, 4))  # budget cap (currency unit), NULL = unlimited
    tpm_limit     = Column(Integer)  # tokens/min, NULL = unlimited
    rpm_limit     = Column(Integer)  # requests/min, NULL = unlimited
    # active / blocked / license_blocked (auto-banned on license lapse, distinct from manual blocked)
    status        = Column(String(20), nullable=False, default="active")
    created_by    = Column(String(64))
    deleted_at    = Column(TIMESTAMP(timezone=True))
    created_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'blocked', 'license_blocked')",
            name="gateway_virtual_keys_status_check",
        ),
        Index("idx_gateway_virtual_keys_status", "status"),
        Index("idx_gateway_virtual_keys_alias", "key_alias"),
    )
