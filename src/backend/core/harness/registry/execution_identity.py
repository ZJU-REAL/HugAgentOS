"""Host-owned execution identity contract for core model and tool operations."""
from __future__ import annotations
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol


class ExecutionIdentity(Protocol):
    def context_headers(self, *, model=False) -> dict[str, str]: ...
    def notify_cloud_changed(self) -> None: ...
    def get_state(self) -> dict[str, Any] | None: ...
    def state_fingerprint(self, state: dict[str, Any] | None) -> str: ...
    def require_current_account(self, state: dict[str, Any]) -> None: ...
    def ensure_current_authorization(self, state: dict[str, Any] | None = None) -> None: ...
    def cloud_headers(self, state: dict[str, Any]) -> dict[str, str]: ...
    def account_scope(self, state: dict[str, Any]) -> AbstractContextManager: ...
    def clear_state(self) -> None: ...


def register_execution_identity(provider: ExecutionIdentity | None) -> None:
    pass
    registration_state().provider = provider


def require_provider() -> ExecutionIdentity:
    if registration_state().provider is None:
        raise RuntimeError("Execution identity provider is not registered")
    return registration_state().provider


def get_state() -> dict[str, Any] | None:
    return registration_state().provider.get_state() if registration_state().provider is not None else None


def state_fingerprint(state: dict[str, Any] | None) -> str:
    if state is None and registration_state().provider is None:
        return ""
    return require_provider().state_fingerprint(state)


def require_current_account(state: dict[str, Any]) -> None:
    require_provider().require_current_account(state)


def ensure_current_authorization(state: dict[str, Any] | None = None) -> None:
    require_provider().ensure_current_authorization(state)


def cloud_headers(state: dict[str, Any]) -> dict[str, str]:
    return require_provider().cloud_headers(state)


def account_scope(state: dict[str, Any]) -> AbstractContextManager:
    return require_provider().account_scope(state)


def clear_state() -> None:
    require_provider().clear_state()


def context_headers(*, model=False):
    return registration_state().provider.context_headers(model=model) if registration_state().provider else {}


def notify_cloud_changed():
    return require_provider().notify_cloud_changed()


@dataclass
class RegistrationState:
    provider: ExecutionIdentity | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
