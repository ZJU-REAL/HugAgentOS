"""Cloud-only plugin bindings reach the actual factory and explain blocked runs."""

import json
import pytest
from core.capabilities import plugins, registry, runtime, skills, store, dependency
from core.capabilities.ref import cloud_ref, profile_id
from core.capabilities.paths import revision_for_hash
from core.capabilities.errors import IntegrityFailed, PackageMissing
from core.services import desktop_cloud_bridge as bridge
from core.services.desktop_capability_protocol import entity_content_hash, skill_content_hash
from tests.capabilities.test_runtime_recovery import state, durable_index


def cloud_plugin(monkeypatch, index_db):
    st = state("cloud-owner")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-owner")
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda: None)
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    profile = profile_id(st["cloud_base"], "cloud-owner")
    definition = {
        "slug": "pack",
        "install_id": "pack@cloud-owner",
        "components": {"skills": ["pack-skill"], "mcp": ["pack-search"]},
    }
    files = plugins.plugin_manifest_files(definition)
    digest = entity_content_hash(files)
    inst = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], "plugin", "pack", scope="private"),
        content_hash=digest,
        payload={"cloud_install_id": "pack@cloud-owner"},
    )
    comp = store.write_from_files("plugin", profile, "pack", revision_for_hash(digest), files)
    registry.set_state(inst.install_id, "ready", resolved_revision=comp.revision)
    skill = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], "skill", "pack-skill", scope="private"),
        content_hash=skill_content_hash("private script", {}),
    )
    return st, inst, comp, skill


def test_factory_expands_cloud_plugin_without_local_installed_plugin_row(
    durable_index, caps_root, monkeypatch
):
    from core.llm.agent_factory import _expand_plugin_bindings

    st, plugin, comp, skill = cloud_plugin(monkeypatch, durable_index)
    skill_ids, mcp_ids = _expand_plugin_bindings(["pack@cloud-owner"], user_id="local-owner")
    assert skill_ids == ["pack-skill"] and mcp_ids == ["pack-search"]
    assert not registry.get(skill.install_id).ready
    assert store.revisions("skill", skill.profile_id, skill.key) == []
    with pytest.raises(PackageMissing):
        runtime.prepare(
            "not-auto-prepared", "local-owner", skill_ids=skill_ids, plugin_ids=["pack@cloud-owner"]
        )
    prepared = store.write_from_files(
        "skill",
        skill.profile_id,
        skill.key,
        revision_for_hash(skill.content_hash),
        {"SKILL.md": "private script"},
    )
    registry.set_state(skill.install_id, "ready", resolved_revision=prepared.revision)
    run = runtime.prepare(
        "explicitly-prepared", "local-owner", skill_ids=skill_ids, plugin_ids=["pack@cloud-owner"]
    )
    ready = runtime.preflight(
        run, skill_ids=skill_ids, plugin_ids=["pack@cloud-owner"], available_mcp=mcp_ids
    )
    assert ready.dependency_report["ready"]


def test_cloud_plugin_binding_discovery_rejects_other_account_and_tampered_definition(
    index_db, caps_root, monkeypatch
):
    st, plugin, comp, skill = cloud_plugin(monkeypatch, index_db)
    assert plugins.cloud_binding_ids(["pack@cloud-owner"], user_id="someone-else") == ([], [])
    comp.entry_file.write_text(json.dumps({"components": {"mcp": ["substituted"]}}))
    with pytest.raises(IntegrityFailed):
        plugins.cloud_binding_ids(["pack@cloud-owner"], user_id="local-owner")


def test_blocked_preflight_persists_safe_report_before_raising(
    durable_index, caps_root, monkeypatch
):
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    body = "---\nname: local\nmcp_servers: search\n---\nREPORT_CONTENT_CANARY"
    skills.publish_local_skill(
        "local",
        files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}),
        owner_user_id="owner",
    )
    run = runtime.prepare("blocked-dependencies", "owner", skill_ids=["local"])
    with pytest.raises(dependency.DependencyMissing):
        runtime.preflight(run, skill_ids=["local"], available_mcp=[])
    report = runtime.get(run.run_id).dependency_report
    assert report["state"] == "blocked" and not report["ready"]
    assert report["error"]["code"] == "dependency_missing"
    assert report["errors"][0]["dependency_chain"][-1] == "mcp:search"
    assert "REPORT_CONTENT_CANARY" not in json.dumps(report)
    ready = runtime.preflight(run, skill_ids=["local"], available_mcp=["search"])
    assert ready.dependency_report["state"] == "ready" and "error" not in ready.dependency_report
