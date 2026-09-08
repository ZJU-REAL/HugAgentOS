"""Regressions for desktop identity, explicit bindings and immutable runs."""

import base64
import json

import pytest
from core.capabilities import registry, skills, manifest_order
from core.capabilities.ref import cloud_ref, profile_id
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services import desktop_cloud_bundles as bundles
from core.services.desktop_capability_protocol import build_skill_manifest, build_entity_manifest


def _ordered_skill_manifest(st, manifest):
    return manifest_order.stamp(manifest, manifest_order.begin("skill", cloud_skills._profile(st)))


def state(uid):
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"u": uid, "c": "center-" + uid, "a": 1, "h": "session-" + uid, "d": "device"}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return {"cloud_base": "https://cloud.example", "token": f"dcap2.{body}.sig"}


def test_explicit_local_binding_does_not_get_cloud_config(index_db, monkeypatch):
    st = state("a")
    profile = profile_id(st["cloud_base"], "a")
    ctx = {
        "state": st,
        "profile": profile,
        "manifest_revision": "r1",
        "servers": [
            {"server_id": "search", "component": "search", "tools": [], "schema_hash": "hash"}
        ],
    }
    monkeypatch.setattr(bridge, "_bridge_context", lambda: ctx)
    monkeypatch.setattr(bridge, "_local_server_base_map", lambda: {"search": "search"})
    monkeypatch.setattr(bridge, "_mcp_json_local_declarations", lambda: {})
    monkeypatch.setattr(bridge, "_mcp_json_local_configs", lambda: {})
    registry.set_preference("mcp", "search", "mcp:local:search")
    assert bridge.apply_to_enabled_mcp_ids(["search"]) == ["search"]
    assert "search" not in bridge.cloud_gateway_mcp_configs()


def test_stale_skill_response_cannot_repopulate_switched_account(index_db, caps_root, monkeypatch):
    a, b = state("a"), state("b")
    current = [a]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    manifest = build_skill_manifest([], ["private-a"])

    def fetch(_):
        current[0] = b
        cloud_skills.on_account_switch()
        return manifest

    monkeypatch.setattr(cloud_skills, "_fetch_manifest", fetch)
    monkeypatch.setattr(skills, "rebuild_views", lambda _: {})
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    cloud_skills.sync_blocking(a)
    assert cloud_skills.status()["revision"] == ""
    assert registry.list_installations() == []


def test_stale_agent_response_cannot_repopulate_switched_account(index_db, caps_root, monkeypatch):
    a, b = state("a"), state("b")
    current = [a]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    manifest = build_entity_manifest("agent", [])

    def fetch(*_):
        current[0] = b
        bundles.on_account_switch()
        return manifest

    monkeypatch.setattr(bundles, "_fetch", fetch)
    assert bundles.sync_kind("agent", a) is False
    assert bundles.status()["agent"]["revision"] == ""


def test_json_local_connector_is_enabled_without_cloud(index_db, monkeypatch):
    monkeypatch.setattr(bridge, "_bridge_context", lambda: None)
    monkeypatch.setattr(bridge, "_local_server_base_map", lambda: {})
    monkeypatch.setattr(
        bridge, "_mcp_json_local_declarations", lambda: {"my-files": {"enabled": True}}
    )
    assert bridge.apply_to_enabled_mcp_ids([]) == ["my-files"]


def _zip(files):
    import io, zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def _intent(monkeypatch, body="v1"):
    from core.services.desktop_capability_protocol import skill_content_hash

    st = state("a")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    profile = profile_id(st["cloud_base"], "a")
    inst = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], "skill", "example", scope="shared"),
        content_hash=skill_content_hash(body, {}),
    )
    return st, inst


def test_prepare_revalidates_existing_revision(index_db, caps_root, monkeypatch):
    from core.capabilities import store
    from core.capabilities.errors import IntegrityFailed
    from core.capabilities.paths import revision_for_hash

    st, inst = _intent(monkeypatch)
    store.write_from_files(
        "skill",
        inst.profile_id,
        inst.key,
        revision_for_hash(inst.content_hash),
        {"SKILL.md": "tampered"},
    )
    with pytest.raises(IntegrityFailed):
        cloud_skills.prepare_one(st, inst.install_id)
    assert not registry.get(inst.install_id).ready


