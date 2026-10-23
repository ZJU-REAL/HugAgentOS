"""Frozen capability authorization supplied by the execution host."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol
from core.harness.capability_errors import ViewUnavailable


class CapabilityRuntime(Protocol):
    def pin_agent_definition(self, run_id, user_id, agent, *, scope_id=""): ...
    def view_for_execution(self, run_id, user_id, *, scope_id=""): ...
    def get(self, run_id: str, scope_id: str = "") -> Any: ...
    def validate(self, run: Any, *, user_id: str | None = None,
                 execution_plane: str = "local", only_skill: str | None = None) -> None: ...
    def validate_activation(self, run: Any, nodes: Any, *, available_mcp=()) -> None: ...
    def require_root_tool_scope(self, run_id: str, user_id: str, tool_call_id: str, tool_name: str) -> None: ...
    def child_scope(self, parent_scope: str, kind: str, *identities: str) -> str: ...


def register_capability_runtime(provider: CapabilityRuntime) -> None:
    pass
    registration_state().provider = provider


def require_runtime() -> CapabilityRuntime:
    if registration_state().provider is None:
        raise ViewUnavailable("Execution host capability runtime is not registered")
    return registration_state().provider


def get(run_id: str, scope_id: str = ""):
    return require_runtime().get(run_id, scope_id=scope_id)


def validate(run, *, user_id=None, execution_plane="local", only_skill=None):
    return require_runtime().validate(run, user_id=user_id, execution_plane=execution_plane, only_skill=only_skill)


def validate_activation(run, nodes, *, available_mcp=()):
    return require_runtime().validate_activation(run, nodes, available_mcp=available_mcp)


def require_root_tool_scope(run_id, user_id, tool_call_id, tool_name):
    return require_runtime().require_root_tool_scope(run_id, user_id, tool_call_id, tool_name)


def child_scope(parent_scope, kind, *identities):
    # Scope identities are used even on cloud hosts without device capabilities.
    import hashlib
    import json
    from core.harness.capability_errors import IntegrityFailed
    if not kind or not identities or any(not str(value or "").strip() for value in identities):
        raise IntegrityFailed("capability scope requires durable invocation identities")
    material = json.dumps([str(parent_scope or ""), str(kind), *map(str, identities)], separators=(",", ":"))
    return "s_" + hashlib.sha256(material.encode()).hexdigest()


def pin_agent_definition(run_id, user_id, agent, *, scope_id=""):
    return require_runtime().pin_agent_definition(run_id, user_id, agent, scope_id=scope_id)


def view_for_execution(run_id, user_id, *, scope_id=""):
    return require_runtime().view_for_execution(run_id, user_id, scope_id=scope_id)


@dataclass
class RegistrationState:
    provider: CapabilityRuntime | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
