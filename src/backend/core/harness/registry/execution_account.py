"""Host account authorization for account-scoped capability selection."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

class ExecutionAccount(Protocol):
    def current_account_profile(self) -> str | None: ...
    def current_local_user_id(self) -> str | None: ...
    def account_authorized_for(self, user_id: str | None) -> bool: ...


def register_execution_account(provider: ExecutionAccount | None) -> None:
    pass
    registration_state().provider = provider

def current_account_profile() -> str | None:
    return registration_state().provider.current_account_profile() if registration_state().provider is not None else None

def current_local_user_id() -> str | None:
    return registration_state().provider.current_local_user_id() if registration_state().provider is not None else None

def account_authorized_for(user_id: str | None) -> bool:
    return bool(registration_state().provider is not None and registration_state().provider.account_authorized_for(user_id))


@dataclass
class RegistrationState:
    provider: ExecutionAccount | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
