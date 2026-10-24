"""Synchronous installed-module lifecycle callbacks during local schema startup."""
from __future__ import annotations
from collections.abc import Callable, Iterable
from importlib import import_module




def register_schema_lifecycle(name: str, callback: Callable[[], None]) -> None:
    """Register an idempotent local-SQLite initializer by stable owner name."""
    registration_state().callbacks[name] = callback


def load_schema_lifecycle(installed: Iterable[str]) -> None:
    """Compose schema hooks without starting runtime workers or enabling features."""
    for package in installed:
        register = getattr(import_module(f"{package}.extension"), "register_schema", None)
        if register is not None:
            register()


def initialize_local_schema() -> None:
    """Run in startup order; initialization failures must block startup."""
    for callback in tuple(registration_state().callbacks.values()):
        callback()


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    callbacks: dict[str, Callable[[], None]] = state_field(default_factory=dict)


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
