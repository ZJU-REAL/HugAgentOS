"""Durable desktop delivery references and non-executable cloud management copies."""

from datetime import datetime, timezone

from core.db.engine import Base
from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Integer, String, Text


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
