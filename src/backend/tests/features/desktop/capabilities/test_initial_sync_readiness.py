"""First-login readiness must cover every capability kind and revision."""
from features.desktop.bridge import client as bridge_client
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity
from features.desktop.bridge import status as bridge_status
from contextlib import nullcontext
import pytest
from features.desktop.bridge import client as bridge
from features.desktop.bridge import skills as desktop_cloud_skills, bundles as desktop_cloud_bundles
from features.desktop.capabilities.paths import revision_for_hash


@pytest.mark.parametrize("case", ["complete", "downloading", "missing_manifest", "old_revision", "error"])
def test_model_manifest_does_not_mark_partial_capabilities_ready(monkeypatch, case):
    digest = "a" * 64
    row = {"enabled": True, "state": "ready", "content_hash": digest,
           "resolved_revision": revision_for_hash(digest)}
    group = {"revision": "revision", "installations": [row], "last_error": None}
    other = {"revision": "revision", "installations": [], "last_error": None}
    if case == "old_revision":
        row["resolved_revision"] = "old"
    if case == "missing_manifest":
        other["revision"] = ""
    if case == "error":
        group["last_error"] = "download failed"
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: {"account": "current"})
    monkeypatch.setattr(bridge_identity, 'account_scope', lambda _: nullcontext())
    monkeypatch.setattr(bridge_client_state, 'manifest', {"servers": []})
    monkeypatch.setattr(bridge_client, 'refresh_manifest_async', lambda **_: None)
    monkeypatch.setattr(bridge_client_state, 'manifest_error', None)
    monkeypatch.setattr(bridge_client_state, 'manifest_fetching', case == "downloading")
    monkeypatch.setattr(desktop_cloud_skills, "_profile", lambda _: "profile")
    monkeypatch.setattr(desktop_cloud_skills, "status", lambda: group)
    monkeypatch.setattr(desktop_cloud_bundles, "status", lambda: {"agent": other, "plugin": other})
    status = bridge_status.initial_sync_status()
    assert status["ready"] is (case == "complete")
    assert status["pending"] == (1 if case == "old_revision" else 0)


def test_logout_cannot_report_cached_manifest_as_ready(monkeypatch):
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: None)
    monkeypatch.setattr(bridge_client_state, 'manifest', {"servers": []})
    assert bridge_status.initial_sync_status()["ready"] is False


@pytest.mark.parametrize("pending, fetching, cloud_error, ready", [
    (False, True, None, True),        # restart: background re-check does not block
    (False, False, "offline", True),  # restart while offline: cached view stays usable
    (True, True, None, False),        # cloud added work since last start: wait for it
])
def test_restored_manifests_are_ready_while_cloud_rechecks(monkeypatch, pending, fetching, cloud_error, ready):
    digest = "a" * 64
    row = {"enabled": True, "state": "pending" if pending else "ready", "content_hash": digest,
           "resolved_revision": revision_for_hash(digest)}
    group = {"revision": "revision", "installations": [row], "last_error": None}
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: {"account": "current"})
    monkeypatch.setattr(bridge_identity, 'require_current_account', lambda _: None)
    monkeypatch.setattr(bridge_client_state, 'manifest', {"servers": []})
    monkeypatch.setattr(bridge_client_state, 'manifest_restored', True)
    monkeypatch.setattr(bridge_client_state, 'manifest_error', cloud_error)
    monkeypatch.setattr(bridge_client_state, 'manifest_fetching', fetching)
    monkeypatch.setattr(desktop_cloud_skills, "_profile", lambda _: "profile")
    monkeypatch.setattr(desktop_cloud_skills, "status", lambda: group)
    monkeypatch.setattr(desktop_cloud_bundles, "status", lambda: {"agent": group, "plugin": group})
    status = bridge_status.initial_sync_status()
    assert status["ready"] is ready
    if ready:
        assert status["error"] is None and status["can_continue"] is False


def test_unrestored_process_still_waits_for_cloud(monkeypatch):
    group = {"revision": "revision", "installations": [], "last_error": None}
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: {"account": "current"})
    monkeypatch.setattr(bridge_identity, 'require_current_account', lambda _: None)
    monkeypatch.setattr(bridge_client_state, 'manifest', {"servers": []})
    monkeypatch.setattr(bridge_client_state, 'manifest_restored', False)
    monkeypatch.setattr(bridge_client_state, 'manifest_error', None)
    monkeypatch.setattr(bridge_client_state, 'manifest_fetching', True)
    monkeypatch.setattr(desktop_cloud_skills, "_profile", lambda _: "profile")
    monkeypatch.setattr(desktop_cloud_skills, "status", lambda: group)
    monkeypatch.setattr(desktop_cloud_bundles, "status", lambda: {"agent": group, "plugin": group})
    assert bridge_status.initial_sync_status()["ready"] is False
