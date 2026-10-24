"""Workbench activity HTTP boundary, including unread projection without launch."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from tests.db_support import share_factory, share_session


@pytest.fixture()
def client(db_session, monkeypatch):
    from features.workbench.api.workbench import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    share_session(monkeypatch, db_session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice", user_center_id="alice", username="Alice"
    )
    return TestClient(app)


def test_unopened_catalog_activity_does_not_create_instances(client, db_session):
    from core.db.models import ContentBlock

    # Persistent service identity is provisioned independently of application instances.
    from features.workbench import origin
    origin.current(db_session)
    before = db_session.query(ContentBlock).count()
    response = client.get("/v1/workbench/me/activity")
    assert response.status_code == 200
    assert response.json()["data"] == {"badges": {}, "notifications": [], "background": []}
    assert db_session.query(ContentBlock).count() == before


def test_unknown_instance_cannot_publish_or_grant_itself(client):
    path = "/v1/workbench/applications/builtin:chat/instances/unopened/activity"
    assert client.put(path + "/badge", json={"count": 3}).status_code == 404
    assert client.post(path + "/notification", json={"title": "x", "body": "y"}).status_code == 404


@pytest.fixture()
def plugin(client, db_session):
    import io
    import json
    import zipfile
    from api.routes.v1.plugins import router
    from core.db.models import UserShadow
    from tests.features.workbench.workbench_plugin_support import package

    client.app.include_router(router)
    db_session.add(
        UserShadow(user_id="alice", username="Alice", extra_data={"can_import_plugin": True})
    )
    db_session.commit()
    old = zipfile.ZipFile(io.BytesIO(package()))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in old.namelist():
            content = old.read(name)
            if name == "plugin.json":
                manifest = json.loads(content)
                ui = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
                ui["applications"][0]["permissions"] = ["badge", "notification", "background"]
                ui["modules"][0]["grants"] = ["badge.update", "notification.show", "background"]
                content = json.dumps(manifest)
            archive.writestr(name, content)
    response = client.post(
        "/v1/plugins/import", files={"file": ("plugin.zip", output.getvalue(), "application/zip")}
    )
    assert response.status_code == 201, response.text
    item = next(
        i
        for i in client.get("/v1/workbench/catalog").json()["data"]["items"]
        if i["declaration"]["target"]["kind"] == "plugin"
    )
    binding = {"revision": str(item["revision"]), **item["binding"]}
    path = (
        "/v1/workbench/applications/" + item["applicationKey"] + "/instances/hostinstance/activity"
    )
    assert client.post(path, json={"binding": binding}).status_code == 200
    return path, item, binding


def approve(client, path, capabilities):
    response = client.post(path + "/consent", json={"capabilities": capabilities})
    assert response.status_code == 200, response.text


def test_badge_needs_host_consent_is_bounded_and_survives_page_close(client, plugin):
    path, item, _ = plugin
    assert client.put(path + "/badge", json={"count": 3}).status_code == 403
    approve(client, path, ["badge"])
    for count in [1, 2, 3, 4, 5]:
        assert client.put(path + "/badge", json={"count": count}).status_code == 200
    assert client.put(path + "/badge", json={"count": 6}).status_code == 429
    assert client.put(path + "/badge", json={"count": True}).status_code == 422
    assert client.put(path + "/badge", json={"count": 10000}).status_code == 422
    assert client.delete(path).status_code == 200
    projection = client.get("/v1/workbench/me/activity").json()["data"]
    assert projection["badges"] == {item["applicationKey"]: {"count": 5}}
    assert projection["background"] == []
    assert client.put(path + "/badge", json={"count": 0}).status_code == 404


def test_notifications_plain_text_limited_and_dismissable(client, plugin):
    path, _, _ = plugin
    approve(client, path, ["notification"])
    assert (
        client.post(path + "/notification", json={"title": "x", "body": "\u0000"}).status_code
        == 422
    )
    assert (
        client.post(path + "/notification", json={"title": "x" * 81, "body": "x"}).status_code
        == 422
    )
    for index in range(3):
        assert (
            client.post(
                path + "/notification", json={"title": "Update", "body": str(index)}
            ).status_code
            == 200
        )
    assert (
        client.post(path + "/notification", json={"title": "Update", "body": "4"}).status_code
        == 429
    )
    projection = client.get("/v1/workbench/me/activity").json()["data"]
    assert len(projection["notifications"]) == 3
    notification = projection["notifications"][0]
    assert (
        client.delete("/v1/workbench/me/activity/notifications/" + notification["id"]).status_code
        == 200
    )
    assert len(client.get("/v1/workbench/me/activity").json()["data"]["notifications"]) == 2
    approve(client, path, [])
    assert client.get("/v1/workbench/me/activity").json()["data"]["notifications"] == []


def test_account_disable_revision_and_self_reported_gesture_do_not_grant(client, plugin):
    path, item, binding = plugin
    assert (
        client.post(
            path + "/consent", json={"capabilities": ["badge"], "gesture": True}
        ).status_code
        == 422
    )
    assert client.post(path, json={"binding": {**binding, "revision": "old"}}).status_code == 409
    approve(client, path, ["badge"])
    assert client.put(path + "/badge", json={"count": 4}).status_code == 200
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="bob", user_center_id="bob", username="Bob"
    )
    assert client.put(path + "/badge", json={"count": 1}).status_code == 404
    assert client.get("/v1/workbench/me/activity").json()["data"]["badges"] == {}
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice", user_center_id="alice", username="Alice"
    )
    response = client.patch(
        "/v1/plugins/installed/" + binding["installId"] + "/enable", json={"enabled": False}
    )
    assert response.status_code == 200, response.text
    assert client.get("/v1/workbench/me/activity").json()["data"]["badges"] == {}
    assert client.put(path + "/badge", json={"count": 1}).status_code == 404


def test_activity_registration_does_not_grant_background_resource(client, plugin):
    path, _, _ = plugin
    assert client.post(path + "/background", json={"resourceId": "unopened"}).status_code == 403
    approve(client, path, ["background"])
    assert client.post(path + "/background", json={"resourceId": "unopened"}).status_code == 404
    assert client.get("/v1/workbench/me/activity").json()["data"]["background"] == []


def test_module_scope_cannot_be_forged(client, plugin):
    path, _, binding = plugin
    approve(client, path, ["badge", "notification"])
    assert client.put(path + "/badge", json={"count": 1, "moduleId": "another"}).status_code == 409
    assert (
        client.post(
            path + "/notification", json={"title": "x", "body": "y", "moduleId": "another"}
        ).status_code
        == 409
    )
    assert client.post(path, json={"binding": binding, "moduleId": "another"}).status_code == 403


def test_concurrent_badge_storm_has_a_durable_shared_budget(client, plugin, db_session, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import sessionmaker

    path, _, _ = plugin
    approve(client, path, ["badge"])
    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)

    def publish(index):
        return client.put(path + "/badge", json={"count": index + 1}).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(publish, range(10)))
    assert statuses.count(200) == 5
    assert statuses.count(429) == 5


def test_dismissal_does_not_reset_user_notification_budget(client, plugin):
    path, item, binding = plugin
    approve(client, path, ["notification"])
    for index in range(10):
        current = path.replace("hostinstance", "instance" + str(index % 4))
        assert client.post(current, json={"binding": binding}).status_code == 200
        response = client.post(current + "/notification", json={"title": "x", "body": "x"})
        assert response.status_code == 200
        nid = response.json()["data"]["id"]
        assert client.delete("/v1/workbench/me/activity/notifications/" + nid).status_code == 200
    current = path.replace("hostinstance", "lastinstance")
    assert client.post(current, json={"binding": binding}).status_code == 200
    assert (
        client.post(current + "/notification", json={"title": "x", "body": "x"}).status_code == 429
    )
