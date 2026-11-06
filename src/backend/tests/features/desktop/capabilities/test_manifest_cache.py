"""Accepted cloud manifests survive a local backend restart, scoped to one account session."""
from core.capabilities.manifest_protocol import CAPABILITY_MANIFEST_VERSION, canonical_hash, validate_manifest
from features.desktop.bridge import client as bridge_client
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity
from features.desktop.bridge import manifest_cache
from features.desktop.bridge import bundles as desktop_cloud_bundles, skills as desktop_cloud_skills


def _tools():
    return validate_manifest({"version": CAPABILITY_MANIFEST_VERSION,
                              "revision": canonical_hash([]), "servers": []})


def _state(session="s1"):
    return {"cloud_base": "https://cloud.example", "token": "opaque-" + session, "device_id": "dev"}


def test_round_trip_is_scoped_to_the_account_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(tmp_path))
    manifest = _tools()
    manifest_cache.save(_state(), manifest_cache.KIND_TOOLS, manifest)
    assert manifest_cache.load(_state(), manifest_cache.KIND_TOOLS) == manifest
    assert manifest_cache.load(_state("other-login"), manifest_cache.KIND_TOOLS) is None


def test_tampered_cache_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(tmp_path))
    manifest_cache.save(_state(), manifest_cache.KIND_TOOLS, {**_tools(), "revision": "forged"})
    assert manifest_cache.load(_state(), manifest_cache.KIND_TOOLS) is None


def test_logout_clears_every_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(tmp_path))
    manifest_cache.save(_state(), manifest_cache.KIND_TOOLS, _tools())
    manifest_cache.clear_all()
    assert manifest_cache.load(_state(), manifest_cache.KIND_TOOLS) is None


def test_restore_requires_every_kind(monkeypatch):
    cached = {manifest_cache.KIND_TOOLS: _tools(), "skill": {"revision": "s"}, "agent": {"revision": "a"}}
    monkeypatch.setattr(manifest_cache, "load", lambda _st, kind: cached.get(kind))
    bridge_identity.reset_for_tests()
    bridge_client.restore_cached_manifests(_state())
    assert bridge_client_state.manifest_restored is False
    assert bridge_client_state.manifest is None

    cached["plugin"] = {"revision": "p"}
    bridge_client.restore_cached_manifests(_state())
    try:
        assert bridge_client_state.manifest_restored is True
        assert bridge_client_state.manifest == cached[manifest_cache.KIND_TOOLS]
        assert desktop_cloud_skills.synced_manifest() == {"revision": "s"}
        assert desktop_cloud_bundles.synced_manifests() == {"agent": {"revision": "a"}, "plugin": {"revision": "p"}}
    finally:
        bridge_identity.reset_for_tests()
        desktop_cloud_bundles.reset_for_tests()
