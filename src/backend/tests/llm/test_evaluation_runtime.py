"""Evaluation sessions keep native tools but never inherit private account state."""
import json

import pytest

from core.llm.evaluation_runtime import apply_evaluation_scope, parse_evaluation_scope


SESSION = "eval_" + "a" * 32


def test_scope_removes_account_context_without_mutating_input():
    context = {
        "chat_id": SESSION, "user_id": "owner", "memory_enabled": True,
        "memory_write_enabled": True, "project_id": "private",
        "channel_origin": {"channel_id": "private"}, "enabled_mcps": ["private"],
        "enabled_skills": ["private"], "enabled_kbs": ["private"],
        "bridge_tools": [{"name": "private"}], "uploaded_files": [{"id": "private"}],
    }
    result = apply_evaluation_scope(context)
    assert result["sandbox_session_id"] == SESSION
    assert result["memory_enabled"] is result["memory_write_enabled"] is False
    assert result["project_id"] is result["channel_origin"] is None
    for key in ("enabled_mcps", "enabled_skills", "enabled_kbs", "bridge_tools", "uploaded_files"):
        assert result[key] == []
    assert context["memory_enabled"] is True


@pytest.mark.parametrize("chat_id, session_id, owner", [
    ("eval_bad", None, "owner"), (SESSION, "normal", "owner"),
    (SESSION, "eval_" + "b" * 32, "owner"), (SESSION, None, ""),
])
def test_malformed_or_conflicting_eval_session_is_rejected(chat_id, session_id, owner):
    with pytest.raises(ValueError):
        parse_evaluation_scope(owner_user_id=owner, chat_id=chat_id, session_id=session_id)


def test_child_without_chat_inherits_the_same_eval_session():
    scope = parse_evaluation_scope(owner_user_id="owner", chat_id=None, session_id=SESSION)
    assert scope.session_id == SESSION
    assert scope.owner_user_id == "owner"


def test_ordinary_context_is_unchanged():
    context = {"chat_id": "normal", "user_id": "owner", "memory_enabled": True}
    assert apply_evaluation_scope(context) is context


def test_bound_agent_projection_does_not_mutate_its_definition():
    from core.db.models import UserAgent
    from core.llm.evaluation_runtime import evaluation_user_agent
    agent = UserAgent(agent_id="ua", name="Agent", system_prompt="task prompt",
                      mcp_server_ids=["private"], skill_ids=["private"],
                      kb_ids=["private"], plugin_ids=["private"])
    projected = evaluation_user_agent(agent)
    assert projected.system_prompt == "task prompt"
    assert projected.mcp_server_ids == []
    assert agent.mcp_server_ids == ["private"]


def test_api_and_eval_scope_cannot_be_combined():
    with pytest.raises(ValueError):
        apply_evaluation_scope({"user_id": "owner", "chat_id": SESSION,
                                "agent_api_scope": {"version": 1}})


class Collector:
    def __init__(self):
        self.tools = {}

    def register_tool_function(self, func, **kwargs):
        self.tools[func.__name__] = func


@pytest.fixture
def evaluation_tools(monkeypatch):
    import core.sandbox
    from core.llm.evaluation_tools import register_evaluation_tools
    from core.sandbox import SandboxError

    class Provider:
        name = "opensandbox"
        def __init__(self):
            self.calls = []
            self.files = {"/app/source.txt": b"before\n"}

        async def get_file(self, session_id, path, user_id):
            self.calls.append(("read", session_id, path, user_id))
            if path not in self.files:
                raise SandboxError("not found")
            return self.files[path]

        async def put_file(self, session_id, path, data, user_id):
            self.calls.append(("write", session_id, path, user_id))
            self.files[path] = data

        async def start_process(self, req, yield_time_ms):
            self.calls.append(("bash", req.session_id, req.script_content, req.user_id))
            return {"stdout": "sandbox", "exit_code": 0, "status": "exited"}

        async def run_to_completion(self, req):
            self.calls.append(("exec", req.session_id, req.script_content, req.user_id))
            from types import SimpleNamespace
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    provider = Provider()
    monkeypatch.setattr(core.sandbox, "get_sandbox_provider", lambda: provider)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    collector = Collector()
    scope = parse_evaluation_scope(owner_user_id="owner", chat_id=SESSION, session_id=None)
    register_evaluation_tools(collector, scope)
    return collector.tools, provider


