"""Transactional cleanup contributions for ORM user deletion."""
from __future__ import annotations
from collections.abc import Callable
from typing import Any




def register_user_cleanup(handler: Callable[[Any, Any], None]) -> None:
    if handler not in registration_state().handlers:
        registration_state().handlers.append(handler)


def cleanup_user(session: Any, user: Any) -> None:
    for handler in tuple(registration_state().handlers):
        handler(session, user)


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    handlers: list[Callable[[Any, Any], None]] = state_field(default_factory=list)


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
