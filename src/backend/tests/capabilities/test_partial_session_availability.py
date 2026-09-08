"""A partial-sync choice is session-scoped and cannot trigger hidden downloads."""
import pytest
from core.capabilities import registry, session_availability, preparation, skills
from core.capabilities.ref import cloud_ref
from core.services import desktop_cloud_bridge as bridge, desktop_cloud_skills, desktop_cloud_bundles
from core.db.models import DeviceCapabilityInstallation


@pytest.fixture(autouse=True)
def clear_selection():
    session_availability.clear()
    yield
    session_availability.clear()


def installation(key, profile="profile", enabled=True):
    return registry.upsert(profile_id=profile, ref=cloud_ref("https://example.test", "skill", key, scope="shared"),
                           content_hash="a" * 64, source="cloud", enabled=enabled)


def test_partial_snapshot_masks_unavailable_rows_without_changing_preferences(index_db):
    ready = installation("ready")
    missing = installation("missing")
    registry.set_state(ready.install_id, "ready", resolved_revision="revision")
    session_availability.activate("profile", {ready.install_id: "revision"})
    assert registry.get(ready.install_id).enabled
    assert not registry.get(missing.install_id).enabled
    with index_db() as db:
        assert db.get(DeviceCapabilityInstallation, missing.install_id).enabled
    session_availability.clear()
    assert registry.get(missing.install_id).enabled


def test_partial_choice_cannot_enable_disabled_or_replaced_revision(index_db):
    disabled = installation("disabled", enabled=False)
    registry.set_state(disabled.install_id, "ready", resolved_revision="revision")
    session_availability.activate("profile", {disabled.install_id: "revision"})
    assert not registry.get(disabled.install_id).enabled
    other = installation("other")
    registry.set_state(other.install_id, "ready", resolved_revision="different")
    assert not registry.get(other.install_id).enabled


def test_another_account_does_not_inherit_the_partial_selection(index_db):
    other = installation("other", profile="another-account")
    session_availability.activate("profile", {})
    assert registry.get(other.install_id).enabled


def test_missing_selected_skill_does_not_download_in_partial_mode(index_db, monkeypatch):
    missing = installation("missing")
    session_availability.activate("profile", {})
    monkeypatch.setattr(skills, "account_authorized_for", lambda _: True)
    monkeypatch.setattr(skills, "current_account_profile", lambda: "profile")
    monkeypatch.setattr(bridge, "get_state", lambda: {"account": "current"})
    def forbidden(*args):
        raise AssertionError("ordinary assembly tried to download an excluded package")
    monkeypatch.setattr(desktop_cloud_skills, "prepare", forbidden)
    monkeypatch.setattr(desktop_cloud_bundles, "prepare", forbidden)
    assert preparation.ensure_cloud_ready("user", skill_keys=[missing.key]) == []


def test_continue_is_rejected_while_sync_is_running(index_db, monkeypatch):
    from contextlib import nullcontext
    from fastapi import HTTPException
    from api.routes.v1 import desktop_capabilities as api
    monkeypatch.setattr(bridge, "account_scope", lambda _: nullcontext())
    monkeypatch.setattr(bridge, "initial_sync_status", lambda: {"can_continue": False})
    with pytest.raises(HTTPException) as error:
        api._accept_synced_capabilities({}, "user")
    assert error.value.status_code == 409
    assert not session_availability.active()


def test_continue_includes_only_current_integrity_verified_revisions(index_db, monkeypatch):
    from contextlib import nullcontext
    from api.routes.v1 import desktop_capabilities as api
    from core.capabilities import readiness
    from core.capabilities.paths import revision_for_hash
    good, corrupt, old, missing = [installation(key) for key in ("good", "corrupt", "old", "missing")]
    revision = revision_for_hash(good.content_hash)
    for row in (good, corrupt):
        registry.set_state(row.install_id, "ready", resolved_revision=revision)
    registry.set_state(old.install_id, "ready", resolved_revision="old-revision")
    monkeypatch.setattr(bridge, "account_scope", lambda _: nullcontext())
    monkeypatch.setattr(bridge, "initial_sync_status", lambda: {"can_continue": True})
    monkeypatch.setattr(api, "_authorized_profile", lambda _: "profile")
    monkeypatch.setattr(api, "_readiness_context", lambda _: object())
    checked = []
    def verify(row, context):
        checked.append(row.install_id)
        return {"ready": row.install_id == good.install_id}
    monkeypatch.setattr(readiness, "file_readiness", verify)
    result = api._accept_synced_capabilities({}, "user")
    assert result["available_count"] == 1
    assert set(checked) == {good.install_id, corrupt.install_id}
    assert registry.get(good.install_id).enabled
    assert all(not registry.get(row.install_id).enabled for row in (corrupt, old, missing))
