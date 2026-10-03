from tests.sandbox.runner_client import run_runner

"""Regressions for desktop identity, explicit bindings and immutable runs."""

import base64
import json

import pytest
from core.capabilities import registry, skills
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services import desktop_cloud_bundles as bundles
from core.services.desktop_capability_protocol import build_skill_manifest


from tests.capabilities.runtime_recovery_support import _ordered_skill_manifest, state, _zip, _intent, durable_index


def test_prepared_run_keeps_old_bytes_after_update_and_rebuild(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    first = skills.publish_local_skill(
        "example", files={"SKILL.md": "v1"}, content_hash=skill_content_hash("v1", {})
    )
    run = runtime.prepare("run-a", "user-a", skill_ids=["example"])
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v2"}, content_hash=skill_content_hash("v2", {})
    )
    replay = runtime.prepare("run-a", "user-a", skill_ids=["example"])
    assert (run.view_dir / "example" / "SKILL.md").read_text() == "v1"
    assert replay.bindings == run.bindings
    newer = runtime.prepare("run-b", "user-a", skill_ids=["example"])
    assert (newer.view_dir / "example" / "SKILL.md").read_text() == "v2"


def test_prepared_run_rejects_account_plane_and_integrity_changes(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.capabilities.errors import CapabilityError
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    original = skills.publish_local_skill(
        "example", files={"SKILL.md": "v1"}, content_hash=skill_content_hash("v1", {})
    )
    run = runtime.prepare("run-a", "user-a", skill_ids=["example"])
    with pytest.raises(CapabilityError):
        runtime.prepare("run-a", "user-b", skill_ids=["example"])
    with pytest.raises(CapabilityError):
        runtime.prepare("run-a", "user-a", skill_ids=["example"], execution_plane="cloud")
    original.entry_file.write_text("tampered")
    # 账号与执行面不符依旧整轮拒绝；单个组件被篡改只摘掉它自己。
    with pytest.raises(CapabilityError):
        runtime.validate(run, only_skill="example")
    runtime.rebuild(run)
    assert "example" in runtime.get("run-a").unavailable


def test_plugin_required_disabled_agent_blocks_optional_missing_skill_does_not(index_db, caps_root):
    from core.capabilities import agents, plugins

    agent = agents.publish_local_agent(
        {"agent_id": "writer", "name": "Writer", "is_enabled": False}
    )
    comp = plugins.publish_local_plugin(
        {
            "slug": "pack",
            "components": {"agents": ["writer"], "skills": [{"id": "optional", "required": False}]},
        },
        owner_user_id=None,
    )
    inst = registry.get("plugin:local:pack")
    status = plugins.readiness(inst, cloud_server_ids=[])
    assert status["missing_required"] == ["agent:local:writer"]
    assert not status["ready"]
    registry.set_enabled("agent:local:writer", True)
    assert plugins.readiness(inst, cloud_server_ids=[])["ready"]


def test_mcp_contract_and_source_are_pinned_across_replay(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    run = runtime.prepare("run-mcp", "u", skill_ids=[])
    v1 = {
        "search": {
            "transport": "streamable_http",
            "url": "https://cloud.example/gateway",
            "headers": {"Authorization": "secret-v1"},
            "manifest_tools": [{"name": "one"}],
            "schema_hash": "v1",
        }
    }
    runtime.bind_mcp(run, v1, None)
    v2 = {
        "search": {
            **v1["search"],
            "headers": {"Authorization": "secret-v2"},
            "manifest_tools": [{"name": "two"}],
            "schema_hash": "v2",
        }
    }
    restored = runtime.bind_mcp(run, v2, None)
    assert restored["search"]["manifest_tools"] == [{"name": "one"}]
    assert restored["search"]["headers"] == {"Authorization": "secret-v2"}
    assert "secret" not in json.dumps(runtime.get("run-mcp").to_dict())
    # 端点换了就不再把它当成原来那个工具接上去：这一个连接器被摘掉，其余不受影响。
    changed = runtime.bind_mcp(run, {"search": {**v2["search"], "url": "http://different"}}, None)
    assert "search" not in changed
    assert runtime.get("run-mcp").unavailable["mcp:search"] == "connector_changed"


def test_agent_definition_is_pinned_on_replay(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime
    from core.capabilities.agents import AgentDefinition

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    old = AgentDefinition(agent_id="writer", name="Writer", system_prompt="v1", skill_ids=["first"])
    newer = AgentDefinition(
        agent_id="writer", name="Writer", system_prompt="v2", skill_ids=["second"]
    )
    runtime.pin_agent_definition("run-a", "u", old)
    replay = runtime.pin_agent_definition("run-a", "u", newer)
    assert replay.system_prompt == "v1" and replay.skill_ids == ["first"]


@pytest.mark.asyncio
async def test_runner_executes_frozen_view_after_background_update(
    durable_index, caps_root, tmp_path, monkeypatch
):
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash
    from services.script_runner_service import server

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v1"}, content_hash=skill_content_hash("v1", {})
    )
    run = runtime.prepare("run-exec", "u", skill_ids=["example"])
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v2"}, content_hash=skill_content_hash("v2", {})
    )
    response = await run_runner(
        server.ProcessRequest(
            script_content=f"cat {run.view_dir}/example/SKILL.md",
            script_name="frozen.sh",
            language="bash",
            session_id="chat-a",
            user_id="u",
            capability_view_key=run.view_dir.parent.name,
        )
    )
    assert response.exit_code == 0 and response.stdout.strip() == "v1"


@pytest.mark.asyncio
async def test_skill_reader_uses_frozen_loader_and_cannot_read_other_profile(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.llm.tools.skill_tool import register_sandboxed_view_text_file

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    body = "---\nname: example\ndescription: sample\n---\nv1"
    skills.publish_local_skill(
        "example", files={"SKILL.md": body}, content_hash=skill_content_hash(body, {})
    )
    run = runtime.prepare("run-reader", "u", skill_ids=["example"])
    loader = runtime.frozen_loader(run)

    class Toolkit:
        def register_tool_function(self, fn, **_):
            self.read = fn

    toolkit = Toolkit()
    loaded = set()
    register_sandboxed_view_text_file(
        toolkit, [str((run.view_dir / "example").resolve())], loader, loaded_skill_ids=loaded
    )
    response = await toolkit.read("/workspace/skills/example/SKILL.md")
    assert body in response.content[0].text and loaded == {"example"}
    outside = caps_root / "other-account.txt"
    outside.write_text("private")
    denied = await toolkit.read(str(outside))
    assert "Access denied" in denied.content[0].text and "private" not in denied.content[0].text


def test_bridge_token_is_memory_only_and_session_bound(durable_index, caps_root, monkeypatch):
    import time
    from core.db.models import ContentBlock
    from core.capabilities.errors import CloudUnavailable

    monkeypatch.setattr(bridge, "_refresh_manifest_async", lambda **_: None)
    monkeypatch.setattr(bridge, "_rebuild_identity_views", lambda: None)
    monkeypatch.setattr("core.db.engine.SessionLocal", durable_index)
    monkeypatch.setattr("core.services.desktop_model_credentials.scrub_legacy_rows", lambda: None)
    with durable_index() as db:
        db.add(ContentBlock(id=bridge.BRIDGE_BLOCK_ID, payload={"token": "old-secret"}))
        db.commit()

    def token(epoch, nonce):
        claims = {
            "u": "a",
            "c": "stable-a",
            "a": epoch,
            "h": "session-" + str(epoch),
            "d": "device-a",
            "n": nonce,
            "e": int(time.time()) + 600,
        }
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return "dcap2." + body + ".test-signature"

    bridge.set_state("https://cloud.example", token(1, "one"), 600, device_id="device-a")
    captured = bridge.get_state()
    with durable_index() as db:
        assert db.get(ContentBlock, bridge.BRIDGE_BLOCK_ID) is None
    assert bridge.cloud_headers(captured)["X-Desktop-Device-Id"] == "device-a"
    bridge.set_state("https://cloud.example", token(1, "two"), 600, device_id="device-a")
    bridge.require_current_account(captured)
    bridge.set_state("https://cloud.example", token(2, "three"), 600, device_id="device-a")
    with pytest.raises(CloudUnavailable):
        bridge.require_current_account(captured)
    bridge.clear_state()
    assert bridge.get_state() is None
    bridge._state_loaded = False
    assert bridge.get_state() is None


def test_stale_mcp_error_does_not_replace_current_account_status(index_db, caps_root, monkeypatch):
    a, b = state("a"), state("b")
    current = [a]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(bridge, "_refresh_manifest_async", lambda **_: None)

    def fetch(*_, **__):
        current[0] = b
        bridge._manifest_error = None
        raise RuntimeError("old account offline")

    monkeypatch.setattr("httpx.get", fetch)
    bridge._fetch_manifest_blocking(a)
    assert bridge._manifest_error is None


def test_cloud_removal_revokes_access_but_retains_history_bytes(index_db, caps_root, monkeypatch):
    from core.capabilities import store

    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    saved = registry.get(inst.install_id)
    cloud_skills._reconcile_intent(_ordered_skill_manifest(st, build_skill_manifest([])), st)
    assert registry.get(inst.install_id).state == "removed"
    assert (
        store.get(
            "skill", inst.profile_id, inst.key, saved.resolved_revision
        ).entry_file.read_text()
        == "v1"
    )


def test_audit_references_the_frozen_run_after_live_update(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.evolution.runtime_binding import _skill_refs

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v1"}, content_hash=skill_content_hash("v1", {})
    )
    frozen = runtime.prepare("run-audit", "u", skill_ids=["example"])
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v2"}, content_hash=skill_content_hash("v2", {})
    )
    ref = _skill_refs(["example"], "run-audit")[0]
    assert ref.detail["binding"]["revision"] == frozen.bindings["example"]["revision"]
    assert ref.version == frozen.bindings["example"]["revision"]
