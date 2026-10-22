from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from core.harness.registry import registry, SandboxCredentials, register_sandbox_credentials
from core.sandbox import credential_dispatch as dispatch
from integrations import register_credentials as register_defaults


@pytest.fixture(autouse=True)
def isolate_registry(monkeypatch):
    monkeypatch.setattr(registry.registration_state(), 'sandbox_credentials', {})


def test_environment_and_mount_order_preserve_registered_contributions():
    register_sandbox_credentials(SandboxCredentials("one", lambda: {"A": "1"}, lambda user: [(user, "one")]))
    register_sandbox_credentials(SandboxCredentials("two", lambda: {"B": "2"}, lambda user: [(user, "two")]))
    assert dispatch.credential_environment() == {"A": "1", "B": "2"}
    assert dispatch.credential_mounts("owner") == [("owner", "one"), ("owner", "two")]


@pytest.mark.asyncio
async def test_builtin_command_order_identity_and_writeback_are_preserved():
    register_defaults()
    names = ("_ensure_dingtalk_creds", "_ensure_email_creds", "_ensure_lark_creds", "_ensure_yida_creds", "_persist_yida_state")
    calls = []
    def callback(name):
        async def run(sandbox, user):
            calls.append((name, sandbox, user))
        return run
    states = {owner: SimpleNamespace(ensure=callback(names[index])) for owner, index in (("dingtalk", 0), ("email", 1), ("lark", 2), ("yida", 3))}
    states["yida"].persist = callback(names[4])
    host = SimpleNamespace(credential_state=lambda name, factory: states[name])
    sandbox = object()
    script = "dws himalaya lark-cli openyida"
    await dispatch.prepare_credentials(host, sandbox, "owner", script)
    await dispatch.persist_credentials(host, sandbox, "owner", script)
    assert calls == [(name, sandbox, "owner") for name in names]
    calls.clear()
    await dispatch.prepare_credentials(host, sandbox, None, script)
    await dispatch.persist_credentials(host, sandbox, None, script)
    await dispatch.prepare_credentials(host, sandbox, "owner", "echo hello")
    await dispatch.persist_credentials(host, sandbox, "owner", "echo hello")
    assert calls == []
