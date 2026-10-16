"""Plugin-owned interactive resources and encrypted checkpoints."""

from core.db.engine import Base
from sqlalchemy import CheckConstraint, Column, Float, LargeBinary, String, Text


class PluginResource(Base):
    __tablename__ = "plugin_resources"
    __table_args__ = (
        CheckConstraint(
            "(chat_id IS NOT NULL AND chat_id <> '' AND application_key IS NULL AND instance_id IS NULL) OR "
            "(chat_id IS NULL AND application_key IS NOT NULL AND application_key <> '' AND instance_id IS NOT NULL AND instance_id <> '')",
            name="ck_plugin_resource_owner",
        ),
    )
    resource_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    chat_id = Column(String(128), nullable=True, index=True)
    application_key = Column(String(256), nullable=True, index=True)
    instance_id = Column(String(128), nullable=True)
    request_digest = Column(String(64), nullable=True)
    install_id = Column(String(160), nullable=False)
    revision = Column(String(128), nullable=False)
    slug = Column(String(100), nullable=False)
    module_id = Column(String(64), nullable=False)
    scope = Column(String(16), nullable=False)
    descriptor = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="active")
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False, index=True)


class PluginResourceTicket(Base):
    __tablename__ = "plugin_resource_tickets"
    digest = Column(String(64), primary_key=True)
    resource_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False)
    expires_at = Column(Float, nullable=False, index=True)
    origin = Column(String(512), nullable=False)
    auth_enc = Column(Text, nullable=False, default="")


class PluginResourceCheckpoint(Base):
    __tablename__ = "plugin_resource_checkpoints"
    checkpoint_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    install_id = Column(String(160), nullable=False)
    name = Column(String(80), nullable=False)
    state_enc = Column(Text, nullable=False)


class PluginResourcePackage(Base):
    __tablename__ = "plugin_resource_packages"
    revision = Column(String(64), primary_key=True)
    archive = Column(LargeBinary, nullable=False)


class PluginResourceAssetTicket(Base):
    __tablename__ = "plugin_resource_asset_tickets"
    digest = Column(String(64), primary_key=True)
    resource_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False)
    expires_at = Column(Float, nullable=False, index=True)
    auth_enc = Column(Text, nullable=False, default="", server_default="")
