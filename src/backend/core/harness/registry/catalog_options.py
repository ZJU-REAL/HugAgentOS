"""Optional domain-owned option lists used by agent administration."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def register_catalog_options(kind: str, provider: Callable[[Any], list[dict]]) -> None:
    registration_state().providers[kind] = provider


def catalog_options(kind: str, db: Any) -> list[dict]:
    provider = registration_state().providers.get(kind)
    return provider(db) if provider is not None else []


@dataclass
class RegistrationState:
    providers: dict[str, Callable[[Any], list[dict]]] = field(default_factory=dict)


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
