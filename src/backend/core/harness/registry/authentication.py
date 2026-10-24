"""Optional host authentication before the ordinary cookie/session resolver."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def register_host_authentication(
    claims: Callable[[Any], bool], resolver: Callable[[Any, Any], Any]
) -> None:
    """Install a host identity resolver.

    ``claims(request)`` must answer from the request alone, without I/O: it is
    asked on every authenticated request, and only a request it claims pays for
    ``resolver(request, db)`` (database work, run off the event loop).
    """
    registration_state().claims = claims
    registration_state().resolver = resolver


def host_user_resolver(request) -> Callable[[Any, Any], Any] | None:
    """The host resolver when one is registered and claims ``request``, else None."""
    state = registration_state()
    if state.resolver is None or state.claims is None or not state.claims(request):
        return None
    return state.resolver


@dataclass
class RegistrationState:
    claims: Callable[[Any], bool] | None = None
    resolver: Callable[[Any, Any], Any] | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
