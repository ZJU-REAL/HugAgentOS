"""DingTalk connection metadata; secrets remain in per-user storage."""
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from sqlalchemy import JSON, CheckConstraint, Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
JSONType = JSON().with_variant(JSONB(), "postgresql")


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
    login_started_at = Column(UTCDateTime(timezone=True))
    auth_bundle = Column(
        Text
    )  # Plan B: dws auth export --base64 (application-layer encrypted), nullable
    last_verified_at = Column(UTCDateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'pending', 'connected', 'error')",
            name="dingtalk_connections_status_check",
        ),
        Index("idx_dingtalk_connections_status", "status"),
    )
