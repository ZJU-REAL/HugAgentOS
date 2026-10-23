"""Desktop installation indexes and durable observation delivery records."""
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from datetime import datetime
from core.db.engine import Base
from core.db.models import JSONType
from sqlalchemy import Boolean, Column, Index, Integer, String, Text
from datetime import datetime, timezone
from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Integer, String, Text

INSTALL_STATES = ("pending", "preparing", "ready", "failed", "removed")

class DeviceCapabilityInstallation(Base):
    """One (profile, kind, key) on this device: intent, resolved revision and state.

    ``state``: pending = account intent known, nothing downloaded yet; preparing =
    download/verification in flight; ready = revision verified, files published;
    failed = last preparation failed (``last_error``); removed = tombstone.
    Ready is only announced after every required component is active and the
    runtime view link exists.
    """

    __tablename__ = "device_capability_installations"

    install_id = Column(String(255), primary_key=True)  # f"{kind}:{profile}:{key}"
    profile_id = Column(String(64), nullable=False)
    kind = Column(String(16), nullable=False)
    key = Column(String(160), nullable=False)
    ref_issuer = Column(String(255), nullable=False)
    ref_namespace = Column(String(64), nullable=False)
    ref_id = Column(String(160), nullable=False)
    display_name = Column(String(255), nullable=False, default="")
    description = Column(Text, default="")
    version = Column(String(64), default="")
    # Cloud-published content hash for the wanted revision; the store revision is derived from it.
    content_hash = Column(String(64), nullable=True)
    resolved_revision = Column(String(64), nullable=True)
    state = Column(String(16), nullable=False, default="pending")
    enabled = Column(Boolean, nullable=False, default=True)
    # "cloud" (account profile), "local" (device-created/imported), "plugin" (component of a plugin)
    source = Column(String(16), nullable=False, default="cloud")
    source_plugin = Column(String(160), nullable=True)
    generation = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    payload = Column(JSONType, default=dict)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("idx_dev_cap_inst_profile_kind", "profile_id", "kind"),
        Index("idx_dev_cap_inst_kind_key", "kind", "key"),
        Index("idx_dev_cap_inst_state", "state"),
    )

class DeviceCapabilityNamePreference(Base):
    """The user's explicit answer to "which same-named candidate wins". Device-only."""

    __tablename__ = "device_capability_name_preferences"

    preference_id = Column(String(200), primary_key=True)  # f"{kind}:{runtime_name}"
    kind = Column(String(16), nullable=False)
    runtime_name = Column(String(160), nullable=False)
    chosen_install_id = Column(String(255), nullable=False)
    chosen_by = Column(String(64), nullable=True)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

class DeviceCapabilityTransaction(Base):
    """Install transaction log: explains what is committed and what is not after a crash."""

    __tablename__ = "device_capability_transactions"

    tx_id = Column(String(64), primary_key=True)
    install_id = Column(String(255), nullable=False)
    phase = Column(String(16), nullable=False)  # staged | published | committed | failed
    target_generation = Column(Integer, nullable=False, default=0)
    file_inventory = Column(JSONType, default=list)
    error = Column(Text, nullable=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (Index("idx_dev_cap_tx_install", "install_id"),)

class DeviceCapabilityComponent(Base):
    """Ownership edge: a plugin installation → the component installations it requires."""

    __tablename__ = "device_capability_components"

    edge_id = Column(String(512), primary_key=True)  # f"{owner}->{component}"
    owner_install_id = Column(String(255), nullable=False)
    component_install_id = Column(String(255), nullable=False)
    required = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("idx_dev_cap_comp_owner", "owner_install_id"),
        Index("idx_dev_cap_comp_component", "component_install_id"),
    )

def utcnow():
    return datetime.now(timezone.utc).isoformat()

class DesktopOutbox(Base):
    __tablename__ = "desktop_observability_outbox"
    id = Column(String(64), primary_key=True)
    cloud_base = Column(String(512), nullable=False)
    subject = Column(String(64), nullable=False)
    device_id = Column(String(128), nullable=False)
    local_user_id = Column(String(64), nullable=False)
    kind = Column(String(24), nullable=False)
    object_id = Column(String(128), nullable=False)
    revision = Column(BigInteger, nullable=False, default=1)
    pending = Column(Boolean, nullable=False, default=True)
    deleted = Column(Boolean, nullable=False, default=False)
    digest = Column(String(64), nullable=False, default="")
    updated_at = Column(String(40), nullable=False, default=utcnow)
    __table_args__ = (
        Index("idx_desktop_outbox_pending", "cloud_base", "subject", "device_id", "pending"),
    )

class DesktopRecord(Base):
    __tablename__ = "desktop_observability_records"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    device_id = Column(String(128), nullable=False)
    kind = Column(String(24), nullable=False)
    object_id = Column(String(128), nullable=False)
    revision = Column(BigInteger, nullable=False)
    chat_id = Column(String(128), nullable=False, default="")
    run_id = Column(String(128), nullable=False, default="")
    observation = Column(String(16), nullable=False, default="desktop")
    payload = Column(JSON, nullable=False)
    received_at = Column(String(40), nullable=False, default=utcnow)
    __table_args__ = (
        Index("idx_desktop_records_identity", "kind", "observation", "user_id", "device_id", "object_id", unique=True),
        Index("idx_desktop_records_user_kind", "user_id", "kind", "received_at"),
        Index("idx_desktop_records_chat", "user_id", "device_id", "chat_id"),
    )

class DesktopSyncDevice(Base):
    __tablename__ = "desktop_observability_devices"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    device_id = Column(String(128), nullable=False)
    last_seen = Column(String(40), nullable=False, default=utcnow)
    pending = Column(Integer, nullable=False, default=0)
    capture_errors = Column(Integer, nullable=False, default=0)
    reconciling = Column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("idx_desktop_devices_user", "user_id"),)

__all__ = ['DeviceCapabilityInstallation', 'DeviceCapabilityNamePreference', 'DeviceCapabilityTransaction', 'DeviceCapabilityComponent', 'DesktopOutbox', 'DesktopRecord', 'DesktopSyncDevice', 'INSTALL_STATES']
