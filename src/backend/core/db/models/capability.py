"""Device-side capability installation index.

These tables live in the device's existing business database (no second SQLite),
so an install transaction and its business-data side effects can share one
transaction. On cloud deployments they exist but stay empty — the file store is
disabled there and nothing writes to them.
"""

from datetime import datetime

from core.db.engine import Base
from core.db.models import JSONType
from sqlalchemy import TIMESTAMP, Boolean, Column, Index, Integer, String, Text

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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

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
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class DeviceCapabilityTransaction(Base):
    """Install transaction log: explains what is committed and what is not after a crash."""

    __tablename__ = "device_capability_transactions"

    tx_id = Column(String(64), primary_key=True)
    install_id = Column(String(255), nullable=False)
    phase = Column(String(16), nullable=False)  # staged | published | committed | failed
    target_generation = Column(Integer, nullable=False, default=0)
    file_inventory = Column(JSONType, default=list)
    error = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

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
