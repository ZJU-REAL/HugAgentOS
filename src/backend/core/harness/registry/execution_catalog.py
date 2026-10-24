"""Host-owned execution catalog selection, optional on cloud-only hosts."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol

class ExecutionCatalog(Protocol):
    def local_plugin_configs(self, user_id, enabled_only=True) -> dict: ...
    def bridge_context(self) -> dict[str, Any] | None: ...
    def apply_to_enabled_mcp_ids(self, ids: list[str] | None) -> list[str] | None: ...
    def apply_to_enabled_skill_ids(self, ids: list[str] | None) -> list[str] | None: ...
    def cloud_gateway_mcp_configs(self, enabled_mcp_ids=None, *, resolution_out=None, unavailable_out=None) -> dict: ...


def register_execution_catalog(provider: ExecutionCatalog | None) -> None:
    pass
    registration_state().provider = provider

def bridge_context():
    return registration_state().provider.bridge_context() if registration_state().provider is not None else None

def apply_to_enabled_mcp_ids(ids):
    return registration_state().provider.apply_to_enabled_mcp_ids(ids) if registration_state().provider is not None else ids

def apply_to_enabled_skill_ids(ids):
    return registration_state().provider.apply_to_enabled_skill_ids(ids) if registration_state().provider is not None else ids

def cloud_gateway_mcp_configs(enabled_mcp_ids=None, *, resolution_out=None, unavailable_out=None):
    if registration_state().provider is None:
        return {}
    return registration_state().provider.cloud_gateway_mcp_configs(enabled_mcp_ids, resolution_out=resolution_out, unavailable_out=unavailable_out)


def local_plugin_configs(user_id, enabled_only=True):
    return registration_state().provider.local_plugin_configs(user_id, enabled_only=enabled_only) if registration_state().provider else {}


def authorizes_manager(user_id, server_id, manager):
    config = local_plugin_configs(user_id).get(server_id)
    return bool(config and config.get("gateway_plugin") == manager)


@dataclass
class RegistrationState:
    provider: ExecutionCatalog | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
