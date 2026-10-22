"""Host-selected request authorization; no execution-environment imports."""
from __future__ import annotations
from collections.abc import Callable
from typing import Protocol




def register_request_header_resolver(resolver: Callable | None) -> None:
    pass
    registration_state().resolver = resolver


def prepare_request_headers(api_key, base_url, captured_state=None):
    if registration_state().resolver is not None:
        return registration_state().resolver(api_key, base_url, captured_state)
    return {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}


class ModelCredentialProvider(Protocol):
    """Host contract for account-bound references and request authorization."""
    def is_reference(self, value): ...
    def bind(self, reference, base_url): ...
    def request_hook(self, reference, base_url): ...
    def sync_client_kwargs(self, reference, base_url, *, timeout=600): ...





def register_model_credential_provider(provider):
    pass
    registration_state().provider = provider


def require_provider():
    if registration_state().provider is None:
        from core.harness.capability_errors import CloudUnavailable
        raise CloudUnavailable("Model credential provider is not registered")
    return registration_state().provider


def is_reference(value):
    if registration_state().provider is not None:
        return registration_state().provider.is_reference(value)
    # A persisted reference must never be sent as a literal bearer credential.
    return isinstance(value, str) and value.startswith("desktop-capability:")


def bind(reference, base_url):
    return require_provider().bind(reference, base_url)


def request_hook(reference, base_url):
    return require_provider().request_hook(reference, base_url)


def sync_client_kwargs(reference, base_url, *, timeout=600):
    if not is_reference(reference):
        return {}
    return require_provider().sync_client_kwargs(reference, base_url, timeout=timeout)


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    resolver: Callable | None = None
    provider: ModelCredentialProvider | None = None


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
