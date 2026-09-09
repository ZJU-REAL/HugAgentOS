"""Durable, device-scoped channel delivery. Shared by CE and EE."""

from core.db.engine import Base
from sqlalchemy import JSON, Boolean, Column, Float, Index, String


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
