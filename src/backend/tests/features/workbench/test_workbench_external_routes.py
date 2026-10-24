"""Workbench management → discovery → launch, using real persistence and policy."""

import pytest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import UserShadow
from fastapi.testclient import TestClient
from tests.db_support import share_factory, share_session

BASE = "/v1/workbench"
ADMIN = BASE + "/admin/external-applications"
APP = {
    "schemaVersion": "1.0.0",
    "id": "wb_reports",
    "title": "Reports",
    "description": "Business reports",
    "target": {"kind": "external", "mode": "browser", "url": "https://reports.example.org/home"},
}


@pytest.fixture()
def client(db_session, monkeypatch):
    from api.app import app

    monkeypatch.setenv("CONFIG_TOKEN", "wb-test-config")
    db_session.add(UserShadow(user_id="wb-user", username="Reader", extra_data={}))
    db_session.commit()

    def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    share_session(monkeypatch, db_session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="wb-user", user_center_id="wb-user", username="Reader", email="reader@example.org"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def create(client, declaration=None):
    return client.post(
        ADMIN,
        headers={"Authorization": "Bearer wb-test-config"},
        json={"declaration": declaration or APP, "enabled": True},
    )


def test_admin_can_publish_external_app_and_reader_launches_it(client):
    response = create(client)
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    assert saved["revision"] == 1
    assert saved["applicationKey"] == "external:wb_reports"
    catalog = client.get(BASE + "/applications")
    assert catalog.status_code == 200
    assert catalog.headers["cache-control"] == "private, no-store"
    assert catalog.json()["data"]["items"][0]["declaration"] == APP
    launch = client.post(BASE + "/applications/external:wb_reports/launch", json={})
    assert launch.status_code == 200, launch.text
    assert launch.json()["data"] == {
        "applicationKey": "external:wb_reports",
        "revision": 1,
        "target": APP["target"],
    }
    assert launch.headers["cache-control"] == "no-store"


AUTH = {"Authorization": "Bearer wb-test-config"}


def test_write_requires_config_privilege(client):
    assert client.post(ADMIN, json={"declaration": APP, "enabled": True}).status_code in (401, 403)
    assert (
        client.post(
            ADMIN,
            headers={"Authorization": "Bearer ordinary"},
            json={"declaration": APP, "enabled": True},
        ).status_code
        == 401
    )
    assert client.get(ADMIN).status_code in (401, 403)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hello",
        "//evil.example.org",
        "https://user:password@example.org",
        "https://example.org\\@evil.org",
        "https://example.org/\nattack",
        "https://exa%mple.org",
        "https://example.org/?sso_token=secret",
        "https://example.org/?TOKEN=secret",
        "https://127.1",
        "https://example.org:99999/",
        "https://-invalid.example.org",
        "https://example.org/?access_token=secret",
    ],
)
def test_invalid_urls_are_rejected_without_reflecting_secret(client, url):
    from copy import deepcopy

    declaration = deepcopy(APP)
    declaration["target"]["url"] = url
    response = create(client, declaration)
    assert response.status_code == 422, response.text
    assert "secret" not in response.text
    assert client.get(BASE + "/applications").json()["data"]["items"] == []


def test_unknown_auth_and_callback_configuration_is_rejected(client):
    body = {
        "declaration": APP,
        "enabled": True,
        "callback": "https://evil.example.org",
        "auth": {"token": "secret"},
    }
    result = client.post(ADMIN, headers=AUTH, json=body)
    assert result.status_code == 422
    assert "secret" not in result.text


def test_revision_conflict_disable_and_audit_are_persistent(client, db_session):
    assert create(client).status_code == 200
    updated = {**APP, "title": "Quarterly reports"}
    change = {"declaration": updated, "enabled": False, "revision": 1}
    path = ADMIN + "/external:wb_reports"
    assert client.put(path, headers=AUTH, json=change).json()["data"]["revision"] == 2
    assert client.put(path, headers=AUTH, json=change).status_code == 409
    db_session.expire_all()
    assert client.get(BASE + "/applications").json()["data"]["items"] == []
    assert (
        client.post(BASE + "/applications/external:wb_reports/launch", json={}).json()["detail"][
            "code"
        ]
        == "app_disabled"
    )
    saved = client.get(ADMIN, headers=AUTH).json()["data"]["items"]
    assert saved[0]["revision"] == 2
    assert saved[0]["declaration"]["title"] == "Quarterly reports"
    audit = client.get(BASE + "/admin/audit", headers=AUTH).json()["data"]["items"]
    assert len(audit) == 2
    assert {a["revision"] for a in audit} == {1, 2}
    assert all("https://" not in str(a) for a in audit)
    assert create(client).status_code == 409


