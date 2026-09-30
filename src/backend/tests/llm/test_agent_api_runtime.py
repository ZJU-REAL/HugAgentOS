"""API execution must not inherit the account's private runtime context."""
import hashlib
import pytest

from core.llm.agent_api_runtime import apply_api_scope, parse_api_scope


def scope(**updates):
    value = {
        "version": 1, "owner_user_id": "owner", "api_key_id": "ak_123",
        "agent_id": "agent_123", "chat_id": "api_chat",
        "sandbox_user_id": "api_ak_123", "sandbox_session_id": "api_" + hashlib.sha256(b"ak_123:api_chat").hexdigest()[:40],
    }
    return {**value, **updates}


def test_api_execution_removes_private_context():
    context = {
        "user_id": "owner", "chat_id": "api_chat", "agent_id": "agent_123",
        "direct_agent_id": "agent_123", "agent_api_scope": scope(),
        "memory_enabled": True, "memory_write_enabled": True,
        "memory_scope_user_id": "owner", "project_id": "private-project",
        "channel_origin": {"channel_id": "private-channel"},
        "visible_subagents": [{"agent_id": "other"}],
    }
    result = apply_api_scope(context)
    assert result["memory_enabled"] is False
    assert result["memory_write_enabled"] is False
    assert result["memory_scope_user_id"] == "api_ak_123"
    assert result["project_id"] is None
    assert result["channel_origin"] is None
    assert result["visible_subagents"] == []
    assert context["memory_enabled"] is True


@pytest.mark.parametrize("updates", [
    {"owner_user_id": "intruder"}, {"chat_id": "other"},
    {"agent_id": "other"}, {"sandbox_user_id": "owner"},
    {"version": 2}, {"sandbox_session_id": "../private"},
])
def test_tampered_api_scope_fails_closed(updates):
    with pytest.raises(ValueError):
        parse_api_scope(scope(**updates), owner_user_id="owner",
                        chat_id="api_chat", agent_id="agent_123")


class Collector:
    def __init__(self):
        self.tools = {}

    def register_tool_function(self, func, **kwargs):
        self.tools[func.__name__] = func


@pytest.mark.asyncio
async def test_api_bash_never_uses_account_identity_or_shared_process_runner(monkeypatch):
    import json
    import core.sandbox
    from core.llm.agent_api_tools import register_agent_api_tools

    class Provider:
        name = "script_runner"
        runs_on_host = False
        calls = []

        async def start_process(self, req, yield_time_ms):
            self.calls.append(req)
            return {"stdout": req.user_id, "exit_code": 0}

    provider = Provider()
    monkeypatch.setattr(core.sandbox, "get_sandbox_provider", lambda: provider)
    collector = Collector()
    register_agent_api_tools(
        collector, parse_api_scope(scope(), owner_user_id="owner",
            chat_id="api_chat", agent_id="agent_123"),
    )
    result = await collector.tools["Bash"]("pwd")
    assert "隔离" in json.loads(result.content[0].text)["error"]
    assert provider.calls == []
    provider.name = "opensandbox"
    result = await collector.tools["Bash"]("pwd")
    assert json.loads(result.content[0].text)["stdout"] == "api_ak_123"
    assert provider.calls[0].session_id == scope()["sandbox_session_id"]
    assert "read_chat" not in collector.tools
    assert "list_related_chats" not in collector.tools


def test_mcp_scope_replaces_identity_and_preserves_exact_kb_grants():
    from core.llm.agent_api_runtime import scope_mcp_servers
    parsed = parse_api_scope(scope(), owner_user_id="owner", chat_id="api_chat", agent_id="agent_123")
    config = {"kb": {"url": "http://kb", "headers": {
        "x-current-user-id": "owner", "X-Chat-Id": "private", "X-Channel-Id": "secret",
        "X-Allowed-Kb-Ids": "kb_selected", "X-Allowed-Dataset-Ids": "",
    }}}
    headers = scope_mcp_servers(config, parsed)["kb"]["headers"]
    assert headers["X-Current-User-Id"] == "api_ak_123"
    assert headers["X-Chat-Id"] == "api_chat"
    assert "X-Channel-Id" not in headers
    assert headers["X-Allowed-Kb-Ids"] == "kb_selected"
    assert headers["X-Allowed-Dataset-Ids"] == "__agent_api_no_kb__"
    assert config["kb"]["headers"]["x-current-user-id"] == "owner"
    with pytest.raises(ValueError, match="stdio"):
        scope_mcp_servers({"host": {"command": "python"}}, parsed)


