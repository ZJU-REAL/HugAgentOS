"""Lark connection persistence metadata."""
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from sqlalchemy import JSON, CheckConstraint, Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
JSONType = JSON().with_variant(JSONB(), "postgresql")

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
    login_started_at = Column(UTCDateTime(timezone=True))
    auth_bundle = Column(
        Text
    )  # Portable credential bundle (for cube cross-machine use, application-layer encrypted), nullable
    last_verified_at = Column(UTCDateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'pending', 'connected', 'error')",
            name="lark_connections_status_check",
        ),
        Index("idx_lark_connections_status", "status"),
    )
