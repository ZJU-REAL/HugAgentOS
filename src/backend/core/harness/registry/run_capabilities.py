"""Per-run capability contracts supplied by an installed execution environment."""
from __future__ import annotations
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class PreparedCapabilities(Protocol):
    run_id: str
    bindings: Mapping[str, Any]
    dependency_report: dict
    unavailable: dict

    def validate_tool_call(self, server_id: str) -> bool: ...
    def release(self) -> None: ...
    def validate_selected_skills(self, skill_ids: list[str] | None) -> None: ...
    def validate(self, *, user_id: str) -> None: ...
    def record_tool_scope(self, tool_call_id: str, tool_name: str) -> None: ...
    def child_scope(self, kind: str, *identities: str) -> str: ...
    def skill_loader(self) -> Any: ...
    def tool_guard(self, *, scope_id: str): ...
    def bind_mcp(self, servers: dict, resolution: Any) -> dict: ...
    def preflight(self, **context: Any) -> PreparedCapabilities: ...


class RunCapabilityProvider(Protocol):
    def cloud_plugin_selection(self, plugin_id, user_id): ...
    def resolve_explicit_ids(self, user_id, skill_ids, mcp_ids): ...
    def plugin_skill_defaults(self, user_id, skill_ids): ...
    def ensure_ready(self, user_id, *, skill_keys=None, plugin_keys=None): ...
    def available_skill_names(self, names, *, user_id): ...
    def plugin_binding_ids(self, plugin_ids, *, user_id): ...
    async def prepare(self, request: Any, **selection: Any) -> tuple[Any, Any, PreparedCapabilities | None]: ...


class CloudCapabilities:
    def cloud_plugin_selection(self, plugin_id, user_id):
        return None

    def resolve_explicit_ids(self, user_id, skill_ids, mcp_ids):
        return skill_ids, mcp_ids

    def plugin_skill_defaults(self, user_id, skill_ids):
        return skill_ids

    def ensure_ready(self, user_id, *, skill_keys=None, plugin_keys=None):
        return None

    def available_skill_names(self, names, *, user_id):
        return list(names)

    def plugin_binding_ids(self, plugin_ids, *, user_id):
        return [], []

    async def prepare(self, request, **selection):
        return selection["_caps_skill_ids"], None, None


_default = CloudCapabilities()


def register_run_capability_provider(name, provider, *, enabled):
    registration_state().providers[name] = (enabled, provider)


def run_capability_provider() -> RunCapabilityProvider:
    for enabled, provider in tuple(registration_state().providers.values()):
        if enabled():
            return provider
    return _default


def has_run_capabilities() -> bool:
    return run_capability_provider() is not _default


async def prepare_run_capabilities(request, **selection):
    return await run_capability_provider().prepare(request, **selection)


def ensure_cloud_ready(user_id, *, skill_keys=None, plugin_keys=None):
    return run_capability_provider().ensure_ready(user_id, skill_keys=skill_keys, plugin_keys=plugin_keys)


def filter_available_names(names, *, user_id):
    return run_capability_provider().available_skill_names(names, user_id=user_id)


def cloud_binding_ids(plugin_ids, *, user_id=None):
    return run_capability_provider().plugin_binding_ids(plugin_ids, user_id=user_id)


def plugin_skill_defaults(user_id, skill_ids):
    return run_capability_provider().plugin_skill_defaults(user_id, skill_ids)


def resolve_explicit_ids(user_id, skill_ids, mcp_ids):
    return run_capability_provider().resolve_explicit_ids(user_id, skill_ids, mcp_ids)


def cloud_plugin_selection(plugin_id, user_id):
    return run_capability_provider().cloud_plugin_selection(plugin_id, user_id)


@dataclass
class RegistrationState:
    providers: dict[str, tuple[Callable[[], bool], RunCapabilityProvider]] = field(default_factory=dict)


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
