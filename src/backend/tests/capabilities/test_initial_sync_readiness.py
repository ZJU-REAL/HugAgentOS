"""First-login readiness must cover every capability kind and revision."""
from contextlib import nullcontext
import pytest
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills, desktop_cloud_bundles
from core.capabilities.paths import revision_for_hash


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
    monkeypatch.setattr(bridge, "get_state", lambda: {"account": "current"})
    monkeypatch.setattr(bridge, "account_scope", lambda _: nullcontext())
    monkeypatch.setattr(bridge, "_manifest", {"servers": []})
    monkeypatch.setattr(bridge, "_refresh_manifest_async", lambda **_: None)
    monkeypatch.setattr(bridge, "_manifest_error", None)
    monkeypatch.setattr(bridge, "_manifest_fetching", case == "downloading")
    monkeypatch.setattr(desktop_cloud_skills, "_profile", lambda _: "profile")
    monkeypatch.setattr(desktop_cloud_skills, "status", lambda: group)
    monkeypatch.setattr(desktop_cloud_bundles, "status", lambda: {"agent": other, "plugin": other})
    status = bridge.initial_sync_status()
    assert status["ready"] is (case == "complete")
    assert status["pending"] == (1 if case == "old_revision" else 0)


def test_logout_cannot_report_cached_manifest_as_ready(monkeypatch):
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    monkeypatch.setattr(bridge, "_manifest", {"servers": []})
    assert bridge.initial_sync_status()["ready"] is False
