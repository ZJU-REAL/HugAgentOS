"""WB16 public HTTP source and exact-install boundary."""

from tests.features.workbench.test_workbench_plugin_routes import client, upload, launched, plugin_items
from core.db.models import ContentBlock


def test_local_execution_origin_and_offline_page(client, monkeypatch):
    from core.config import local_mode

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    upload(client)
    item = plugin_items(client)[0]
    assert item["binding"]["executionScope"] == "local"
    assert item["binding"]["origin"] == item["origin"]
    assert item["origin"]["kind"] == "local"
    value = launched(client)
    assert value["context"]["origin"] == item["origin"]
    assert value["context"]["executionScope"] == "local"
    assert value["applicationKey"].startswith("plugin:local:" + item["origin"]["id"] + ":")
    response = client.get(value["assetPath"])
    assert response.status_code == 200
    assert "Package page" in response.text
    assert "connect-src 'none'" in response.headers["content-security-policy"]


def test_service_change_never_reuses_installation_key(client, db_session):
    upload(client)
    old = launched(client)
    origin = old["binding"]["origin"]
    row = db_session.get(ContentBlock, "workbench_origin_identity")
    row.payload = {"id": "a" * 32 if origin["id"] != "a" * 32 else "b" * 32}
    db_session.commit()
    new = launched(client)
    assert new["binding"]["installId"] == old["binding"]["installId"]
    assert new["applicationKey"] != old["applicationKey"]
    assert client.get(old["assetPath"]).status_code == 404
    assert (
        client.post("/v1/workbench/applications/" + old["applicationKey"] + "/launch").status_code
        == 404
    )


def test_source_header_cannot_choose_other_service(client):
    upload(client)
    value = launched(client)
    response = client.post(
        "/v1/workbench/applications/" + value["applicationKey"] + "/launch",
        headers={"x-workbench-origin": "0" * 32},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "origin_mismatch"
    response = client.get(value["assetPath"] + "?hg_origin=" + "0" * 32)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "origin_mismatch"


def test_directory_origin_is_stable_and_minimal(client):
    upload(client)
    first = client.get("/v1/workbench/catalog").json()["data"]
    second = client.get("/v1/workbench/catalog").json()["data"]
    assert first["origin"] == second["origin"]
    assert set(first["origin"]) == {"kind", "id"}
    assert len(first["origin"]["id"]) == 32
    assert first["origin"]["kind"] == "cloud"
    plugin = plugin_items(client)[0]
    assert plugin["origin"] == first["origin"]
    assert "package" not in plugin["binding"]


def test_cloud_preferences_reject_device_only_reference(client):
    upload(client)
    local_key = launched(client)["applicationKey"].replace(":cloud:", ":local:", 1)
    response = client.put(
        "/v1/workbench/me/navigation-preferences",
        json={
            "revision": 0,
            "pinned": [local_key],
            "order": [local_key],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_reference_origin"


def test_missing_packaged_sdk_asset_is_explicit(client):
    from tests.features.workbench.workbench_plugin_support import package

    response = client.post(
        "/v1/plugins/import",
        files={
            "file": (
                "plugin.zip",
                package(html='<script src="missing-sdk.js"></script>'),
                "application/zip",
            ),
        },
    )
    assert response.status_code == 201
    value = launched(client)
    response = client.get(value["assetPath"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "package_asset_invalid"


def test_complete_package_css_script_and_images_need_no_network(client):
    from tests.features.workbench.workbench_plugin_support import package

    response = client.post(
        "/v1/plugins/import",
        files={
            "file": (
                "plugin.zip",
                package(
                    html='<link rel="stylesheet" href="style.css"><img src="pic.png"><script src="page.js"></script>',
                    assets={
                        "web/style.css": "body{background:url(pic.png)}",
                        "web/pic.png": b"png",
                        "web/page.js": "document.title='offline';",
                    },
                ),
                "application/zip",
            ),
        },
    )
    assert response.status_code == 201
    value = launched(client)
    response = client.get(value["assetPath"])
    assert response.status_code == 200
    assert "data:image/png;base64," in response.text
    assert "document.title='offline'" in response.text
    assert "<script src=" not in response.text
    assert '<link rel="stylesheet"' not in response.text


def test_device_registry_install_launch_is_exact_and_owned(
    client, db_session, monkeypatch, tmp_path
):
    from sqlalchemy.orm import sessionmaker
    from core.config import local_mode
    from features.desktop.capabilities import registry

    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(tmp_path / "caps"))
    monkeypatch.setattr(registry, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    from datetime import datetime, timezone
    from core.infra import time

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(time, "datetime", Clock)
    installed = upload(client)
    values = plugin_items(client)
    assert len(values) == 1
    value = launched(client)
    assert value["binding"]["installId"] == installed
    assert value["binding"]["executionScope"] == "local"
    assert client.get(value["assetPath"]).status_code == 200
    assert client.delete("/v1/plugins/installed/" + installed).status_code == 200
    upload(client)
    new = launched(client)
    assert new["applicationKey"] != value["applicationKey"]
    assert client.get(value["assetPath"]).status_code == 404
    value = new
    from core.auth.backend import UserContext, get_current_user

    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="another",
        user_center_id="another",
        username="Other",
    )
    assert client.get(value["assetPath"]).status_code == 404
    assert (
        client.post("/v1/workbench/applications/" + value["applicationKey"] + "/launch").status_code
        == 404
    )


def test_declared_execution_scope_cannot_launch_on_another_source(client):
    import io
    import json
    import zipfile
    from tests.features.workbench.workbench_plugin_support import package

    original = zipfile.ZipFile(io.BytesIO(package()))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in original.namelist():
            content = original.read(name)
            if name == "plugin.json":
                manifest = json.loads(content)
                app = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]["applications"][0]
                app["availability"] = {"executionScopes": ["local"]}
                content = json.dumps(manifest).encode()
            archive.writestr(name, content)
    response = client.post(
        "/v1/plugins/import",
        files={
            "file": ("plugin.zip", output.getvalue(), "application/zip"),
        },
    )
    assert response.status_code == 201
    item = plugin_items(client)[0]
    assert item["availability"] == {"available": False, "reason": "execution_scope_unsupported"}
    response = client.post("/v1/workbench/applications/" + item["applicationKey"] + "/launch")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "execution_scope_unsupported"
