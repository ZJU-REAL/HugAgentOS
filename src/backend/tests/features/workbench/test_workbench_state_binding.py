"""Exact installed package and revocation at the HTTP boundary."""

from urllib.parse import urlencode
from core.auth.backend import UserContext, get_current_user
from tests.features.workbench.test_workbench_plugin_routes import client as client, upload, launched


def test_installed_state_requires_exact_binding_and_stops_on_revoke(client):
    install_id = upload(client)
    launch = launched(client)
    binding = launch["binding"]
    base = "/v1/workbench/me/application-state/" + launch["applicationKey"]
    scope = {
        "packageRevision": launch["revision"],
        "installId": install_id,
        "executionScope": binding["executionScope"],
    }
    if binding.get("origin"):
        scope["origin"] = binding["origin"]["id"]
    url = base + "?" + urlencode(scope)
    body = {"revision": 0, "schemaVersion": "1", "payload": {"filter": "recent"}}
    assert client.put(url, json=body).status_code == 200
    assert client.get(url).json()["data"]["payload"] == body["payload"]
    for invalid in [
        {"installId": "wrong"},
        {"packageRevision": "old"},
        {"origin": "wrong"},
        {"executionScope": "local"},
    ]:
        assert client.get(base + "?" + urlencode({**scope, **invalid})).status_code == 409
    original = client.app.dependency_overrides[get_current_user]
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="another", user_center_id="another", username="Other"
    )
    assert client.get(url).status_code == 404
    assert client.put(url, json={**body, "revision": 1}).status_code == 404
    client.app.dependency_overrides[get_current_user] = original
    assert (
        client.patch(
            "/v1/plugins/installed/" + install_id + "/enable", json={"enabled": False}
        ).status_code
        == 200
    )
    assert client.get(url).status_code == 404
    assert client.put(url, json={**body, "revision": 1}).status_code == 404
