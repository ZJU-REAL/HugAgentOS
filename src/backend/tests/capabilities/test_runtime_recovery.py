from tests.sandbox.runner_client import run_runner

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


from tests.capabilities.runtime_recovery_support import (
    _ordered_skill_manifest,
    state,
    _zip,
    _intent,
    durable_index,
    _shell_id,
    _state_v2,
)


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
    monkeypatch.setattr(bridge, "_local_server_ids", lambda: {"search"})
    monkeypatch.setattr(bridge, "_mcp_json_local_declarations", lambda: {})
    monkeypatch.setattr(bridge, "_mcp_json_local_configs", lambda: {})
    registry.set_preference("mcp", "search", "mcp:local:search")
    assert bridge.apply_to_enabled_mcp_ids(["search"]) == ["search"]
    assert "search" not in bridge.cloud_gateway_mcp_configs()


def test_stale_skill_response_cannot_repopulate_switched_account(index_db, caps_root, monkeypatch):
    a, b = state("a"), state("b")
    current = [a]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    manifest = build_skill_manifest([])

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
    monkeypatch.setattr(bridge, "_local_server_ids", lambda: set())
    monkeypatch.setattr(
        bridge, "_mcp_json_local_declarations", lambda: {"my-files": {"enabled": True}}
    )
    assert bridge.apply_to_enabled_mcp_ids([]) == ["my-files"]


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
