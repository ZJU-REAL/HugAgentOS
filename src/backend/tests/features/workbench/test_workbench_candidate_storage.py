"""Candidate storage limits through upload/decision HTTP and retained-asset boundary."""

import os
from pathlib import Path
import pytest
from tests.features.workbench.test_workbench_plugin_routes import client, upload, launched
from tests.features.workbench.test_workbench_upgrade_routes import import_upgrade, endpoint, decision
from core.capabilities.plugins.packaging.runtime_assets import materialize


def test_cloud_candidate_replacement_cleanup_failure_quota_and_retry(client, db_session, tmp_path):
    install = upload(client)
    old = launched(client)
    assert import_upgrade(client, grants=["file.save"], html="candidate one").status_code == 201
    first = client.get(endpoint(install)).json()["data"]["candidateRevision"]
    cache = Path(os.environ["PLUGIN_ASSET_CACHE_DIR"])
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("unrelated data")
    cache.mkdir(parents=True, exist_ok=True)
    redirected = cache / first
    redirected.symlink_to(outside, target_is_directory=True)

    assert import_upgrade(client, grants=["file.save"], html="candidate two").status_code == 201
    second = client.get(endpoint(install)).json()["data"]
    rejected = import_upgrade(client, grants=["file.save"], html="candidate three")
    assert rejected.status_code == 413
    assert rejected.json()["detail"]["code"] == "candidate_storage_quota_exceeded"
    status = client.get(endpoint(install)).json()["data"]
    assert status["candidateRevision"] == second["candidateRevision"]
    assert status["candidateStorage"]["count"] == 2
    assert status["candidateStorage"]["cleanupFailures"] == 1
    assert marker.read_text() == "unrelated data"
    assert launched(client)["revision"] == old["revision"]

    redirected.unlink()
    assert import_upgrade(client, grants=["file.save"], html="candidate three").status_code == 201
    with pytest.raises(ValueError, match="package_assets_unavailable"):
        materialize(db_session, first)
    assert decision(client, install, old, choice="reject").status_code == 200
    assert client.get(endpoint(install)).json()["data"]["candidateStorage"]["count"] == 0
    assert marker.read_text() == "unrelated data"
    assert client.get(old["assetPath"]).status_code == 200


def test_cloud_candidate_byte_budget_rejects_before_archive_retention(client, monkeypatch):
    from features.workbench import plugin_candidate_storage

    install = upload(client)
    old = launched(client)
    monkeypatch.setattr(plugin_candidate_storage, "MAX_BYTES", 100)
    response = import_upgrade(client, grants=["file.save"])
    assert response.status_code == 413
    status = client.get(endpoint(install)).json()["data"]
    assert status["status"] == "current"
    assert status["candidateStorage"]["count"] == 0
    assert launched(client)["revision"] == old["revision"]


def test_rejected_candidate_cannot_delete_another_users_current_package(client, db_session):
    from core.auth.backend import UserContext, get_current_user
    from core.db.models import UserShadow
    from tests.features.workbench.test_workbench_upgrade_routes import upgraded

    install = upload(client)
    old = launched(client)
    assert import_upgrade(client, grants=["file.save"]).status_code == 201
    candidate = client.get(endpoint(install)).json()["data"]["candidateRevision"]
    previous_identity = client.app.dependency_overrides[get_current_user]
    db_session.add(
        UserShadow(
            user_id="candidate-other", username="Other", extra_data={"can_import_plugin": True}
        )
    )
    db_session.commit()
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="candidate-other", user_center_id="candidate-other", username="Other"
    )
    response = client.post(
        "/v1/plugins/import",
        files={"file": ("plugin.zip", upgraded(grants=["file.save"]), "application/zip")},
    )
    assert response.status_code == 201, response.text
    other = launched(client)
    assert other["revision"] == candidate
    client.app.dependency_overrides[get_current_user] = previous_identity
    assert decision(client, install, old, choice="reject").status_code == 200
    assert client.get(endpoint(install)).json()["data"]["candidateStorage"]["count"] == 0
    assert client.get(old["assetPath"]).status_code == 200
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="candidate-other", user_center_id="candidate-other", username="Other"
    )
    assert client.get(other["assetPath"]).status_code == 200
    assert launched(client)["revision"] == candidate


def test_owner_quota_and_reinstall_orphan_cleanup_are_bounded(client, monkeypatch):
    from features.workbench import plugin_candidate_storage

    install = upload(client)
    old = launched(client)
    assert import_upgrade(client, grants=["file.save"], html="first").status_code == 201
    monkeypatch.setattr(plugin_candidate_storage, "MAX_OWNER_CANDIDATES", 1)
    blocked = import_upgrade(client, grants=["file.save"], html="second")
    assert blocked.status_code == 413
    status = client.get(endpoint(install)).json()["data"]
    assert status["candidateStorage"]["ownerCount"] == 1
    assert launched(client)["revision"] == old["revision"]
    assert client.delete("/v1/plugins/installed/" + install).status_code == 200
    new_install = upload(client)
    assert import_upgrade(client, grants=["file.save"], html="after reinstall").status_code == 201
    status = client.get(endpoint(new_install)).json()["data"]
    assert status["candidateStorage"]["ownerCount"] == 1
    assert status["candidateStorage"]["count"] == 1
