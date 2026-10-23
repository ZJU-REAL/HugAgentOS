"""Upgrade behavior through authenticated upload, decision and launch HTTP boundaries."""

import io
import json
import zipfile
from urllib.parse import quote
from tests.features.workbench.test_workbench_plugin_routes import client, upload, plugin_items, launched
from tests.features.workbench.workbench_plugin_support import package


def upgraded(*, grants=None, permissions=None, state=None, required=None, html="New revision"):
    source = zipfile.ZipFile(io.BytesIO(package(html=html)))
    manifest = json.loads(source.read("plugin.json"))
    manifest["version"] = "2.0.0"
    c = manifest["extensions"]["org.hugagent"]["ui"]["contributes"]
    c["modules"][0]["grants"] = grants or []
    if permissions:
        c["applications"][0]["permissions"] = permissions
    if state:
        c["applications"][0]["lifecycle"] = {"stateSchemaVersion": state}
    if required:
        c["applications"][0]["compatibility"] = {"requiredCapabilities": required}
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as z:
        z.writestr("plugin.json", json.dumps(manifest))
        z.writestr("web/index.html", html)
    return result.getvalue()


def import_upgrade(client, **options):
    return client.post(
        "/v1/plugins/import", files={"file": ("plugin.zip", upgraded(**options), "application/zip")}
    )


def endpoint(install):
    return "/v1/workbench/plugin-upgrades/" + quote(install, safe="")


def test_permission_expansion_retains_old_revision_until_owner_approves(client):
    install = upload(client)
    old = launched(client)
    response = import_upgrade(client, grants=["file.save"])
    assert response.status_code == 201, response.text
    assert response.json()["data"]["action"] == "approval_required"
    assert launched(client)["revision"] == old["revision"]
    pending = client.get(endpoint(install)).json()["data"]
    assert pending["addedPermissions"] == ["module:page:file.save"]
    approved = client.post(
        endpoint(install) + "/decision",
        json={
            "expectedRevision": old["revision"],
            "candidateRevision": pending["candidateRevision"],
            "decision": "approve",
        },
    )
    assert approved.status_code == 200, approved.text
    new = launched(client)
    assert new["applicationKey"] == old["applicationKey"]
    assert new["revision"] != old["revision"]
    assert client.get(new["assetPath"]).text == "New revision"


def decision(client, install, old, *, choice="approve", reset=False, candidate=None):
    pending = client.get(endpoint(install)).json()["data"]
    return client.post(
        endpoint(install) + "/decision",
        json={
            "expectedRevision": old["revision"],
            "candidateRevision": candidate or pending["candidateRevision"],
            "decision": choice,
            "resetState": reset,
        },
    )


def test_rejection_preserves_verified_package_and_does_not_grant(client):
    install = upload(client)
    old = launched(client)
    import_upgrade(client, permissions=["sensitive-data"])
    assert decision(client, install, old, choice="reject").status_code == 200
    assert client.get(endpoint(install)).json()["data"]["status"] == "rejected"
    assert launched(client)["revision"] == old["revision"]
    assert client.get(old["assetPath"]).text == "<h1>Package page</h1>"
    assert decision(client, install, old).status_code == 409


def test_old_leased_asset_is_exact_and_revocation_is_immediate(client):
    install = upload(client)
    old = launched(client)
    key = quote(old["applicationKey"], safe="")
    body = {"revision": old["revision"], "instanceId": "running_instance_1234"}
    lease = "/v1/workbench/applications/" + key + "/revision-leases"
    assert client.post(lease, json=body).status_code == 200
    assert import_upgrade(client).status_code == 201
    new = launched(client)
    assert new["revision"] != old["revision"]
    assert client.get(old["assetPath"]).status_code == 409
    url = old["assetPath"] + "?instanceId=running_instance_1234"
    assert client.get(url).text == "<h1>Package page</h1>"
    assert client.get(new["assetPath"]).text == "New revision"
    assert client.put(lease, json=body).status_code == 200
    assert (
        client.patch(
            "/v1/plugins/installed/" + install + "/enable", json={"enabled": False}
        ).status_code
        == 200
    )
    assert client.get(url).status_code == 404
    assert client.put(lease, json=body).status_code == 404


def test_schema_upgrade_requires_explicit_reset_and_failure_preserves_old(client):
    install = upload(client)
    old = launched(client)
    assert import_upgrade(client, state="2").status_code == 201
    assert decision(client, install, old).status_code == 409
    assert launched(client)["revision"] == old["revision"]
    assert decision(client, install, old, reset=True).status_code == 200
    assert launched(client)["revision"] != old["revision"]


def test_incompatible_host_or_missing_asset_cannot_replace_verified_install(client):
    upload(client)
    old = launched(client)
    # data.query is supported; legacy canvas.open remains unavailable to workspace modules.
    assert import_upgrade(client, required=["canvas.open"]).status_code == 409
    assert launched(client)["revision"] == old["revision"]
    response = client.post(
        "/v1/plugins/import",
        files={"file": ("plugin.zip", package(entry="web/missing.html"), "application/zip")},
    )
    assert response.status_code == 404
    assert launched(client)["revision"] == old["revision"]


def test_superseded_candidate_and_wrong_owner_cannot_approve(client):
    from core.auth.backend import UserContext, get_current_user

    install = upload(client)
    old = launched(client)
    import_upgrade(client, grants=["file.save"])
    first = client.get(endpoint(install)).json()["data"]["candidateRevision"]
    import_upgrade(client, permissions=["private-data"])
    assert decision(client, install, old, candidate=first).status_code == 409
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other", user_center_id="other", username="Other"
    )
    assert client.get(endpoint(install)).status_code == 404


def test_closed_leases_cannot_read_old_assets(client):
    upload(client)
    old = launched(client)
    lease = (
        "/v1/workbench/applications/" + quote(old["applicationKey"], safe="") + "/revision-leases"
    )
    body = {"revision": old["revision"], "instanceId": "closed_instance_1234"}
    assert client.post(lease, json=body).status_code == 200
    import_upgrade(client)
    url = old["assetPath"] + "?instanceId=closed_instance_1234"
    assert client.get(url).status_code == 200
    assert client.post(lease + "/close", json=body).status_code == 200
    assert client.get(url).status_code == 410
    assert client.post(lease, json=body).status_code == 409
