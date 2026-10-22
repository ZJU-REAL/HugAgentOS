"""Host account authorization for account-scoped capability selection."""
from __future__ import annotations
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


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    provider: ExecutionAccount | None = None


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