def payload(value):
    return json.loads(value.content[0].text)


@pytest.mark.asyncio
async def test_native_bash_skips_both_myspace_sync_directions(evaluation_tools, monkeypatch):
    tools, provider = evaluation_tools

    async def reject(*args, **kwargs):
        raise AssertionError("account myspace was accessed")

    monkeypatch.setattr("core.llm.tools.sandbox_tool._pull_myspace_updates", reject)
    monkeypatch.setattr("core.myspace.sandbox_sync.reflect_sandbox_myspace", reject)
    assert payload(await tools["bash"]("pwd"))["exit_code"] == 0
    assert provider.calls == [("bash", SESSION, "pwd", "owner")]
    assert set(tools) == {"bash", "Bash", "write_stdin", "Read", "Write", "Edit", "Glob", "Grep"}


@pytest.mark.asyncio
async def test_native_read_write_edit_use_only_bound_container(evaluation_tools):
    tools, provider = evaluation_tools
    assert "error" not in payload(await tools["Read"]("/app/source.txt"))
    assert "error" not in payload(await tools["Write"]("/app/source.txt", "after\n"))
    assert "error" not in payload(await tools["Read"]("/app/source.txt"))
    assert "error" not in payload(await tools["Edit"]("/app/source.txt", "after", "edited"))
    assert provider.files["/app/source.txt"] == b"edited\n"
    assert all(call[1] == SESSION and call[3] == "owner" for call in provider.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/myspace/private", "/workspace/myspace/owner/private", "/app/../myspace/private"])
async def test_private_paths_are_rejected_before_any_io(evaluation_tools, path):
    tools, provider = evaluation_tools
    assert "error" in payload(await tools["Read"](path))
    assert "error" in payload(await tools["Glob"]("*", path=path))
    assert provider.calls == []


@pytest.mark.asyncio
async def test_missing_file_never_recovers_account_artifacts(evaluation_tools, monkeypatch):
    tools, _ = evaluation_tools

    def reject(*args, **kwargs):
        raise AssertionError("account artifact fallback was accessed")

    monkeypatch.setattr("core.llm.tools.read_tool._fallback_recover_from_artifact", reject)
    monkeypatch.setattr("core.llm.tools.myspace_vfs.materialize_into_sandbox", reject)
    assert "error" in payload(await tools["Read"]("/app/missing"))


@pytest.mark.asyncio
async def test_artifact_registration_is_rejected_before_io(evaluation_tools):
    tools, provider = evaluation_tools
    assert "error" in payload(await tools["Write"]("/app/output", "secret", register_as_artifact=True))
    assert provider.calls == []


@pytest.mark.asyncio
async def test_paths_are_task_local_during_concurrent_calls_and_reset_afterward(monkeypatch):
    import asyncio
    from core.llm.evaluation_runtime import CURRENT_EVALUATION_SCOPE
    from core.llm.evaluation_tools import _EvaluationCollector
    from core.llm.tools._paths import validate_workspace_path
    collector = Collector()
    scope = parse_evaluation_scope(owner_user_id="owner", chat_id=SESSION)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe():
        entered.set()
        await release.wait()
        assert validate_workspace_path("/app/file") is None
        raise RuntimeError("intentional")

    _EvaluationCollector(collector, scope).register_tool_function(probe)
    task = asyncio.create_task(collector.tools["probe"]())
    await entered.wait()
    assert CURRENT_EVALUATION_SCOPE.get() is None
    assert validate_workspace_path("/app/file") is not None
    release.set()
    with pytest.raises(RuntimeError, match="intentional"):
        await task
    assert CURRENT_EVALUATION_SCOPE.get() is None


def test_readonly_surface_and_local_mode_fail_closed(monkeypatch):
    from core.llm.evaluation_tools import register_evaluation_tools
    collector = Collector()
    scope = parse_evaluation_scope(owner_user_id="owner", chat_id=SESSION)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    register_evaluation_tools(collector, scope, read_only=True)
    assert set(collector.tools) == {"Read", "Glob", "Grep"}
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    with pytest.raises(ValueError, match="container"):
        register_evaluation_tools(Collector(), scope)