@pytest.mark.asyncio
async def test_pin_rejects_private_artifact_and_only_accepts_file_ids(monkeypatch):
    import inspect
    import json
    import core.llm.agent_api_tools as tools
    from core.llm.tool_collector import ToolCollector
    parsed = parse_api_scope(scope(), owner_user_id="owner", chat_id="api_chat", agent_id="agent_123")
    collector = ToolCollector()
    tools.register_agent_api_tools(collector, parsed)
    assert all(collector.get_tool(name) is not None for name in (
        "Bash", "write_stdin", "read_artifact", "sandbox_get_artifact", "pin_to_workspace",
    ))
    pin = collector.get_tool("pin_to_workspace")._func
    assert "file_paths" not in inspect.signature(pin).parameters
    monkeypatch.setattr(tools, "artifact_in_scope", lambda *_: False)
    result = await pin(["private"])
    assert "当前 API 会话" in json.loads(result.content[0].text)["error"]


@pytest.fixture
def artifact_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.db.engine import Base
    from core.db.models import Artifact, ChatSession, UserShadow, UserFolder
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (Artifact, ChatSession, UserShadow, UserFolder)])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="Owner"))
        db.add(ChatSession(
            chat_id="api_chat", user_id="owner", title="API",
            extra_data={"agent_id": "agent_123", "agent_api_scope": scope()},
        ))
        db.add(Artifact(
            artifact_id="output", user_id="owner", chat_id="api_chat", type="other", title="Output",
            filename="output.txt", size_bytes=10, mime_type="text/plain", storage_key="output.txt",
        ))
        db.commit()
    yield factory
    engine.dispose()


def test_artifacts_require_the_same_session_provenance_and_respect_deletion(artifact_db):
    from datetime import datetime, timezone
    from core.db.models import Artifact, ChatSession
    from core.llm.agent_api_runtime import artifact_in_scope
    parsed = parse_api_scope(scope(), owner_user_id="owner", chat_id="api_chat", agent_id="agent_123")
    assert artifact_in_scope("output", parsed)
    with artifact_db() as db:
        db.get(Artifact, "output").deleted_at = datetime.now(timezone.utc)
        db.commit()
    assert not artifact_in_scope("output", parsed)
    with artifact_db() as db:
        db.get(Artifact, "output").deleted_at = None
        db.get(ChatSession, "api_chat").extra_data = {"agent_id": "agent_123"}
        db.commit()
    assert not artifact_in_scope("output", parsed)


def test_pending_output_requires_trusted_metadata_before_store_resolution(artifact_db, monkeypatch):
    from core.llm.agent_api_runtime import artifact_in_scope
    parsed = parse_api_scope(scope(), owner_user_id="owner", chat_id="api_chat", agent_id="agent_123")
    metadata = {
        "source": "sandbox_get_artifact", "user_id": "owner",
        "chat_id": "api_chat", "agent_api_key_id": "ak_123",
    }
    monkeypatch.setattr("core.artifacts.store._read_record", lambda _: {"metadata": metadata})
    assert artifact_in_scope("pending", parsed)
    metadata["agent_api_key_id"] = "another_key"
    assert not artifact_in_scope("pending", parsed)
    assert not artifact_in_scope("../private", parsed)


@pytest.mark.asyncio
async def test_selected_skills_are_staged_once_without_other_owner_packages(tmp_path):
    from core.llm.agent_api_skills import ApiSkillStager
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "SKILL.md").write_text("selected skill")
    (tmp_path / "private.txt").write_text("private")
    parsed = parse_api_scope(scope(), owner_user_id="owner", chat_id="api_chat", agent_id="agent_123")
    class Provider:
        def __init__(self):
            self.calls = []
        async def put_file(self, session, path, content, user_id):
            self.calls.append((session, path, content, user_id))
    provider = Provider()
    stager = ApiSkillStager({"selected": str(selected)})
    await stager.stage(provider, parsed)
    await stager.stage(provider, parsed)
    assert provider.calls == [(scope()["sandbox_session_id"], "/workspace/skills/selected/SKILL.md",
                               b"selected skill", "api_ak_123")]


def test_skill_staging_rejects_escaping_packages(tmp_path):
    from core.llm.agent_api_skills import _skill_files
    package = tmp_path / "selected"
    package.mkdir()
    (tmp_path / "private").write_text("secret")
    (package / "leak").symlink_to(tmp_path / "private")
    with pytest.raises(ValueError, match="软链接"):
        _skill_files({"selected": str(package)})
    with pytest.raises(ValueError, match="标识"):
        _skill_files({"../private": str(package)})


