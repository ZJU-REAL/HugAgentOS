"""Workbench plugin public HTTP seam; isolated fixture DB only."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import UserShadow
from tests.features.workbench.workbench_plugin_support import package
from tests.db_support import share_factory, share_session


@pytest.fixture
def client(db_session, monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGIN_ASSET_CACHE_DIR", str(tmp_path / "plugin-assets"))
    from features.workbench.api.workbench import router
    from api.routes.v1.plugins import router as plugins

    app = FastAPI()
    from core.infra.exceptions import AppException
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppException)
    async def app_error(request, error):
        return JSONResponse({"code": error.code}, status_code=error.status_code)

    app.include_router(router)
    app.include_router(plugins)
    db_session.add(
        UserShadow(user_id="wb07", username="WB07", extra_data={"can_import_plugin": True})
    )
    db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="wb07", user_center_id="wb07", username="WB07", email=""
    )
    app.dependency_overrides[get_db] = lambda: db_session
    share_session(monkeypatch, db_session)
    return TestClient(app)


def upload(client, **kwargs):
    result = client.post(
        "/v1/plugins/import", files={"file": ("plugin.zip", package(**kwargs), "application/zip")}
    )
    assert result.status_code == 201, result.text
    return result.json()["data"]["install_id"]


def plugin_items(client):
    return [
        i
        for i in client.get("/v1/workbench/catalog").json()["data"]["items"]
        if i["declaration"]["target"]["kind"] == "plugin"
    ]


def launched(client):
    item = plugin_items(client)[0]
    result = client.post("/v1/workbench/applications/" + item["applicationKey"] + "/launch")
    assert result.status_code == 200, result.text
    return result.json()["data"]


def test_ui_only_upload_launch_disable_and_uninstall(client):
    installed = upload(client)
    item = plugin_items(client)[0]
    assert item["binding"]["installId"] == installed
    launch = launched(client)
    response = client.get(launch["assetPath"])
    assert response.status_code == 200, response.text
    assert "Package page" in response.text
    assert "connect-src 'none'" in response.headers["content-security-policy"]
    assert (
        client.patch(
            "/v1/plugins/installed/" + installed + "/enable", json={"enabled": False}
        ).status_code
        == 200
    )
    assert plugin_items(client) == []
    assert client.get(launch["assetPath"]).status_code == 404
    assert (
        client.patch(
            "/v1/plugins/installed/" + installed + "/enable", json={"enabled": True}
        ).status_code
        == 200
    )
    assert plugin_items(client)
    assert client.delete("/v1/plugins/installed/" + installed).status_code == 200
    assert not plugin_items(client)


def test_broken_entry_is_isolated(client):
    upload(client, entry="web/missing.html")
    item = plugin_items(client)[0]
    result = client.post("/v1/workbench/applications/" + item["applicationKey"] + "/launch")
    assert result.status_code == 404
    assert client.get("/v1/workbench/catalog").status_code == 200


def test_unknown_required_capability_is_not_launchable(client):
    # An invalid UI-only package has no valid contribution and cannot install.
    response = client.post(
        "/v1/plugins/import",
        files={"file": ("plugin.zip", package(required=["dangerous.native"]), "application/zip")},
    )
    assert response.status_code in (400, 422)
    assert plugin_items(client) == []


def test_wrong_user_and_version_cannot_read_package(client, db_session):
    upload(client)
    launch = launched(client)
    assert (
        client.get(launch["assetPath"].replace(launch["revision"], "old-version")).status_code
        == 409
    )
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="another", user_center_id="another", username="Other"
    )
    assert client.get(launch["assetPath"]).status_code == 404
    assert (
        client.post(
            "/v1/workbench/applications/" + launch["applicationKey"] + "/launch"
        ).status_code
        == 404
    )


def test_visibility_does_not_grant_package_access(client, db_session):
    upload(client)
    launch = launched(client)
    user = db_session.get(UserShadow, "wb07")
    user.extra_data = {"allowed_apps": []}
    db_session.commit()
    assert plugin_items(client) == []
    assert client.get(launch["assetPath"]).status_code == 404


def test_assets_cannot_escape_or_read_prior_revision(client):
    upload(client)
    launch = launched(client)
    path = launch["assetPath"].rsplit("/", 1)[0]
    for relative in ["..%2Fplugin.json", "%2e%2e%2F%2e%2e%2Fplugin.json"]:
        assert client.get(path + "/" + relative).status_code == 404


def test_known_but_unimplemented_required_capability_blocks_launch(client):
    upload(client, required=["canvas.new_tab"])
    item = plugin_items(client)[0]
    assert item["availability"] == {"available": False, "reason": "host_incompatible"}
    result = client.post("/v1/workbench/applications/" + item["applicationKey"] + "/launch")
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "host_incompatible"


def test_plugin_cannot_borrow_external_id_permission(client, db_session):
    upload(client)
    launch = launched(client)
    user = db_session.get(UserShadow, "wb07")
    user.extra_data = {"allowed_apps": ["reports"]}
    db_session.commit()
    assert plugin_items(client) == []
    assert (
        client.post(
            "/v1/workbench/applications/" + launch["applicationKey"] + "/launch"
        ).status_code
        == 404
    )
    user.extra_data = {"allowed_apps": [launch["applicationKey"]]}
    db_session.commit()
    assert len(plugin_items(client)) == 1
    assert launched(client)["applicationKey"] == launch["applicationKey"]


def test_css_expansion_is_bounded_before_large_allocation(client):
    archive = package(
        html='<link rel="stylesheet" href="style.css"><h1>Bounded</h1>',
        assets={
            "web/style.css": "body{background:url(big.png)}" * 1000,
            "web/big.png": b"x" * (8 * 1024 * 1024),
        },
    )
    response = client.post(
        "/v1/plugins/import", files={"file": ("plugin.zip", archive, "application/zip")}
    )
    assert response.status_code == 201, response.text
    launch = launched(client)
    response = client.get(launch["assetPath"])
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "page_too_large"


def test_simultaneous_first_switches_are_atomic(client, db_session, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import sessionmaker

    installed = upload(client)
    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)

    def toggle(value):
        return client.patch(
            "/v1/plugins/installed/" + installed + "/enable", json={"enabled": value}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(toggle, [True, False])) == [200, 200]
    assert toggle(True) == 200
    assert len(plugin_items(client)) == 1
    db_session.expire_all()
    assert db_session.get(UserShadow, "wb07").extra_data == {"can_import_plugin": True}


def test_reinstall_does_not_reuse_old_installation_access(client, monkeypatch):
    from datetime import datetime, timezone
    from core.infra import time

    class Clock(datetime):
        instant = datetime(2026, 10, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant if tz else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(time, "datetime", Clock)
    installed = upload(client)
    old = launched(client)
    assert "20261014T120000.123456Z" in old["applicationKey"]
    assert client.delete("/v1/plugins/installed/" + installed).status_code == 200
    Clock.instant = Clock.instant.replace(microsecond=123457)
    upload(client)
    new = launched(client)
    assert "20261014T120000.123457Z" in new["applicationKey"]
    assert new["applicationKey"] != old["applicationKey"]
    assert client.get(old["assetPath"]).status_code == 404
    assert (
        client.post("/v1/workbench/applications/" + old["applicationKey"] + "/launch").status_code
        == 404
    )


def test_catalog_plugin_key_can_be_pinned_and_survives_refresh_and_upgrade(client):
    upload(client)
    key = plugin_items(client)[0]["applicationKey"]
    endpoint = "/v1/workbench/me/navigation-preferences"
    body = {"revision": 0, "pinned": [key], "order": [key]}
    saved = client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["pinned"] == [key]
    assert client.get(endpoint).json()["data"]["order"] == [key]
    assert plugin_items(client)[0]["applicationKey"] == key
    upload(client, html="<h1>Updated package</h1>")
    assert plugin_items(client)[0]["applicationKey"] == key
    assert client.get(endpoint).json()["data"]["pinned"] == [key]
