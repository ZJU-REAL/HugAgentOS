"""Optional host authentication before the ordinary cookie/session resolver."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def register_host_authentication(resolver: Callable[[Any, Any], Any]) -> None:
    pass
    registration_state().resolver = resolver


def resolve_host_user(request, db):
    return registration_state().resolver(request, db) if registration_state().resolver is not None else None


@dataclass
class RegistrationState:
    resolver: Callable[[Any, Any], Any] | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
