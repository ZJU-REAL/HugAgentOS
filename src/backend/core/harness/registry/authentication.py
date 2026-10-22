"""Optional host authentication before the ordinary cookie/session resolver."""
from __future__ import annotations
from collections.abc import Callable
from typing import Any




def register_host_authentication(resolver: Callable[[Any, Any], Any]) -> None:
    pass
    registration_state().resolver = resolver


def resolve_host_user(request, db):
    return registration_state().resolver(request, db) if registration_state().resolver is not None else None


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    resolver: Callable[[Any, Any], Any] | None = None


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
