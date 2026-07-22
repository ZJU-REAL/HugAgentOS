"""SQLAlchemy ORM models — identity / teams."""

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


class UserShadow(Base):
    """User shadow table - synced from user center."""

    __tablename__ = "users_shadow"

    user_id = Column(String(64), primary_key=True)
    username = Column(String(255), nullable=False)
    email = Column(String(255))
    avatar_url = Column(Text)
    user_center_id = Column(String(64))
    extra_data = Column("metadata", JSONType, default={})  # Map to 'metadata' column in DB
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    last_sync_at = Column(TIMESTAMP(timezone=True))

    # Relationships
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    catalog_overrides = relationship(
        "CatalogOverride", back_populates="user", cascade="all, delete-orphan"
    )
    kb_spaces = relationship("KBSpace", back_populates="user", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="user", cascade="all, delete-orphan")
    user_agents = relationship(
        "UserAgent", foreign_keys="[UserAgent.user_id]", back_populates="user"
    )

    __table_args__ = (
        Index("idx_users_shadow_user_center_id", "user_center_id"),
        Index("idx_users_shadow_updated_at", "updated_at"),
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
    password_updated_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

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
    (see core/services/user_folder_service.py).
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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

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
    the key up by hash and inherits all of that user's capabilities.
    """

    __tablename__ = "user_api_keys"

    id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(128), nullable=False, default="API Key")
    key_prefix = Column(
        String(32), nullable=False
    )  # Plaintext prefix (e.g. sk-jx-a1b2c3), for list display
    key_hash = Column(String(128), nullable=False)  # sha256(full key), unique
    key_enc = Column(
        Text
    )  # Reversible ciphertext of the full key (Fernet), supports copy-again; NULL for legacy keys
    enabled = Column(Boolean, nullable=False, default=True)
    expires_at = Column(TIMESTAMP(timezone=True))  # NULL = never expires
    last_used_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    revoked_at = Column(TIMESTAMP(timezone=True))  # Soft delete: revocation time

    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_user_api_keys_key_hash"),
        Index("idx_user_api_keys_user_id", "user_id"),
        Index("idx_user_api_keys_key_hash", "key_hash"),
    )


class DingTalkConnection(Base):
    """Per-user DingTalk account connection (used by the dingtalk skill / dws CLI). 1:1 with users_shadow.

    The credentials themselves (OAuth token + encrypted keychain) live on the backend
    persistent volume at ``$STORAGE/dws_cache/{user_id}/``, bind-mounted into the sandbox
    dws's ``$HOME`` — they **never go into the DB**. This table only stores the connection
    **status and a DingTalk identity summary**, plus an optional portable credential bundle
    (``auth_bundle``, for plan-B fallback/migration, stored after application-layer
    encryption). See internal design docs.
    """

    __tablename__ = "dingtalk_connections"

    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # disconnected (default / disconnected) | pending (device-flow login in progress) | connected | error
    status = Column(String(16), nullable=False, default="disconnected")
    dingtalk_user_id = Column(
        String(128)
    )  # DingTalk userId (backfilled from get-self after a successful connect)
    dingtalk_name = Column(String(255))  # DingTalk display name
    corp_id = Column(String(128))  # DingTalk corp corpId
    granted_scopes = Column(
        JSONType, default=list
    )  # List of granted scopes (accumulated via PAT grants)
    # Device-flow login state (echoed to the frontend while pending; cleared on success)
    login_verification_url = Column(
        Text
    )  # Plain verification URL (must be used together with user_code)
    login_verification_url_complete = Column(
        Text
    )  # Full URL with the code embedded → target of the QR code
    login_user_code = Column(String(64))
    login_started_at = Column(TIMESTAMP(timezone=True))
    auth_bundle = Column(
        Text
    )  # Plan B: dws auth export --base64 (application-layer encrypted), nullable
    last_verified_at = Column(TIMESTAMP(timezone=True))
    last_error = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'pending', 'connected', 'error')",
            name="dingtalk_connections_status_check",
        ),
        Index("idx_dingtalk_connections_status", "status"),
    )


class LarkConnection(Base):
    """Per-user Lark (Feishu) account connection (used by the feishu-cli plugin / lark-cli). 1:1 with users_shadow.

    Isomorphic to [[DingTalkConnection]]: the credentials themselves (user_access_token +
    file-based encrypted store, including master.key) live on the backend persistent volume at
    ``$STORAGE/lark_cache/{user_id}/``, bind-mounted into the sandbox lark-cli's ``$HOME``
    (~/.lark-cli + ~/.local/share/lark-cli) — they **never go into the DB**. This table only
    stores the connection status, a Lark identity summary, and the device-flow login state.
    The Lark device flow uses ``auth login --no-wait`` to first obtain device_code/
    verification_url (persisted into login_device_code), then completes with ``--device-code``.
    The app's app_id/secret are seeded into the per-user HOME by the backend via config init
    (not injected as env vars, to avoid breaking --as user).
    See internal design docs.
    """

    __tablename__ = "lark_connections"

    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # disconnected (default / disconnected) | pending (device-flow login in progress) | connected | error
    status = Column(String(16), nullable=False, default="disconnected")
    lark_open_id = Column(
        String(128)
    )  # Lark open_id / union_id (backfilled after a successful connect)
    lark_name = Column(String(255))  # Lark display name
    tenant_key = Column(String(128))  # Lark tenant tenant_key (counterpart of DingTalk corp_id)
    granted_scopes = Column(
        JSONType, default=list
    )  # List of granted scopes (accumulated via incremental grants)
    # Device-flow login state (echoed to the frontend while pending; cleared on success)
    login_verification_url = Column(
        Text
    )  # Plain verification URL (must be used together with user_code)
    login_verification_url_complete = Column(
        Text
    )  # Full URL with the code embedded → target of the QR code
    login_user_code = Column(String(64))
    login_device_code = Column(
        Text
    )  # device_code obtained via --no-wait, used when completing with --device-code
    login_started_at = Column(TIMESTAMP(timezone=True))
    auth_bundle = Column(
        Text
    )  # Portable credential bundle (for cube cross-machine use, application-layer encrypted), nullable
    last_verified_at = Column(TIMESTAMP(timezone=True))
    last_error = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'pending', 'connected', 'error')",
            name="lark_connections_status_check",
        ),
        Index("idx_lark_connections_status", "status"),
    )


class EmailConnection(Base):
    """Per-user email account connection (used by the email plugin / himalaya CLI). 1:1 with users_shadow.

    Isomorphic to [[DingTalkConnection]]/[[LarkConnection]], but with **no device flow / OAuth /
    QR code**: email binds via per-user IMAP/SMTP + an authorization code (app password),
    covering Gmail/Outlook/Exchange/NetEase enterprise mail/Tencent enterprise mail/self-hosted
    mailboxes. Binding is **synchronous** ("save the form → write himalaya config.toml → run
    ``himalaya folder list`` to verify"), so the status is only
    disconnected/connected/error (no pending).

    The credentials themselves (himalaya ``config.toml``, containing plaintext authorization-code
    lines) live on the backend persistent volume at
    ``$STORAGE/email_cache/{user_id}/home/.config/himalaya/`` (0600), bind-mounted into the
    sandbox himalaya's ``~/.config/himalaya``. This table only stores the connection status +
    account/server metadata + the encrypted authorization code (``secret_enc``,
    application-layer Fernet encryption) + a portable config bundle (``config_bundle``, injected
    per-session when cube has no bind-mount, base64(config.toml)).
    See internal design docs.
    """

    __tablename__ = "email_connections"

    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # disconnected (default / disconnected) | connected | error (no pending — binding is a synchronous check)
    status = Column(String(16), nullable=False, default="disconnected")
    email_address = Column(String(320))  # Bound email address (= IMAP/SMTP login name)
    display_name = Column(String(255))  # Sender display name
    provider = Column(
        String(32)
    )  # Auto-detected provider: gmail/outlook/netease/qq/qiye163/exmail/custom

    # Server settings (auto-detected + user-overridable)
    imap_host = Column(String(255))
    imap_port = Column(Integer)
    imap_security = Column(String(16))  # tls | starttls | none
    smtp_host = Column(String(255))
    smtp_port = Column(Integer)
    smtp_security = Column(String(16))

    secret_enc = Column(
        Text
    )  # Authorization code (application-layer Fernet encryption, never stored in plaintext)
    config_bundle = Column(Text)  # base64(config.toml), for cube cross-session injection, nullable
    last_verified_at = Column(TIMESTAMP(timezone=True))
    last_error = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'connected', 'error')",
            name="email_connections_status_check",
        ),
        Index("idx_email_connections_status", "status"),
    )


class ChannelConnection(Base):
    """Inbound channel bot (owner service-account model). One row = one user-created external IM bot.

    Orthogonal to the **outbound** plugins [[LarkConnection]]/[[DingTalkConnection]]: those are
    "the agent operates Lark/DingTalk with the user's OAuth identity"; this table is "an
    external IM pushes messages in to trigger the agent" (**inbound**), and the credentials are
    an **application identity** (App ID/Secret), not user OAuth.

    Multi-tenancy uses the owner service-account model: the bot is bound to ``owner_user_id``,
    and all inbound messages run with the owner's identity + permissions; people in a group are
    not tied to platform teams and there is no per-sender identity resolution (open_id is only
    recorded for audit). p2p and group share the same orchestration path; the only difference is
    session keying (by sender open_id vs by group chat_id).

    Credentials (App Secret / Encrypt Key / Verification Token) are encrypted with
    application-layer Fernet via ``core.infra.crypto`` and stored in the ``config`` JSON —
    **never stored in plaintext, never unioned into the global environment**. ``app_id`` is
    redundantly stored in plaintext in its own column for the unique constraint (token lock:
    the same Lark app cannot be bound by two people).

    See internal design docs.
    """

    __tablename__ = "channel_connections"

    channel_id = Column(String(64), primary_key=True)
    owner_user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type = Column(String(16), nullable=False)  # 'lark' | 'dingtalk' | 'wecom' | ...
    display_name = Column(String(100), nullable=False, default="我的机器人")
    transport = Column(String(16), nullable=False, default="long_conn")  # 'long_conn' | 'webhook'
    app_id = Column(
        String(128), nullable=False
    )  # Plaintext redundancy: for the unique constraint + establishing the long connection
    config = Column(
        JSONType, nullable=False, default=dict
    )  # Encrypted credentials {app_secret_enc, encrypt_key_enc, verification_token_enc}
    # Resource allowlist (for when the owner wants precise control over what the bot exposes):
    # {"kb_ids": [...], "skill_ids": [...]}; NULL = expose everything the owner has (default).
    # Retrieval/skill loading narrows down by this on top of the owner's permissions.
    resource_scope = Column(JSONType, nullable=True)
    # Binding to a specific sub-agent: non-NULL → inbound messages are pinned to that
    # [[UserAgent]] (runs with its own prompt/tools/model); written when binding on the
    # "sub-agent page"; NULL → main agent (owner's default capabilities), left empty when
    # binding in the settings "My Bots" page.
    # ondelete=SET NULL: deleting the sub-agent makes the bot fall back to the main agent
    # automatically, without cascading the bot's deletion.
    agent_id = Column(
        String(64), ForeignKey("user_agents.agent_id", ondelete="SET NULL"), nullable=True
    )
    # disconnected (default / disconnected) | pending (verifying) | connected | error
    status = Column(String(16), nullable=False, default="pending")
    enabled = Column(Boolean, nullable=False, default=True)
    last_event_at = Column(TIMESTAMP(timezone=True))
    last_error = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'pending', 'connected', 'error')",
            name="channel_connections_status_check",
        ),
        CheckConstraint(
            "transport IN ('long_conn', 'webhook')",
            name="channel_connections_transport_check",
        ),
        # token lock: within the same channel, the same app can only be bound once (prevents multiple people grabbing the same bot credentials)
        UniqueConstraint("channel_type", "app_id", name="uq_channel_connections_type_app"),
        Index("idx_channel_connections_owner", "owner_user_id"),
        Index("idx_channel_connections_enabled", "enabled", "status"),
        Index("idx_channel_connections_agent", "agent_id"),
    )
