"""Core sees client sessions only through its port; the integration plugs in at setup."""
import pytest

from core.harness.registry import client_sessions
from core.harness.registry.client_sessions import client_session_services


def test_without_a_provider_no_chat_is_leased(monkeypatch):
    monkeypatch.setattr(client_session_services(), "provider", None)
    assert client_sessions.admit("u", "chat", None) is None
    assert client_sessions.approval_mode("u", "chat") is None
    assert client_sessions.servers_for("u", "chat", "run") == {}
    # A lease id nobody can honour is refused, never silently treated as unleased.
    with pytest.raises(ValueError):
        client_sessions.admit("u", "chat", "0" * 32)
    with pytest.raises(ValueError):
        client_sessions.require("u", "chat", "0" * 32)


def test_setup_registers_the_lease_provider_and_its_route(monkeypatch):
    from integrations.agent_client import extension, registry

    routers = []
    monkeypatch.setattr(client_session_services(), "provider", None)
    monkeypatch.setattr("core.harness.registry.register_router",
                        lambda router, policy_key=None: routers.append(router))

    assert extension.setup()
    assert client_session_services().provider is registry
    assert [router.prefix for router in routers] == ["/v1/client-sessions"]
