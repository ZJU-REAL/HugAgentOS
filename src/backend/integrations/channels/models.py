"""Channel connection and durable device relay metadata."""
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from sqlalchemy import JSON, Boolean, CheckConstraint, Column, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
JSONType = JSON().with_variant(JSONB(), "postgresql")

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
    # Group listening: how much of a group conversation the bot may see.
    #   mention_only (default) — only messages that @ the bot; anything else is dropped.
    #   observe_all            — bystander group messages are recorded as background context
    #                            (never replied to) and folded into the next @bot turn.
    # observe_all is inert unless the channel app also holds the platform's "read all group
    # messages" permission (Lark ``im:message.group_msg`` / DingTalk group-message read) —
    # without it the platform never delivers non-@ messages in the first place.
    # Defaults to mention_only because observing a whole group's chatter is privacy-sensitive
    # and must be an explicit, per-bot decision by the owner.
    group_listen_mode = Column(
        String(16), nullable=False, default="mention_only", server_default="mention_only"
    )
    # disconnected (default / disconnected) | pending (verifying) | connected | error
    status = Column(String(16), nullable=False, default="pending")
    enabled = Column(Boolean, nullable=False, default=True)
    last_event_at = Column(UTCDateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'pending', 'connected', 'error')",
            name="channel_connections_status_check",
        ),
        CheckConstraint(
            "transport IN ('long_conn', 'webhook')",
            name="channel_connections_transport_check",
        ),
        CheckConstraint(
            "group_listen_mode IN ('mention_only', 'observe_all')",
            name="channel_connections_group_listen_check",
        ),
        # token lock: within the same channel, the same app can only be bound once (prevents multiple people grabbing the same bot credentials)
        UniqueConstraint("channel_type", "app_id", name="uq_channel_connections_type_app"),
        Index("idx_channel_connections_owner", "owner_user_id"),
        Index("idx_channel_connections_enabled", "enabled", "status"),
        Index("idx_channel_connections_agent", "agent_id"),
    )

class DesktopChannelBinding(Base):
    __tablename__ = "desktop_channel_bindings"
    binding_id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False, index=True)
    device_id = Column(String(128), nullable=False)
    device_name = Column(String(100), nullable=False)
    consumed = Column(Boolean, nullable=False, default=False)
    created_at = Column(Float, nullable=False)
    last_seen = Column(Float, nullable=False)

class ChannelRelayDelivery(Base):
    __tablename__ = "channel_relay_deliveries"
    delivery_id = Column(String(64), primary_key=True)
    channel_id = Column(String(64), nullable=False)
    binding_id = Column(String(64), nullable=False)
    owner_id = Column(String(64), nullable=False)
    device_id = Column(String(128), nullable=False)
    status = Column(String(24), nullable=False)
    payload = Column(JSON, nullable=False)
    lease = Column(String(64), nullable=True)
    created_at = Column(Float, nullable=False)
    deadline = Column(Float, nullable=False)
    error = Column(String(300), nullable=True)
    __table_args__ = (
        Index("ix_channel_relay_queue", "owner_id", "device_id", "status", "created_at"),
    )

class ChannelRelayOperation(Base):
    __tablename__ = "channel_relay_operations"
    operation_id = Column(String(128), primary_key=True)
    status = Column(String(24), nullable=False)
    result = Column(JSON, nullable=True)
