"""Installed application owners supply policy for generic plugin resources."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ResourceOwner:
    binding: Callable[..., Any]
    resolve_installation: Callable[..., Any]
    exact_installation: Callable[..., Any]
    authorize: Callable[..., Any]
    observe: Callable[..., Any]
    record: Callable[..., Any]
    renew: Callable[..., Any]
    arm: Callable[..., Any]
    invalidate: Callable[..., Any]
    sweep: Callable[..., Any]
    collect: Callable[..., Any]
    create: Callable[..., Any]
    owned: Callable[..., Any]
    lease: Callable[..., Any]
    reserve: Callable[..., Any]
    close: Callable[..., Any]
    revoke_session: Callable[..., Any]





def register_resource_owner(owner: ResourceOwner) -> None:
    pass
    registration_state().owner = owner


def resource_owner() -> ResourceOwner:
    if registration_state().owner is None:
        raise RuntimeError("Host must register the application resource owner")
    return registration_state().owner


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    owner: ResourceOwner | None = None


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
