"""Optional domain-owned option lists used by agent administration."""
from __future__ import annotations
from collections.abc import Callable
from typing import Any




def register_catalog_options(kind: str, provider: Callable[[Any], list[dict]]) -> None:
    registration_state().providers[kind] = provider


def catalog_options(kind: str, db: Any) -> list[dict]:
    provider = registration_state().providers.get(kind)
    return provider(db) if provider is not None else []


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    providers: dict[str, Callable[[Any], list[dict]]] = state_field(default_factory=dict)


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