def test_users_cannot_choose_another_users_permissions(client, db_session):
    assert create(client).status_code == 200
    row = db_session.get(UserShadow, "wb-user")
    row.extra_data = {"allowed_apps": []}
    db_session.commit()
    assert client.get(BASE + "/applications?user_id=admin").json()["data"]["items"] == []
    assert client.get(BASE + "/applications/external:wb_reports?user_id=admin").status_code == 404
    assert (
        client.post(
            BASE + "/applications/external:wb_reports/launch?user_id=admin", json={}
        ).status_code
        == 404
    )
    row.extra_data = {"allowed_apps": ["wb_reports"]}
    db_session.commit()
    assert (
        client.post(BASE + "/applications/external:wb_reports/launch", json={}).status_code == 200
    )
    row.extra_data = {"allowed_apps": "*"}
    db_session.commit()
    assert len(client.get(BASE + "/applications").json()["data"]["items"]) == 1


def test_legacy_definitions_cannot_be_shadowed(client, db_session):
    from core.db.models import ContentBlock

    db_session.add(
        ContentBlock(
            id="app_config",
            payload={
                "apps": [
                    {
                        "id": "wb_reports",
                        "name": "Legacy",
                        "url": "https://legacy.example.org",
                        "enabled": True,
                    }
                ]
            },
        )
    )
    db_session.commit()
    assert create(client).status_code == 409


def test_legacy_editor_cannot_overwrite_new_definition(client):
    assert create(client).status_code == 200
    response = client.put(
        "/v1/content/app_config",
        headers=AUTH,
        json={
            "payload": {
                "apps": [
                    {
                        "id": "wb_reports",
                        "name": "Legacy",
                        "url": "https://legacy.example.org",
                        "enabled": True,
                    }
                ]
            }
        },
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize(
    "payload", [{"apps": None}, {"apps": [{"id": ["odd"]}]}, {"apps": [{"id": {"odd": 1}}]}]
)
def test_legacy_normalizable_payloads_do_not_become_server_errors(client, payload):
    response = client.put("/v1/content/app_config", headers=AUTH, json={"payload": payload})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "endpoint", ["/v1/content/app_config", "/v1/content/docs/app_config", "/v1/content/docs/import"]
)
def test_all_legacy_writes_reject_normalized_duplicate_ids(client, monkeypatch, endpoint):
    monkeypatch.setenv("ADMIN_TOKEN", "wb-test-admin")
    assert create(client).status_code == 200
    payload = {
        "apps": [{"id": " wb_reports ", "name": "Legacy", "url": "https://legacy.example.org"}]
    }
    if endpoint.endswith("/import"):
        body = {"schema_version": 1, "blocks": {"app_config": {"payload": payload}}}
        method = client.post
    else:
        body = {"payload": payload}
        method = client.put
    headers = {"Authorization": "Bearer wb-test-admin"} if "/docs/" in endpoint else AUTH
    response = method(endpoint, headers=headers, json=body)
    assert response.status_code == 409, response.text


def test_concurrent_new_and_legacy_definitions_never_share_effective_id(client, db_session, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from api.app import app
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)
    # Both HTTP operations start together. Their independent DB sessions must
    # serialize the namespace decision even when app_config does not yet exist.
    for n in range(8):
        identifier = f"wb_race_{n}"
        barrier = Barrier(2)

        def modern():
            barrier.wait()
            declaration = {**APP, "id": identifier}
            return create(client, declaration).status_code

        def legacy():
            barrier.wait()
            return client.put(
                "/v1/content/app_config",
                headers=AUTH,
                json={
                    "payload": {
                        "apps": [
                            {
                                "id": identifier,
                                "name": "Legacy",
                                "url": "https://legacy.example.org",
                            }
                        ]
                    }
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(modern), pool.submit(legacy)
            assert sorted([a.result(), b.result()]) == [200, 409]


def test_management_rejects_simple_cross_origin_writes(client):
    import json

    response = client.post(
        ADMIN, headers=AUTH, content=json.dumps({"declaration": APP, "enabled": True})
    )
    assert response.status_code == 415
    assert client.get(BASE + "/applications").json()["data"]["items"] == []
