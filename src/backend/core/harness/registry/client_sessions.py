"""Client-session port: an external agent client's lease over one local chat.

While a client (ACP) leases a chat, the lease rather than the account decides
the approval preset, and the client's own MCP servers join that chat's runs.
The lease state lives in the owning integration; with nothing registered no
chat is ever leased, so a lease id from a request can never be honoured.
Every lookup on a revoked or expired lease raises ValueError: work it once
owned must not fall back to the account's authority.
"""
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol


class ClientSessions(Protocol):
    def admit(self, user: str, chat: str, lease_id: str | None) -> Any | None: ...

    def bind_run(self, user: str, chat: str, lease_id: str, run_id: str, mode: str) -> None: ...

    def require(self, user: str, chat: str, lease_id: str) -> Any: ...

    def approval_mode(self, user: str, chat: str | None = None, run_id: str | None = None) -> str | None: ...

    def servers_for(self, user: str, chat: str, run_id: str | None) -> dict: ...


@dataclass
class _Registration:
    provider: ClientSessions | None = None


@lru_cache(maxsize=1)
def client_session_services() -> _Registration:
    return _Registration()


def register_client_sessions(provider: ClientSessions) -> None:
    client_session_services().provider = provider


def admit(user: str, chat: str, lease_id: str | None):
    """The lease a request runs under, None when the chat is not leased."""
    if client_session_services().provider is None and lease_id is None:
        return None
    return _require_provider().admit(user, chat, lease_id)


def bind_run(user: str, chat: str, lease_id: str, run_id: str, mode: str) -> None:
    _require_provider().bind_run(user, chat, lease_id, run_id, mode)


def require(user: str, chat: str, lease_id: str):
    return _require_provider().require(user, chat, lease_id)


def approval_mode(user: str, chat: str | None = None, run_id: str | None = None) -> str | None:
    provider = client_session_services().provider
    return provider.approval_mode(user, chat, run_id) if provider else None


def servers_for(user: str, chat: str, run_id: str | None) -> dict:
    provider = client_session_services().provider
    return provider.servers_for(user, chat, run_id) if provider else {}


def _require_provider() -> ClientSessions:
    provider = client_session_services().provider
    if provider is None:
        raise ValueError("Client sessions are unavailable")
    return provider
