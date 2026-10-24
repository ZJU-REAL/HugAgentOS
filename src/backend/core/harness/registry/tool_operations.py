"""Host registrations for component-owned local dispatch and durable receipts."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class RemoteOperationConflict(ValueError):
    """The authenticated operation key is already bound to another request."""


@dataclass(frozen=True)
class ToolOperations:
    receipt_tools: frozenset[str]
    local_tools: frozenset[str]
    bind: Callable[..., str]
    lookup: Callable[..., Any]
    remember: Callable[..., Any]
    dispatch_local: Callable[..., Any]
    prepare_arguments: Callable[..., dict] | None = None


def register_tool_operations(component: str, operations: ToolOperations) -> None:
    registration_state().components[component] = operations


def receipt_operations(tool_name: str) -> ToolOperations | None:
    return next((value for value in registration_state().components.values() if tool_name in value.receipt_tools), None)


def local_tool_allowed(component: str, tool_name: str) -> bool:
    provider = registration_state().components.get(component)
    return provider is not None and tool_name in provider.local_tools


async def local_tool_result(component, tool_name, arguments, headers, *, authorize=None):
    provider = registration_state().components.get(component)
    if provider is None or tool_name not in provider.local_tools:
        return None
    return await provider.dispatch_local(component, tool_name, arguments, headers, authorize=authorize)


def prepare_tool_arguments(tool_name, arguments, user_id, chat_id):
    provider = receipt_operations(tool_name)
    if provider is None or provider.prepare_arguments is None:
        return arguments
    return provider.prepare_arguments(tool_name, arguments, user_id, chat_id)


@dataclass
class RegistrationState:
    components: dict[str, ToolOperations] = field(default_factory=dict)


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
