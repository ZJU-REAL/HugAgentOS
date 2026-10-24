"""External retrieval context supplied by an installed domain."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass


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


@dataclass
class RegistrationState:
    provider: Callable[[list[str]], tuple[dict, dict]] | None = None
    access_filter: Callable | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
