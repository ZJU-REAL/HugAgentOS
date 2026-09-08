"""Child capability scopes retain their own execution view and audit references."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core.capabilities import runtime
from core.capabilities.errors import IntegrityFailed
from core.evolution import runtime_binding as audit
from core.evolution.contract import ASSET_SKILL, AssetRef
from core.sandbox.protocol import ExecuteRequest
from core.sandbox.script_runner_provider import ScriptRunnerProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "child:one"])
async def test_runner_uses_scope_for_view_and_completion_checkpoint(monkeypatch, scope):
    calls = []
    prepared = SimpleNamespace(run_id="run-1", scope_id=scope)
    view_key = "root-view" if not scope else "child-view"
    monkeypatch.setattr("core.capabilities.paths.capabilities_enabled", lambda: True)

    def view(run_id, user_id, scope_id=""):
        calls.append(("view", run_id, user_id, scope_id))
        return Path("/synthetic/views") / view_key / "skills"

    def get(run_id, scope_id=""):
        calls.append(("get", run_id, scope_id))
        assert scope_id == scope
        return prepared

    def validate(run, *, user_id):
        assert run is prepared
        calls.append(("validate", run.scope_id, user_id))

    def respond(request):
        body = json.loads(request.content)
        assert body["capability_view_key"] == view_key
        assert body["session_id"] == "chat-1"
        return httpx.Response(
            200, json={"stdout": "ok", "stderr": "", "exit_code": 0, "execution_time_ms": 1}
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(runtime, "view_for_execution", view)
    monkeypatch.setattr(runtime, "get", get)
    monkeypatch.setattr(runtime, "validate", validate)
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    result = await ScriptRunnerProvider().execute(
        ExecuteRequest(
            "echo ok",
            "scope.sh",
            user_id="user-1",
            session_id="chat-1",
            capability_run_id="run-1",
            capability_scope=scope,
        )
    )
    assert result.stdout == "ok"
    assert calls == [
        ("view", "run-1", "user-1", scope),
        ("get", "run-1", scope),
        ("validate", scope, "user-1"),
    ]


@pytest.mark.asyncio
async def test_missing_child_snapshot_does_not_fall_back_to_root(monkeypatch):
    monkeypatch.setattr("core.capabilities.paths.capabilities_enabled", lambda: True)

    def view(run_id, user_id, scope_id=""):
        return None if scope_id else Path("/synthetic/root/skills")

    monkeypatch.setattr(runtime, "view_for_execution", view)

    def no_client(**kw):
        raise AssertionError("missing child scope must stop before the runner request")

    monkeypatch.setattr("core.sandbox.script_runner_provider.httpx.AsyncClient", no_client)
    with pytest.raises(IntegrityFailed, match="prepared run is missing"):
        await ScriptRunnerProvider().execute(
            ExecuteRequest(
                "echo ok",
                "scope.sh",
                user_id="user-1",
                capability_run_id="run-1",
                capability_scope="missing-child",
            )
        )


@pytest.mark.asyncio
async def test_bash_preserves_prepared_scope(monkeypatch):
    from core.llm.tools import sandbox_tool

    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    captured = []

    async def execute(req):
        captured.append(req)
        return SimpleNamespace(stdout="ok", stderr="", exit_code=0, execution_time_ms=1, files=[])

    monkeypatch.setattr(
        "core.sandbox.get_sandbox_provider", lambda: SimpleNamespace(execute=execute)
    )

    class Toolkit:
        def register_tool_function(self, fn, **kwargs):
            self.fn = fn

    toolkit = Toolkit()
    loader = SimpleNamespace(capability_run=SimpleNamespace(run_id="run-1", scope_id="step:two"))
    sandbox_tool.register_bash(toolkit, loader=loader, loaded_skill_ids=set(), chat_id="chat-1")
    await toolkit.fn(command="echo ok")
    assert captured[0].capability_run_id == "run-1"
    assert captured[0].capability_scope == "step:two"
    assert captured[0].session_id == "chat-1"


def test_skill_audit_looks_up_the_exact_scope(monkeypatch):
    monkeypatch.setattr("core.capabilities.paths.capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        "core.agent_skills.loader.get_skill_loader",
        lambda: SimpleNamespace(load_all_metadata=lambda: {}),
    )
    seen = []

    def get(run_id, scope_id=""):
        seen.append((run_id, scope_id))
        return SimpleNamespace(
            execution_plane="local",
            bindings={
                "example": {
                    "install_id": "skill:local:example",
                    "revision": "child-revision" if scope_id else "root-revision",
                }
            },
        )

    monkeypatch.setattr(runtime, "get", get)
    refs = audit._skill_refs(["example"], "run-1", capability_scope="child:one")
    assert seen == [("run-1", "child:one")]
    assert refs[0].version == "child-revision"
    assert refs[0].detail["binding"]["capability_scope"] == "child:one"


def test_child_audit_rebind_and_clear_preserve_parent_bundle(monkeypatch):
    audit.reset_for_tests()
    for name in (
        "_prompt_refs",
        "_ontology_refs",
        "_memory_refs",
        "_model_refs",
        "_kb_refs",
        "_workflow_refs",
    ):
        monkeypatch.setattr(audit, name, lambda *args: [])
    monkeypatch.setattr(
        audit,
        "_skill_refs",
        lambda ids, run_id, capability_scope="": [
            AssetRef(kind=ASSET_SKILL, asset_id="example", version=capability_scope or "root")
        ],
    )
    root = audit.bind_runtime_assets(run_id="run-1", skill_ids=["example"])
    child = audit.bind_runtime_assets(
        run_id="run-1", skill_ids=["example"], capability_scope="child:one"
    )
    assert audit.resolve_bundle_for_run("run-1").bundle_id == root.bundle_id
    assert (
        audit.resolve_bundle_for_run("run-1", capability_scope="child:one").bundle_id
        == child.bundle_id
    )
    rebound = audit.rebind_execution_manifest(
        run_id="run-1", base_bundle=child, execution_manifest={}, capability_scope="child:one"
    )
    assert (
        audit.resolve_bundle_for_run("run-1", capability_scope="child:one").bundle_id
        == rebound.bundle_id
    )
    assert audit.resolve_bundle_for_run("run-1").bundle_id == root.bundle_id
    audit.clear_run_binding("run-1", capability_scope="child:one")
    assert audit.resolve_bundle_for_run("run-1", capability_scope="child:one") is None
    assert audit.resolve_bundle_for_run("run-1").bundle_id == root.bundle_id
    audit.reset_for_tests()


@pytest.mark.asyncio
async def test_child_snapshot_disappearing_after_execution_fails_closed(monkeypatch):
    monkeypatch.setattr("core.capabilities.paths.capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        runtime,
        "view_for_execution",
        lambda run, user, scope_id="": Path("/synthetic/child/skills"),
    )
    seen = []

    def get(run_id, scope_id=""):
        seen.append((run_id, scope_id))
        return None

    monkeypatch.setattr(runtime, "get", get)
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        lambda **kw: client_class(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"stdout": "must not return", "exit_code": 0})
            ),
            **kw,
        ),
    )
    with pytest.raises(IntegrityFailed, match="prepared run is missing"):
        await ScriptRunnerProvider().execute(
            ExecuteRequest(
                "echo ok",
                "scope.sh",
                user_id="user-1",
                capability_run_id="run-1",
                capability_scope="child:one",
            )
        )
    assert seen == [("run-1", "child:one")]


@pytest.mark.asyncio
async def test_real_scoped_snapshots_reach_runner_and_audit_without_root_drift(
    index_db, caps_root, monkeypatch
):
    from core.capabilities import skills
    from core.db.models import ContentBlock
    from core.services.desktop_capability_protocol import skill_content_hash

    with index_db() as db:
        ContentBlock.__table__.create(db.get_bind(), checkfirst=True)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(
        "core.agent_skills.loader.get_skill_loader",
        lambda: SimpleNamespace(load_all_metadata=lambda: {}),
    )

    def publish(text):
        return skills.publish_local_skill(
            "scope-marker",
            files={"SKILL.md": text},
            content_hash=skill_content_hash(text, {}),
            owner_user_id="user-1",
        )

    publish("root revision")
    root = runtime.prepare("same-run", "user-1", skill_ids=["scope-marker"])
    root = runtime.preflight(root, skill_ids=["scope-marker"], available_models=set())
    publish("child revision")
    child = runtime.prepare("same-run", "user-1", skill_ids=["scope-marker"], scope_id="child:one")
    child = runtime.preflight(child, skill_ids=["scope-marker"], available_models=set())
    assert root.view_dir != child.view_dir
    assert root.bindings != child.bindings
    sessions = []

    def respond(request):
        body = json.loads(request.content)
        sessions.append(body["session_id"])
        view_dir = caps_root / ".capabilities" / "views" / body["capability_view_key"] / "skills"
        text = (view_dir / "scope-marker" / "SKILL.md").read_text()
        return httpx.Response(
            200, json={"stdout": text, "stderr": "", "exit_code": 0, "execution_time_ms": 1}
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    for scope, expected, snapshot in [
        ("child:one", "child revision", child),
        ("", "root revision", root),
    ]:
        result = await ScriptRunnerProvider().execute(
            ExecuteRequest(
                "cat /workspace/skills/scope-marker/SKILL.md",
                "scope.sh",
                user_id="user-1",
                session_id="shared-chat",
                capability_run_id="same-run",
                capability_scope=scope,
            )
        )
        assert result.stdout == expected
        reference = audit._skill_refs(["scope-marker"], "same-run", capability_scope=scope)[0]
        assert reference.version == snapshot.bindings["scope-marker"]["revision"]
        assert reference.detail["binding"]["capability_scope"] == scope
    assert sessions == ["shared-chat", "shared-chat"]
    assert runtime.get("same-run").dependency_report == root.dependency_report
