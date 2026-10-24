"""Transactional cleanup contributions for ORM user deletion."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def register_user_cleanup(handler: Callable[[Any, Any], None]) -> None:
    if handler not in registration_state().handlers:
        registration_state().handlers.append(handler)


def cleanup_user(session: Any, user: Any) -> None:
    for handler in tuple(registration_state().handlers):
        handler(session, user)


@dataclass
class RegistrationState:
    handlers: list[Callable[[Any, Any], None]] = field(default_factory=list)


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
