"""Actual operator cutover and public admin auth/launch; never expose protected snapshots by HTTP."""
from copy import deepcopy

import pytest
from features.workbench.migration import apply, inventory, rollback
from features.workbench.migration_snapshot import RECEIPT, state
from core.db.models import ContentBlock, UserShadow
from tests.features.workbench.test_workbench_migration import http

KEY = "legacy:old_portal"
AUTH_KEY = "workbench_external_auth:" + KEY
BASE = "/v1/workbench"
AUTH = {"Authorization": "Bearer migration-auth-test-admin"}


def legacy(db):
    db.add(ContentBlock(id="app_config", payload={"apps": [{
        "id": "old_portal", "name": "Old Portal", "enabled": True,
        "url": "https://portal.example/app",
    }]}))
    db.add(UserShadow(user_id="bob", username="Bob", extra_data={
        "allowed_apps": None, "preferences": {"theme": "dark"},
    }))
    db.commit()
    return http(db)


def test_gap_blocks_actual_cutover_until_explicit_admin_configuration(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_TOKEN", "migration-auth-test-admin")
    client = legacy(db_session)
    before = deepcopy(state(db_session))
    report = inventory(db_session, tmp_path / "missing.json")
    assert len(report["authGaps"]) == 1
    with pytest.raises(ValueError, match="migration_auth_incomplete"):
        apply(db_session, tmp_path / "missing.json", report["digest"], writes_frozen=True)
    assert state(db_session) == before
    assert db_session.get(ContentBlock, RECEIPT) is None
    configured = client.put(BASE + "/admin/external-auth/" + KEY, headers=AUTH,
                            json={"revision": 0, "mode": "independent"})
    assert configured.status_code == 200, configured.text
    # A new auth revision requires a fresh inventory; stale reviewed snapshots cannot cut over.
    with pytest.raises(ValueError, match="source_changed"):
        apply(db_session, tmp_path / "missing.json", report["digest"], writes_frozen=True)
    ready = inventory(db_session, tmp_path / "configured.json")
    assert ready["authGaps"] == []
    result = apply(db_session, tmp_path / "configured.json", ready["digest"], writes_frozen=True)
    assert result["status"] == "applied"
    opened = client.post(BASE + "/applications/" + KEY + "/launch", json={})
    assert opened.status_code == 200, opened.text
    assert opened.json()["data"]["target"]["url"] == "https://portal.example/app"
    assert db_session.get(ContentBlock, AUTH_KEY).payload == {"revision": 1, "mode": "independent"}
    assert db_session.get(UserShadow, "bob").extra_data["preferences"]["theme"] == "dark"
    # A legitimate post-cutover auth write remains protected against lossy snapshot rollback.
    changed = client.put(BASE + "/admin/external-auth/" + KEY, headers=AUTH,
                         json={"revision": 1, "mode": "independent"})
    assert changed.status_code == 200, changed.text
    with pytest.raises(ValueError, match="delta_not_representable"):
        rollback(db_session, tmp_path / "configured.json", ready["digest"],
                 tmp_path / "delta.json", writes_frozen=True)
    assert db_session.get(ContentBlock, AUTH_KEY).payload["revision"] == 2
    assert db_session.get(ContentBlock, "app_config") is None
    assert db_session.get(ContentBlock, RECEIPT).payload["status"] == "applied"


@pytest.mark.parametrize("config", [
    {"revision": 0, "mode": "independent"},
    {"revision": 1, "mode": "unknown"},
    {"revision": 7, "mode": "exchange", "secretHash": "synthetic", "audience": "portal"},
    {"revision": 1, "mode": "shared_identity", "identityProvider": "idp",
     "loginUrl": "https://other.example/login"},
])
def test_unusable_canonical_auth_is_not_a_migration_approval(db_session, tmp_path, config):
    legacy(db_session)
    db_session.add(ContentBlock(id=AUTH_KEY, payload=config))
    db_session.commit()
    before = deepcopy(state(db_session))
    report = inventory(db_session, tmp_path / "invalid.json")
    assert len(report["authGaps"]) == 1
    with pytest.raises(ValueError, match="migration_auth_incomplete"):
        apply(db_session, tmp_path / "invalid.json", report["digest"], writes_frozen=True)
    assert state(db_session) == before
    assert db_session.get(ContentBlock, RECEIPT) is None
