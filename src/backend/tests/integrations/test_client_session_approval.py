"""A client-session lease, not the account preset, governs host work in a leased chat."""
import pytest

from core.harness.registry.client_sessions import client_session_services
from core.harness.tool_call_context import CURRENT_RUN_BINDING
from core.sandbox.permissions import approval
from core.sandbox.permissions.account import resolve_work_approval_mode
from integrations.agent_client import registry

CHAT = "lease-approval-chat"


@pytest.fixture
def leased(monkeypatch):
    """Account says full; the ACP session in CHAT asked for ask."""
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setattr(client_session_services(), "provider", registry)
    monkeypatch.setattr(approval, "resolve_approval_mode", lambda explicit, *, user_id: "full")
    lease_id = registry.create("u", CHAT, "/tmp", "ask", {})
    registry.bind_run("u", CHAT, lease_id, "run-leased", "ask")
    yield lease_id
    registry._entries.pop(CHAT, None)


def test_lease_overrides_account_by_chat_and_by_running_tool(leased):
    assert resolve_work_approval_mode("u", CHAT) == "ask"
    assert resolve_work_approval_mode("u") == "full"
    token = CURRENT_RUN_BINDING.set(("run-leased", "u"))
    try:
        assert resolve_work_approval_mode("u") == "ask"
    finally:
        CURRENT_RUN_BINDING.reset(token)
    # Another user's run never borrows this lease.
    token = CURRENT_RUN_BINDING.set(("run-leased", "v"))
    try:
        assert resolve_work_approval_mode("v") == "full"
    finally:
        CURRENT_RUN_BINDING.reset(token)


def test_revoked_lease_never_falls_back_to_account(leased):
    registry._entries[CHAT].revoked = True
    with pytest.raises(ValueError):
        resolve_work_approval_mode("u", CHAT)


def test_interactive_runtime_launch_uses_lease_mode(leased, monkeypatch, tmp_path):
    from core.capabilities.plugins.resources.confinement import launch_policy
    from core.sandbox.permissions.dispatch_models import LocalCommandAuthorization

    monkeypatch.setattr("core.sandbox.workspace.resolution.workspace_directory", lambda chat: str(tmp_path))
    monkeypatch.setattr(LocalCommandAuthorization, "confine", lambda self: self)
    provider = type("Provider", (), {"runs_on_host": True})()

    authorization = launch_policy(provider, "u", CHAT)

    assert authorization.approval_mode == "ask"
    assert not authorization.access.unconfined


def test_session_file_reference_uses_lease_mode(leased, monkeypatch, tmp_path):
    from core.files.artifacts.local_reference import resolve_session_reference

    (tmp_path / "a.txt").write_text("x")
    seen = []
    monkeypatch.setattr("core.sandbox.workspace.resolution.workspace_directory", lambda chat: str(tmp_path))
    monkeypatch.setattr("core.harness.registry.local_files.policy_for_gate", lambda mode: seen.append(mode) or mode)
    monkeypatch.setattr("core.harness.registry.local_files.grants_for_gate", lambda: ())
    monkeypatch.setattr("core.sandbox.permissions.evaluator.evaluate_local_path",
                        lambda *a, **k: type("Verdict", (), {"decision": "allow"})())
    item = {"file_id": "lpf_x", "metadata": {"reference_kind": "session", "user_id": "u", "session_id": CHAT,
                                             "session_root": str(tmp_path.resolve()), "relative_path": "a.txt"}}

    assert resolve_session_reference(item) is not None
    assert seen == ["ask"]
    registry._entries[CHAT].revoked = True
    assert resolve_session_reference(item) is None
