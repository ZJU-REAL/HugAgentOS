"""SQLAlchemy ORM models — identity / teams."""
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now


from core.db.engine import Base
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship
from core.db.models.column_types import JSONType


class UserShadow(Base):
    """User shadow table - synced from user center."""

    __tablename__ = "users_shadow"

    user_id = Column(String(64), primary_key=True)
    username = Column(String(255), nullable=False)
    email = Column(String(255))
    avatar_url = Column(Text)
    user_center_id = Column(String(64))
    extra_data = Column("metadata", JSONType, default={})  # Map to 'metadata' column in DB
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    last_sync_at = Column(UTCDateTime(timezone=True))

    # Relationships
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    catalog_overrides = relationship(
        "CatalogOverride", back_populates="user", cascade="all, delete-orphan"
    )
    artifacts = relationship("Artifact", back_populates="user", cascade="all, delete-orphan")
    user_agents = relationship(
        "UserAgent", foreign_keys="[UserAgent.user_id]", back_populates="user"
    )

    __table_args__ = (
        Index("idx_users_shadow_user_center_id", "user_center_id"),
        Index("idx_users_shadow_updated_at", "updated_at"),
        # 一个用户中心身份最多一行影子用户。缺了这条约束，"先查后建"在并发下会各建
        # 一行：桌面壳登录后一次性打出的多个桥接请求就足以造出重复身份，而会话、生成物
        # 等全部挂在 user_id 上——之后认到哪一行，就只看得见挂在那一行下的数据。
        # 局部索引：user_center_id 为空表示这行还没有绑定用户中心身份，彼此不冲突。
        Index(
            "uq_users_shadow_user_center_id",
            "user_center_id",
            unique=True,
            sqlite_where=text("user_center_id IS NOT NULL AND user_center_id <> ''"),
            postgresql_where=text("user_center_id IS NOT NULL AND user_center_id <> ''"),
        ),
    )


class LocalUser(Base):
    """Sensitive local-account info (password, status, contact details). 1:1 with users_shadow."""

    __tablename__ = "local_users"

    user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), primary_key=True
    )
    password_hash = Column(String(255), nullable=False)
    nickname = Column(String(64))
    real_name = Column(String(64))
    phone = Column(String(32))
    status = Column(String(20), nullable=False, default="active")
    invited_by_code = Column(String(32))
    password_updated_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled', 'pending')",
            name="local_users_status_check",
        ),
        Index("idx_local_users_status", "status"),
        Index("idx_local_users_phone", "phone"),
    )


class UserFolder(Base):
    """Personal folder — tree structure under My Space; NULL parent means the root directory.

    Constraints such as tree depth and name validation are enforced by UserFolderService
    (see core/projects/user_folders.py).
    """

    __tablename__ = "user_folders"

    folder_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_folder_id = Column(
        String(64),
        ForeignKey("user_folders.folder_id", ondelete="CASCADE"),
        nullable=True,
    )
    name = Column(String(255), nullable=False)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(UTCDateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "length(name) >= 1 AND length(name) <= 255",
            name="user_folders_name_length",
        ),
        CheckConstraint(
            "name NOT LIKE '%/%' AND name <> '.' AND name <> '..'",
            name="user_folders_name_safe",
        ),
        Index("idx_user_folders_user_parent", "user_id", "parent_folder_id"),
        Index("idx_user_folders_user_deleted", "user_id", "deleted_at"),
    )


class UserApiKey(Base):
    """Personal user API key: for invoking the agent over HTTP with the user's identity.

    Stores the key's SHA256 hash (for auth reverse lookup) and its prefix (for list display),
    plus a reversible ciphertext of the full key (``key_enc``, Fernet application-layer
    encryption) to support "copy again". The plaintext is returned exactly once at creation;
    list responses carry no plaintext — copying goes through the reveal endpoint, which
    decrypts on demand. Callers send ``Authorization: Bearer sk-jx-...``; the auth layer looks
    the key up by hash. A non-null agent_id restricts it to that agent's API
    sessions; project_id restricts it to a project's API sessions. Both null
    preserve the full-account personal-key contract.
    """

    __tablename__ = "user_api_keys"

    id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Immutable resource scope. No SET NULL FK: deleting an agent must never
    # promote its credential to an unrestricted personal key.
    agent_id = Column(String(64), nullable=True)
    project_id = Column(String(64), nullable=True)
    name = Column(String(128), nullable=False, default="API Key")
    key_prefix = Column(
        String(32), nullable=False
    )  # Plaintext prefix (e.g. sk-jx-a1b2c3), for list display
    key_hash = Column(String(128), nullable=False)  # sha256(full key), unique
    key_enc = Column(
        Text
    )  # Reversible ciphertext of the full key (Fernet), supports copy-again; NULL for legacy keys
    enabled = Column(Boolean, nullable=False, default=True)
    expires_at = Column(UTCDateTime(timezone=True))  # NULL = never expires
    last_used_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    revoked_at = Column(UTCDateTime(timezone=True))  # Soft delete: revocation time

    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_user_api_keys_key_hash"),
        Index("idx_user_api_keys_user_id", "user_id"),
        Index("idx_user_api_keys_user_agent", "user_id", "agent_id"),
        Index("idx_user_api_keys_user_project", "user_id", "project_id"),
        CheckConstraint("agent_id IS NULL OR project_id IS NULL", name="ck_user_api_keys_single_scope"),
        Index("idx_user_api_keys_key_hash", "key_hash"),
    )


# Optional modules own their dependent rows; all cleanups share this transaction.
from core.db.user_cleanup import install_user_cleanup

install_user_cleanup(UserShadow)
