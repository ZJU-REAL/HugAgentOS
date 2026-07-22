"""SQLAlchemy ORM models — knowledge base / capability catalog."""

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


class KBSpace(Base):
    """Knowledge base space table."""

    __tablename__ = "kb_spaces"

    kb_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    description = Column(Text)
    document_count = Column(Integer, default=0)
    total_size_bytes = Column(BigInteger, default=0)
    visibility = Column(String(16), nullable=False, default="private")
    chunk_method = Column(String(32), nullable=False, default="semantic")
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

    # Relationships
    user = relationship("UserShadow", back_populates="kb_spaces")
    documents = relationship("KBDocument", back_populates="kb_space", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("length(name) >= 1 AND length(name) <= 255", name="kb_spaces_name_length"),
        CheckConstraint("document_count >= 0", name="kb_spaces_document_count_check"),
        CheckConstraint("total_size_bytes >= 0", name="kb_spaces_total_size_check"),
        CheckConstraint(
            "visibility IN ('public', 'private', 'scoped')", name="kb_spaces_visibility_check"
        ),
        Index("idx_kb_spaces_user_id", "user_id"),
        Index("idx_kb_spaces_updated_at", "updated_at"),
        Index(
            "idx_kb_spaces_deleted", "deleted_at", postgresql_where=Column("deleted_at").isnot(None)
        ),
        Index("idx_kb_spaces_visibility", "visibility"),
    )


class KBDocument(Base):
    """Knowledge base document table."""

    __tablename__ = "kb_documents"

    document_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=False)
    filename = Column(String(500), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    mime_type = Column(String(100), nullable=False)
    storage_key = Column(Text, nullable=False)
    storage_url = Column(Text)
    checksum = Column(String(64))
    indexing_status = Column(
        String(20), nullable=False, default="processing"
    )  # processing | completed | failed
    extra_data = Column("metadata", JSONType, default={})
    uploaded_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

    # Relationships
    kb_space = relationship("KBSpace", back_populates="documents")
    chunks = relationship("KBChunk", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="kb_documents_size_check"),
        CheckConstraint("length(filename) >= 1", name="kb_documents_filename_length"),
        Index("idx_kb_documents_kb_id", "kb_id"),
        Index("idx_kb_documents_uploaded_at", "uploaded_at"),
        Index("idx_kb_documents_kb_uploaded", "kb_id", "uploaded_at"),
        Index(
            "idx_kb_documents_deleted",
            "deleted_at",
            postgresql_where=Column("deleted_at").isnot(None),
        ),
        Index("idx_kb_documents_metadata_gin", "metadata", postgresql_using="gin"),
    )


class KBChunk(Base):
    """Knowledge base chunk table - stores parent chunks for context retrieval.

    Each document is split into parent chunks (stored here) and child chunks
    (vectorised in Milvus hugagent_kb_private collection). Retrieval finds child
    chunks via vector search, then fetches the parent content from this table.
    """

    __tablename__ = "kb_chunks"

    chunk_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    document_id = Column(
        String(64), ForeignKey("kb_documents.document_id", ondelete="CASCADE"), nullable=False
    )
    chunk_index = Column(Integer, nullable=False)
    content = Column(
        Text, nullable=False
    )  # parent chunk original text, returned to the LLM on retrieval hit
    tags = Column(JSONType, default=list)  # tag list ["数字化转型", "申报条件"]
    questions = Column(JSONType, default=list)  # associated question list (array of strings)
    char_start = Column(Integer)
    char_end = Column(Integer)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    document = relationship("KBDocument", back_populates="chunks")

    __table_args__ = (
        Index("idx_kb_chunks_kb_id", "kb_id"),
        Index("idx_kb_chunks_document_id", "document_id"),
        Index("idx_kb_chunks_kb_doc", "kb_id", "document_id"),
    )


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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

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
