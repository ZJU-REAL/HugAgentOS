"""WB08 public HTTP boundary: exact installation, disabled discovery and declared views."""
import io
import json
import zipfile
from urllib.parse import quote
from core.auth.backend import UserContext, get_current_user
from tests.features.workbench.test_workbench_plugin_routes import client, upload, plugin_items
from tests.features.workbench.workbench_plugin_support import package

def test_disabled_deep_link_retains_safe_reason_without_changing_directory(client):
    installed = upload(client)
    item = plugin_items(client)[0]
    path = "/v1/workbench/applications/" + quote(item["applicationKey"], safe="")
    assert client.patch("/v1/plugins/installed/" + installed + "/enable", json={"enabled": False}).status_code == 200
    assert plugin_items(client) == []
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["data"]["availability"] == {"available": False, "reason": "app_disabled"}
    assert client.post(path + "/launch").status_code == 404
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="other", user_center_id="other", username="other", email="")
    assert client.get(path).status_code == 404

def test_secondary_module_launch_is_limited_to_this_application_views(client):
    raw = zipfile.ZipFile(io.BytesIO(package()))
    manifest = json.loads(raw.read("plugin.json"))
    contributions = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
    contributions["modules"].append({"id": "detail", "surface": "workspace", "entry": "web/detail.html", "title": "Details", "grants": []})
    contributions["modules"].append({"id": "unlisted", "surface": "workspace", "entry": "web/unlisted.html", "title": "Other", "grants": []})
    contributions["applications"][0]["views"] = [{"id": "details", "title": "Details", "path": "details", "moduleId": "detail"}]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr("web/index.html", "<h1>Main</h1>")
        archive.writestr("web/detail.html", "<h1>Details</h1>")
        archive.writestr("web/unlisted.html", "<h1>Other</h1>")
    response = client.post("/v1/plugins/import", files={"file": ("plugin.zip", output.getvalue(), "application/zip")})
    assert response.status_code == 201, response.text
    item = plugin_items(client)[0]
    path = "/v1/workbench/applications/" + quote(item["applicationKey"], safe="") + "/launch"
    response = client.post(path, params={"moduleId": "detail"})
    assert response.status_code == 200, response.text
    launch = response.json()["data"]
    assert launch["module"]["id"] == "detail"
    assert "<h1>Details</h1>" in client.get(launch["assetPath"]).text
    assert client.post(path, params={"moduleId": "unlisted"}).status_code == 404
    assert client.post(path, params={"moduleId": "../detail"}).status_code == 404