@pytest.mark.asyncio
async def test_api_sandbox_never_uses_general_pool_or_host_mounts(monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import core.sandbox._opensandbox_session as sessions

    user_pool = SimpleNamespace(acquire=AsyncMock(return_value=SimpleNamespace(id="api-container")))
    general_pool = SimpleNamespace(acquire=AsyncMock())
    host = SimpleNamespace(_jupyter_user_pool=user_pool, _pool=general_pool)
    monkeypatch.setattr(sessions, "_user_bound_sandbox_required", lambda: False)
    result = await sessions._OpenSandboxSessionMixin._create_session(host, "api_ak_123")
    assert result.user_id == "api_ak_123"
    user_pool.acquire.assert_awaited_once_with("api_ak_123")
    general_pool.acquire.assert_not_awaited()

    def reject_mount(*args):
        raise AssertionError("API container tried to mount host data")
    for name in ("_make_skills_volumes", "_make_myspace_volume", "_make_dws_creds_volumes",
                 "_make_lark_creds_volumes", "_make_email_creds_volumes", "_make_yida_creds_volumes"):
        monkeypatch.setattr(sessions, name, reject_mount)
    create = AsyncMock(return_value=SimpleNamespace(id="new-container"))
    monkeypatch.setitem(sys.modules, "opensandbox", SimpleNamespace(Sandbox=SimpleNamespace(create=create)))
    host = SimpleNamespace(
        _make_config=lambda: None, _ttl_s=60, _image="image",
        _repoint_execd_direct=AsyncMock(),
    )
    await sessions._OpenSandboxSessionMixin._create_jupyter_sandbox(host, user_id="api_ak_123")
    assert create.call_args.kwargs["volumes"] is None


@pytest.mark.asyncio
async def test_generated_output_is_pinned_and_downloadable_only_with_its_key(artifact_db, monkeypatch):
    import json
    from types import SimpleNamespace
    from starlette.requests import Request
    import core.sandbox
    from core.auth.agent_api_scope import enforce_agent_api_route
    from core.db.models import Artifact
    from core.llm import workspace
    from core.llm.agent_api_tools import register_agent_api_tools

    records = {}
    class Provider:
        name = "opensandbox"
        runs_on_host = False
        async def get_file_to_path(self, session, src, dest, *, max_bytes, user_id):
            assert session == scope()["sandbox_session_id"] and user_id == "api_ak_123"
            dest.write_bytes(b"api output")
            return 10

    def save(path, *, name, mime_type, user_id, source, extra_metadata):
        assert path.read_bytes() == b"api output"
        item = {
            "file_id": "new_output", "name": name, "mime_type": mime_type,
            "size": 10, "storage_key": "test/output",
            "metadata": {"source": source, "user_id": user_id, **extra_metadata},
        }
        records["new_output"] = item
        return {key: value for key, value in item.items() if key != "metadata"}

    monkeypatch.setattr(core.sandbox, "get_sandbox_provider", lambda: Provider())
    monkeypatch.setattr("core.llm.tools._tool_helpers._store_generated_file_path", save)
    monkeypatch.setattr("core.artifacts.store._read_record", records.get)
    monkeypatch.setattr("core.artifacts.store.get_artifact", records.get)
    monkeypatch.setattr("core.services.artifact_service.resolve_artifact_storage_key", lambda fid, key: key)
    parsed = parse_api_scope(scope(), owner_user_id="owner", chat_id="api_chat", agent_id="agent_123")
    collector = Collector()
    register_agent_api_tools(collector, parsed)
    saved = await collector.tools["sandbox_get_artifact"]("/workspace/new-output.txt")
    assert json.loads(saved.content[0].text)["file_id"] == "new_output"
    with workspace.scope():
        pinned = await collector.tools["pin_to_workspace"](["new_output"])
        assert json.loads(pinned.content[0].text)["ok"]
        assert workspace.get_pinned_file_ids() == ["new_output"]
    request = Request({"type": "http", "method": "GET", "path": "/files/new_output",
                       "headers": [], "query_string": b"", "server": ("test", 80), "scheme": "http"})
    user = SimpleNamespace(user_id="owner", api_key_id="ak_123", api_key_agent_id="agent_123")
    with artifact_db() as db:
        assert db.get(Artifact, "new_output").extra_data["agent_api_key_id"] == "ak_123"
        enforce_agent_api_route(request, db, user)
        user.api_key_id = "other_key"
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            enforce_agent_api_route(request, db, user)
