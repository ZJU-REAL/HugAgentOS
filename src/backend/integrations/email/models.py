"""Email connection persistence metadata."""
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text

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
    last_verified_at = Column(UTCDateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('disconnected', 'connected', 'error')",
            name="email_connections_status_check",
        ),
        Index("idx_email_connections_status", "status"),
    )