def test_prepare_failure_retains_previous_ready_revision(index_db, caps_root, monkeypatch):
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    old = registry.get(inst.install_id).resolved_revision
    st, inst = _intent(monkeypatch, "v2")
    monkeypatch.setattr(
        cloud_skills, "_download", lambda *_: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    with pytest.raises(RuntimeError):
        cloud_skills.prepare_one(st, inst.install_id)
    restored = registry.get(inst.install_id)
    assert restored.ready and restored.resolved_revision == old
    assert restored.payload["update_available"]


def test_switch_during_download_cannot_publish_old_account(index_db, caps_root, monkeypatch):
    from core.capabilities import store
    from core.capabilities.errors import CloudUnavailable

    st, inst = _intent(monkeypatch)

    def download(*_):
        monkeypatch.setattr(bridge, "get_state", lambda: state("b"))
        return _zip({"SKILL.md": "v1"})

    monkeypatch.setattr(cloud_skills, "_download", download)
    with pytest.raises(CloudUnavailable):
        cloud_skills.prepare_one(st, inst.install_id)
    assert list(store.iter_components("skill", inst.profile_id)) == []
    assert not registry.get(inst.install_id).ready


def test_publishing_new_local_revision_preserves_old_run_target(index_db, caps_root):
    from core.capabilities import store

    old = skills.publish_local_skill("example", files={"SKILL.md": "v1"}, content_hash="a" * 64)
    skills.publish_local_skill("example", files={"SKILL.md": "v2"}, content_hash="b" * 64)
    assert old.entry_file.read_text() == "v1"
    assert len(store.revisions("skill", "local", "example")) == 2


@pytest.fixture
def durable_index(index_db):
    from core.db.models import ContentBlock

    with index_db() as db:
        ContentBlock.__table__.create(db.get_bind(), checkfirst=True)
    return index_db


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
    with pytest.raises(CapabilityError):
        runtime.validate(run)


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
    from core.capabilities.errors import IntegrityFailed

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
    with pytest.raises(IntegrityFailed):
        runtime.bind_mcp(run, {"search": {**v2["search"], "url": "http://different"}}, None)


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
    response = await server.execute(
        server.ExecuteRequest(
            script_content="cat /workspace/skills/example/SKILL.md",
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
    cloud_skills._reconcile_intent(_ordered_skill_manifest(st, build_skill_manifest([], [])), st)
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


def _shell_id(center: str) -> str:
    """本机影子用户按壳的命名空间规则建档：cloud:<host>:<port>:<ucid>。"""
    from core.services.desktop_cloud_bridge import shell_user_center_id

    return shell_user_center_id("https://cloud.example", center)


def _state_v2(uid, epoch=1, nonce="one"):
    claims = {
        "u": "cloud-" + uid,
        "c": "center-" + uid,
        "a": epoch,
        "h": "session-" + str(epoch),
        "d": "device",
        "n": nonce,
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"cloud_base": "https://cloud.example", "token": "dcap2." + body + ".sig"}


def test_switch_rebuild_never_puts_new_account_in_old_users_view(
    durable_index, caps_root, tmp_path, monkeypatch
):
    from core.capabilities import store
    from core.db.models import UserShadow
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.capabilities.paths import revision_for_hash

    with durable_index() as db:
        UserShadow.__table__.create(db.get_bind(), checkfirst=True)
        db.add_all(
            [
                UserShadow(user_id="local-a", username="A", user_center_id=_shell_id("center-a")),
                UserShadow(user_id="local-b", username="B", user_center_id=_shell_id("center-b")),
            ]
        )
        db.commit()
    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(
        bridge,
        "get_identity_state",
        lambda: {
            "user_center_id": "center-"
            + ("a" if current[0]["token"] == _state_v2("a")["token"] else "b"),
            "shell_user_center_id": _shell_id(
                "center-" + ("a" if current[0]["token"] == _state_v2("a")["token"] else "b")
            ),
        },
    )
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "device_view_dir", lambda: tmp_path / "shared" / "skills")
    monkeypatch.setattr(skills, "user_view_dir", lambda uid: tmp_path / "shared" / "skills_u" / uid)
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    for uid in ("a", "b"):
        profile = profile_id("https://cloud.example", "cloud-" + uid)
        digest = skill_content_hash("private-" + uid, {})
        inst = registry.upsert(
            profile_id=profile,
            ref=cloud_ref("https://cloud.example", "skill", "private", scope="private"),
            content_hash=digest,
        )
        comp = store.write_from_files(
            "skill", profile, "private", revision_for_hash(digest), {"SKILL.md": "private-" + uid}
        )
        registry.set_state(inst.install_id, "ready", resolved_revision=comp.revision)
    skills.rebuild_user_view("local-a")
    old = skills.user_view_dir("local-a")
    assert (old / "private" / "SKILL.md").read_text() == "private-a"
    current[0] = _state_v2("b")
    bridge._rebuild_identity_views()
    assert not (old / "private").exists()
    skills.rebuild_user_view("local-b")
    assert (skills.user_view_dir("local-b") / "private" / "SKILL.md").read_text() == "private-b"


def test_ensure_cloud_ready_downloads_only_unready_cloud_components(durable_index, caps_root, monkeypatch):
    """对话里选中尚未下载的云端技能 / 插件时按需准备：只对当前账号未就绪的记录调下载，
    插件定义就绪后再准备它的组件；已就绪的与别的账号的记录不碰。"""
    from core.capabilities.preparation import ensure_cloud_ready
    from core.services import desktop_cloud_bundles, desktop_cloud_skills

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(skills, "account_authorized_for", lambda uid: uid == "local-a")
    profile = profile_id("https://cloud.example", "cloud-a")
    other = profile_id("https://cloud.example", "cloud-b")
    pending_skill = registry.upsert(
        profile_id=profile, ref=cloud_ref("https://cloud.example", "skill", "pdf-editing", scope="shared"),
        content_hash="a" * 64,
    )
    ready_skill = registry.upsert(
        profile_id=profile, ref=cloud_ref("https://cloud.example", "skill", "ready", scope="shared"),
        content_hash="b" * 64,
    )
    registry.set_state(ready_skill.install_id, "ready", resolved_revision="rev-b")
    foreign = registry.upsert(
        profile_id=other, ref=cloud_ref("https://cloud.example", "skill", "pdf-editing", scope="shared"),
        content_hash="c" * 64,
    )
    plugin = registry.upsert(
        profile_id=profile, ref=cloud_ref("https://cloud.example", "plugin", "knowledge", scope="shared"),
        content_hash="d" * 64,
    )
    component = registry.upsert(
        profile_id=profile, ref=cloud_ref("https://cloud.example", "skill", "knowledge-daily", scope="shared"),
        content_hash="e" * 64,
    )
    prepared = {"skills": [], "definitions": []}

    def fake_bundles(state, install_ids):
        prepared["definitions"].append(list(install_ids))
        for iid in install_ids:
            registry.set_state(iid, "ready", resolved_revision="rev")
            registry.set_components(iid, {component.install_id: True})
        return [{"install_id": iid, "ok": True} for iid in install_ids]

    def fake_skills(state, install_ids):
        prepared["skills"].append(list(install_ids))
        for iid in install_ids:
            registry.set_state(iid, "ready", resolved_revision="rev")
        return [{"install_id": iid, "ok": True} for iid in install_ids]

    monkeypatch.setattr(desktop_cloud_bundles, "prepare", fake_bundles)
    monkeypatch.setattr(desktop_cloud_skills, "prepare", fake_skills)

    assert ensure_cloud_ready("local-b", skill_keys=["pdf-editing"]) == []
    assert prepared == {"skills": [], "definitions": []}

    assert ensure_cloud_ready("local-a", skill_keys=["pdf-editing", "ready"], plugin_keys=["knowledge"]) == []
    assert prepared["definitions"] == [[plugin.install_id]]
    assert sorted(prepared["skills"][0]) == sorted([pending_skill.install_id, component.install_id])
    assert registry.get(foreign.install_id).state != "ready"
    assert registry.get(pending_skill.install_id).ready and registry.get(component.install_id).ready


def test_prepared_run_rejects_new_session_of_the_same_account(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.capabilities.errors import PermissionDenied

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    from pathlib import Path
    from core.capabilities.resolver import Candidate, Resolution

    monkeypatch.setattr(skills, "resolve_for_user", lambda _: Resolution())
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-a")
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    config = {"search": {"url": "https://cloud.example/gateway/search"}}
    monkeypatch.setattr(bridge, "cloud_gateway_mcp_configs", lambda *_: config)
    run = runtime.prepare("session-run", "local-a", skill_ids=[])
    assert run.profile is None
    profile = skills.current_account_profile()
    choice = Candidate(
        install_id="mcp:" + profile + ":search",
        runtime_name="search",
        kind="mcp",
        profile=profile,
        source="cloud",
        path=Path("<cloud>"),
    )
    runtime.bind_mcp(run, config, Resolution(chosen={"search": choice}))
    run = runtime.get("session-run")
    assert run.profile == profile
    current[0] = _state_v2("a", nonce="rotated")
    runtime.validate(run)
    current[0] = _state_v2("a", epoch=2)
    with pytest.raises(PermissionDenied):
        runtime.validate(run)


def test_authorization_check_makes_no_network_call(index_db, caps_root, monkeypatch):
    """装配前的授权判定不发网络请求——同步只由登录和变更信号驱动，没有事前探测。"""
    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("authorization must not probe the cloud")

    monkeypatch.setattr("httpx.get", forbidden)
    for _ in range(3):
        bridge.ensure_current_authorization()
    assert current[0] is not None


@pytest.mark.asyncio
async def test_revoked_grant_is_decided_by_the_gateway_call(index_db, caps_root, monkeypatch):
    """撤权由云端在真实网关调用时裁决：401 立刻清掉本机的桥状态。"""
    import httpx
    import mcp.types
    from core.llm.mcp_manager import GatewayMCPTool

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(bridge, "clear_state", lambda: current.__setitem__(0, None))
    tool = GatewayMCPTool(
        mcp_name="search",
        tool=mcp.types.Tool(name="search", inputSchema={"type": "object"}),
        invoke_url="https://cloud.example/api/v1/desktop/capability/gateway/search/call",
        schema_hash="frozen",
        headers=bridge.cloud_headers(current[0]),
        timeout=5,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )
    with pytest.raises(RuntimeError):
        await tool()
    assert current[0] is None


@pytest.mark.asyncio
async def test_running_gateway_uses_rotated_token_but_rejects_new_account(
    index_db, caps_root, monkeypatch
):
    import httpx
    import mcp.types
    from core.llm.mcp_manager import GatewayMCPTool
    from core.capabilities.errors import CloudUnavailable

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": {"content": [{"type": "text", "text": "ok"}]}})

    tool = GatewayMCPTool(
        mcp_name="search",
        tool=mcp.types.Tool(name="search", inputSchema={"type": "object"}),
        invoke_url="https://cloud.example/api/v1/desktop/capability/gateway/search/call",
        schema_hash="frozen",
        headers=bridge.cloud_headers(current[0]),
        timeout=5,
        transport=httpx.MockTransport(handler),
    )
    current[0] = _state_v2("a", nonce="rotated")
    await tool()
    assert seen == ["Bearer " + current[0]["token"]]
    current[0] = _state_v2("b")
    with pytest.raises(CloudUnavailable):
        await tool()
    assert len(seen) == 1


def test_missing_explicit_source_never_falls_back(index_db):
    from pathlib import Path
    from core.capabilities.resolver import Candidate, resolve

    builtin = Candidate(
        install_id="skill:builtin:example",
        runtime_name="example",
        kind="skill",
        profile="builtin",
        source="builtin",
        path=Path("/tmp/unused"),
    )
    result = resolve("skill", [builtin], preferences={"example": "skill:p_old:example"})
    assert not result.chosen and result.reasons["example"] == "preferred_missing"


@pytest.mark.asyncio
async def test_concurrent_runs_in_same_chat_cannot_repoint_running_script(
    durable_index, caps_root, tmp_path, monkeypatch
):
    import asyncio
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
    old = runtime.prepare("concurrent-old", "u", skill_ids=["example"])
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v2"}, content_hash=skill_content_hash("v2", {})
    )
    new = runtime.prepare("concurrent-new", "u", skill_ids=["example"])
    first = asyncio.create_task(
        server.execute(
            server.ExecuteRequest(
                script_content="sleep 0.2; cat /workspace/skills/example/SKILL.md",
                script_name="old.sh",
                language="bash",
                session_id="same-chat",
                user_id="u",
                capability_view_key=old.view_dir.parent.name,
            )
        )
    )
    await asyncio.sleep(0.05)
    second = await server.execute(
        server.ExecuteRequest(
            script_content="cat /workspace/skills/example/SKILL.md",
            script_name="new.sh",
            language="bash",
            session_id="same-chat",
            user_id="u",
            capability_view_key=new.view_dir.parent.name,
        )
    )
    before = await first
    assert (before.exit_code, before.stdout.strip()) == (0, "v1")
    assert (second.exit_code, second.stdout.strip()) == (0, "v2")


def test_multiple_failed_updates_keep_actual_resolved_hash(index_db, caps_root, monkeypatch):
    from core.services.desktop_capability_protocol import skill_content_hash

    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    _intent(monkeypatch, "v2")
    _, latest = _intent(monkeypatch, "v3")
    assert latest.content_hash == skill_content_hash("v3", {})
    assert latest.payload["resolved_content_hash"] == skill_content_hash("v1", {})


def test_device_disable_survives_cloud_manifest_refresh(index_db, caps_root, monkeypatch):
    st, inst = _intent(monkeypatch)
    registry.set_state(
        inst.install_id, inst.state, payload_update={"device_enabled_override": False}
    )
    registry.set_enabled(inst.install_id, False)
    from core.services.desktop_capability_protocol import skill_content_hash

    manifest = build_skill_manifest(
        [
            {
                "skill_id": "example",
                "display_name": "Example",
                "description": "",
                "version": "1",
                "scope": "shared",
                "content_hash": skill_content_hash("v1", {}),
                "mcp_server_ids": [],
            }
        ],
        [],
    )
    cloud_skills._reconcile_intent(_ordered_skill_manifest(st, manifest), st)
    assert not registry.get(inst.install_id).enabled


def test_mcp_public_environment_is_frozen_while_credentials_rotate(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.capabilities.errors import IntegrityFailed

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    run = runtime.prepare("env-run", "u", skill_ids=[])
    runtime.bind_mcp(
        run, {"local": {"command": "node", "env": {"MODE": "safe", "API_KEY": "old"}}}, None
    )
    runtime.bind_mcp(
        run, {"local": {"command": "node", "env": {"MODE": "safe", "API_KEY": "new"}}}, None
    )
    with pytest.raises(IntegrityFailed):
        runtime.bind_mcp(
            run, {"local": {"command": "node", "env": {"MODE": "other", "API_KEY": "new"}}}, None
        )


@pytest.mark.parametrize("extra", ["__pycache__/evil.pyc", ".git/hooks/evil", "large.bin"])
def test_full_package_hash_never_skips_hidden_or_executable_bytes(
    index_db, caps_root, monkeypatch, extra
):
    from core.capabilities import store
    from core.capabilities.paths import revision_for_hash
    from core.capabilities.errors import IntegrityFailed

    st, inst = _intent(monkeypatch)
    store.write_from_files(
        "skill",
        inst.profile_id,
        inst.key,
        revision_for_hash(inst.content_hash),
        {"SKILL.md": "v1", extra: b"unlisted extra"},
    )
    with pytest.raises(IntegrityFailed):
        cloud_skills.prepare_one(st, inst.install_id)


def test_full_package_hash_rejects_limit_instead_of_skipping(tmp_path, monkeypatch):
    from core.capabilities import archive
    from core.capabilities.errors import IntegrityFailed

    root = tmp_path / "package"
    root.mkdir()
    (root / "SKILL.md").write_text("v1")
    (root / "extra").write_bytes(b"123456789")
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", 8)
    with pytest.raises(IntegrityFailed):
        skills.skill_dir_hash(root, fresh=True)


def test_expired_cloud_token_keeps_local_copy_identity_until_logout(
    durable_index, caps_root, monkeypatch
):
    from core.db.models import UserShadow
    from core.agent_skills.backends.capability_store import CapabilityStoreBackend
    from core.services.desktop_capability_protocol import skill_content_hash

    with durable_index() as db:
        UserShadow.__table__.create(db.get_bind(), checkfirst=True)
        db.add(UserShadow(user_id="local-a", username="A", user_center_id=_shell_id("center-a")))
        db.commit()
    now = [100.0]
    monkeypatch.setattr(bridge.time, "time", lambda: now[0])
    monkeypatch.setattr(bridge, "_purge_persisted_state", lambda: None)
    monkeypatch.setattr(bridge, "_rebuild_identity_views", lambda: None)
    monkeypatch.setattr(bridge, "_refresh_manifest_async", lambda **kwargs: None)
    skills.publish_local_skill(
        "copy",
        files={"SKILL.md": "copy"},
        content_hash=skill_content_hash("copy", {}),
        owner_user_id="local-a",
    )
    st = _state_v2("a")
    bridge.set_state(st["cloud_base"], st["token"], 1)
    now[0] += 2
    assert bridge.get_state() is None
    assert skills.current_account_profile() is None
    assert skills.current_local_user_id() == "local-a"
    assert CapabilityStoreBackend(local=True).exists("copy")
    bridge.clear_state()
    assert skills.current_local_user_id() is None
    assert not CapabilityStoreBackend(local=True).exists("copy")


@pytest.mark.asyncio
async def test_dedicated_agent_factory_reaches_config_after_freezing_definition(
    durable_index, caps_root, monkeypatch
):
    from core.llm import agent_factory
    from core.capabilities.agents import AgentDefinition
    from core.capabilities import runtime

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(
        "core.llm.tool_permissions.resolve_approval_mode", lambda *args, **kwargs: "auto"
    )

    class ConfigurationReached(Exception):
        pass

    monkeypatch.setattr(
        agent_factory, "load_prompt_config", lambda: (_ for _ in ()).throw(ConfigurationReached())
    )
    definition = AgentDefinition(agent_id="dedicated", name="Dedicated", system_prompt="frozen")
    with pytest.raises(ConfigurationReached):
        await agent_factory.create_agent_executor(
            user_agent=definition,
            current_user_id="owner",
            run_id="dedicated-run",
            disable_tools=True,
        )
    changed = AgentDefinition(agent_id="dedicated", name="Dedicated", system_prompt="new")
    assert runtime.pin_agent_definition("dedicated-run", "owner", changed).system_prompt == "frozen"


def test_name_preferences_are_isolated_between_accounts(index_db, monkeypatch, tmp_path):
    from core.capabilities import connectors
    from core.capabilities.resolver import Candidate

    def candidate(iid):
        path = tmp_path / iid.rsplit(":", 1)[-1]
        path.mkdir()
        (path / "SKILL.md").write_text("---\nname: same\ndescription: Test\n---\nBody\n")
        return Candidate(
            install_id=iid,
            runtime_name="same",
            kind="skill",
            profile="local",
            source="local",
            path=path,
        )
    a, b = candidate("skill:local:a"), candidate("skill:local:b")
    monkeypatch.setattr(skills, "candidates", lambda uid: [a] if uid == "a" else [b])
    registry.set_preference("skill", "same", a.install_id, chosen_by="a")
    assert skills.resolve_for_user("b").chosen["same"].install_id == b.install_id
    registry.set_preference("skill", "same", b.install_id, chosen_by="b")
    assert skills.resolve_for_user("a").chosen["same"].install_id == a.install_id
    assert registry.preferences("skill") == {}
    assert registry.clear_preference("skill", "same", user_id="b")
    assert registry.preferences("skill", user_id="a") == {"same": a.install_id}
    registry.set_preference("mcp", "search", "mcp:local:a", chosen_by="a")
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "b")
    cands = connectors.json_candidates({"search": {"enabled": True}})
    assert (
        connectors.resolve_bindings(cands, keep_local=set()).chosen["search"].install_id
        == "mcp:local-json:search"
    )


def test_legacy_name_preference_stays_with_its_owner(index_db):
    from core.db.models import DeviceCapabilityNamePreference

    with registry._session() as db:
        db.add(
            DeviceCapabilityNamePreference(
                preference_id="skill:same",
                kind="skill",
                runtime_name="same",
                chosen_install_id="skill:local:old",
                chosen_by="a",
            )
        )
    assert registry.preferences("skill", user_id="a") == {"same": "skill:local:old"}
    assert registry.preferences("skill", user_id="b") == {}
    registry.set_preference("skill", "same", "skill:local:new", chosen_by="a")
    assert registry.preferences("skill", user_id="a") == {"same": "skill:local:new"}
    assert not registry.clear_preference("skill", "same", user_id="b")
    assert registry.clear_preference("skill", "same", user_id="a")
    assert registry.preferences("skill", user_id="a") == {}


def test_enabled_cloud_connector_without_schema_is_unusable(index_db, monkeypatch):
    from core.capabilities import connectors
    from core.capabilities.errors import PackageMissing

    st = state("a")
    ctx = {
        "state": st,
        "profile": profile_id(st["cloud_base"], "a"),
        "manifest_revision": "r1",
        "servers": [
            {"server_id": "empty", "component": "empty", "tools": [], "schema_hash": "hash"}
        ],
    }
    empty = connectors.cloud_candidates(ctx["profile"], ctx["servers"], {})[0]
    assert not empty.usable and empty.state == "schema_empty"
    disabled = connectors.cloud_candidates(ctx["profile"], ctx["servers"], {"empty": False})[0]
    assert not disabled.usable and disabled.state == "disabled"
    monkeypatch.setattr(bridge, "_bridge_context", lambda: ctx)
    monkeypatch.setattr(bridge, "_local_server_base_map", lambda: {})
    monkeypatch.setattr(bridge, "_mcp_json_local_declarations", lambda: {})
    monkeypatch.setattr(bridge, "_mcp_json_local_configs", lambda: {})
    with pytest.raises(PackageMissing):
        bridge.cloud_gateway_mcp_configs(["empty"])


@pytest.mark.parametrize("method", ["get_by_id", "get_raw_by_id"])
def test_cloud_agent_details_cannot_cross_local_user_identity(
    index_db, caps_root, monkeypatch, method
):
    from types import SimpleNamespace
    from core.capabilities import agents
    from core.services.user_agent_base import UserAgentBaseService

    current = agents.AgentDefinition(agent_id="private-agent", name="Private", origin="cloud")
    monkeypatch.setattr(agents, "account_definition", lambda _: current)
    monkeypatch.setattr(agents, "account_definitions", lambda: [current])
    monkeypatch.setattr(skills, "account_authorized_for", lambda uid: uid == "current-user")
    service = object.__new__(UserAgentBaseService)
    service.repo = SimpleNamespace(get_by_id=lambda _: None)
    with pytest.raises(LookupError):
        getattr(service, method)("private-agent", "old-user")
    assert getattr(service, method)("private-agent", "current-user") is not None
    assert not agents.resolve_visible("old-user", []).chosen
    assert (
        agents.resolve_visible("current-user", []).chosen["Private"].install_id
        == "agent:local:private-agent"
    )


def test_unselected_cloud_skill_does_not_block_local_run_offline(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.capabilities.errors import CloudUnavailable, PermissionDenied
    from core.services.desktop_capability_protocol import skill_content_hash

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    skills.publish_local_skill(
        "local-copy",
        files={"SKILL.md": "offline"},
        content_hash=skill_content_hash("offline", {}),
        owner_user_id="owner",
    )
    monkeypatch.setattr(
        bridge,
        "ensure_current_authorization",
        lambda *_: (_ for _ in ()).throw(CloudUnavailable("offline")),
    )
    run = runtime.prepare("offline-local", "owner", skill_ids=["local-copy"])
    assert run.profile is None and set(run.bindings) == {"local-copy"}
    assert runtime.frozen_loader(run).get_skill_dir("example") is None
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    runtime.validate(run, user_id="owner")
    with pytest.raises(PermissionDenied):
        runtime.validate(run, user_id="someone-else")


def test_cloud_skill_declared_by_agent_is_included_in_frozen_selection(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime, agents

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    definition = agents.AgentDefinition(
        agent_id="declare", name="Declare", dependencies=[{"kind": "skill", "id": "example"}]
    )
    run = runtime.prepare("agent-dependency", "owner", skill_ids=[], agent_definition=definition)
    assert run.profile == inst.profile_id and "example" in run.bindings


def test_cloud_skill_declared_by_selected_plugin_is_frozen(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime, plugins

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    plugins.publish_local_plugin(
        {"slug": "pack", "install_id": "pack@owner", "components": {"skills": ["example"]}},
        owner_user_id="owner",
    )
    run = runtime.prepare("plugin-dependency", "owner", skill_ids=[], plugin_ids=["pack@owner"])
    assert "example" in run.bindings and run.profile == inst.profile_id
    ready = runtime.preflight(run, plugin_ids=["pack@owner"])
    assert ready.dependency_report["ready"]


def test_local_agent_replay_does_not_depend_on_cloud_session(durable_index, caps_root, monkeypatch):
    from core.capabilities import runtime, agents

    current = [state("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    definition = agents.AgentDefinition(agent_id="local", name="Local", system_prompt="offline")
    runtime.pin_agent_definition("local-agent", "owner", definition)
    current[0] = None
    updated = agents.AgentDefinition(agent_id="local", name="Local", system_prompt="new")
    assert runtime.pin_agent_definition("local-agent", "owner", updated).system_prompt == "offline"


