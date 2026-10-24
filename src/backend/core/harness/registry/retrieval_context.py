"""External retrieval context supplied by an installed domain."""
from __future__ import annotations
from collections.abc import Callable




def register_retrieval_context(provider: Callable[[list[str]], tuple[dict, dict]] | None) -> None:
    pass
    registration_state().provider = provider


def runtime_request_context(selected_ids: list[str]) -> tuple[dict, dict]:
    return registration_state().provider(selected_ids) if registration_state().provider is not None else ({}, {})





def register_retrieval_access_filter(provider: Callable) -> None:
    pass
    registration_state().access_filter = provider


def filter_accessible_kb_ids(db, user_id: str, selected_ids: list[str]) -> list[str]:
    if registration_state().access_filter is None:
        raise RuntimeError("Retrieval access policy is not registered")
    return registration_state().access_filter(db, user_id, selected_ids)


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    provider: Callable[[list[str]], tuple[dict, dict]] | None = None
    access_filter: Callable | None = None


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
