"""Device mutations respect account ownership and the selected capability kind."""

from types import SimpleNamespace
import pytest
from core.capabilities import registry, store
from core.capabilities.ref import cloud_ref, local_ref
from api.routes.v1 import desktop_capabilities as api
from tests.capabilities.test_desktop_capabilities_api import client, USER, PROFILE


def install(kind, key="shared-name", profile=PROFILE, owner=None):
    ref = (
        local_ref(kind, key)
        if profile == "local"
        else cloud_ref("https://cloud.example", kind, key, scope="shared")
    )
    item = registry.upsert(
        profile_id=profile,
        ref=ref,
        content_hash="a" * 64,
        source="local" if profile == "local" else "cloud",
        payload={"owner_user_id": owner} if owner else {},
    )
    entry = {"skill": "SKILL.md", "agent": "agent.json", "plugin": "plugin.json"}[kind]
    store.write_from_files(kind, profile, key, "a" * 12, {entry: "{}"})
    registry.set_state(item.install_id, "ready", resolved_revision="a" * 12)
    return registry.get(item.install_id)


@pytest.mark.parametrize("operation", ["removals", "preparations"])
@pytest.mark.parametrize("profile,owner", [("p_other", None), ("local", "another-user")])
def test_mutation_cannot_touch_other_account(client, operation, profile, owner):
    item = install("skill", profile=profile, owner=owner)
    body = (
        {"install_id": item.install_id, "target": "device"}
        if operation == "removals"
        else {"install_ids": [item.install_id]}
    )
    result = client.post("/v1/desktop/capabilities/" + operation, json=body)
    assert result.status_code == 404
    assert registry.get(item.install_id).ready
    assert store.get("skill", profile, item.key, "a" * 12) is not None


@pytest.mark.parametrize("kind", ["agent", "plugin"])
def test_removal_dispatches_by_kind(client, monkeypatch, kind):
    item = install(kind, profile="local", owner=USER)
    skill = install("skill", profile="local", owner=USER)
    monkeypatch.setattr(api, "_invalidate_capability_caches", lambda: None)
    result = client.post(
        "/v1/desktop/capabilities/removals",
        json={"install_id": item.install_id, "target": "device"},
    )
    assert result.status_code == 200
    assert store.get(kind, "local", item.key, "a" * 12) is None
    assert store.get("skill", "local", skill.key, "a" * 12) is not None
    assert registry.get(skill.install_id).ready


def test_prepare_validates_full_resource_identity(client):
    item = install("skill")
    ref = item.ref.to_dict()
    ref["issuer"] = "different.example"
    result = client.post("/v1/desktop/capabilities/preparations", json={"resource_refs": [ref]})
    assert result.status_code == 404
    assert registry.get(item.install_id).ready


def test_agent_name_preference_is_supported(client, monkeypatch):
    item = install("agent")
    listing = {
        "items": [{"install_id": item.install_id, "runtime_name": "writer"}],
        "kind": "agent",
    }
    monkeypatch.setattr(api, "_agents_view", lambda uid: listing)
    monkeypatch.setattr(api, "_invalidate_capability_caches", lambda: None)
    result = client.put(
        "/v1/desktop/capabilities/name-preferences",
        json={"kind": "agent", "runtime_name": "writer", "install_id": item.install_id},
    )
    assert result.status_code == 200
    assert registry.preferences("agent", user_id=USER)["writer"] == item.install_id


def test_mcp_document_is_scoped_to_active_account(client, monkeypatch):
    from core.capabilities import mcp_json

    doc = SimpleNamespace(
        generation=1,
        digest="sha",
        local={},
        managed={PROFILE: {"servers": {}}, "p_other": {"servers": {"private-other": {}}}},
    )
    monkeypatch.setattr(mcp_json, "load", lambda: doc)
    result = client.get("/v1/desktop/capabilities/mcp-json")
    assert result.status_code == 200
    assert set(result.json()["data"]["managedProfiles"]) == {PROFILE}
    assert result.json()["data"]["digest"] == "sha"


def test_managed_preference_cannot_modify_other_account(client, monkeypatch):
    from core.capabilities import mcp_json

    changed = []
    monkeypatch.setattr(mcp_json, "set_managed_enabled", lambda *args: changed.append(args))
    result = client.put(
        "/v1/desktop/capabilities/mcp-json/managed/p_other/private/ enabled".replace(
            "/ enabled", "/enabled"
        ),
        json={"enabled": False},
    )
    assert result.status_code == 404
    assert not changed


def test_cloud_skill_copy_survives_cloud_update_and_restores(client, monkeypatch):
    from core.capabilities import skills
    from tests.capabilities.test_desktop_capabilities_api import _iid, _Cloud

    monkeypatch.setattr(api, "_invalidate_capability_caches", lambda: None)
    assert client.post("/v1/desktop/capabilities/sync").status_code == 200
    iid = _iid("ppt-design")
    assert client.post("/v1/desktop/capabilities/preparations", json={"install_ids": [iid]}).json()[
        "data"
    ]["results"][0]["ok"]
    response = client.post(f"/v1/desktop/capabilities/installations/{iid}/local-copy", json={})
    assert response.status_code == 200, response.text
    copied = response.json()["data"]["installation"]
    lid = copied["install_id"]
    assert copied["derived_from"] == iid
    assert copied["runtime_name"] == "ppt-design"
    assert lid != iid
    local = registry.get(lid)
    comp = store.get("skill", "local", local.key, local.resolved_revision)
    original = comp.entry_file.read_bytes()
    assert skills.resolve_for_user(USER).chosen["ppt-design"].install_id == lid
    assert (
        comp.entry_file.resolve()
        != store.get(
            "skill", PROFILE, "ppt-design", registry.get(iid).resolved_revision
        ).entry_file.resolve()
    )
    cloud = _Cloud(
        {
            "ppt-design": {
                "SKILL.md": "---\nname: ppt-design\ndescription: changed\n---\nupdated cloud"
            }
        }
    )
    monkeypatch.setattr("httpx.get", cloud.get)
    assert client.post("/v1/desktop/capabilities/sync").status_code == 200
    assert client.post("/v1/desktop/capabilities/preparations", json={"install_ids": [iid]}).json()[
        "data"
    ]["results"][0]["ok"]
    assert comp.entry_file.read_bytes() == original
    assert skills.resolve_for_user(USER).chosen["ppt-design"].install_id == lid
    response = client.put(
        "/v1/desktop/capabilities/name-preferences",
        json={"kind": "skill", "runtime_name": "ppt-design", "install_id": iid},
    )
    assert response.status_code == 200
    assert skills.resolve_for_user(USER).chosen["ppt-design"].install_id == iid
    assert comp.entry_file.read_bytes() == original


def test_local_copy_rejects_other_account_and_corrupt_source(client, monkeypatch):
    monkeypatch.setattr(api, "_invalidate_capability_caches", lambda: None)
    other = install("skill", profile="p_other")
    assert (
        client.post(
            f"/v1/desktop/capabilities/installations/{other.install_id}/local-copy", json={}
        ).status_code
        == 404
    )
    corrupt = install("skill")
    response = client.post(
        f"/v1/desktop/capabilities/installations/{corrupt.install_id}/local-copy", json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "integrity_failed"
    assert registry.list_installations(kind="skill", profile_id="local") == []


def test_disabled_cloud_mcp_remains_visible_and_cannot_win(client, monkeypatch):
    from core.capabilities import mcp_json
    from core.services import desktop_cloud_bridge as bridge
    from core.services.mcp_service import McpServerConfigService

    manifest = {
        "revision": "r1",
        "servers": [{"server_id": "remote", "component": "remote", "tools": [{"name": "run"}]}],
    }
    monkeypatch.setattr(bridge, "get_cached_manifest", lambda: manifest)
    monkeypatch.setattr(mcp_json, "managed_enabled", lambda profile: {"remote": False})
    monkeypatch.setattr(
        McpServerConfigService,
        "get_instance",
        lambda: SimpleNamespace(
            get_all_servers=lambda **kw: {}, get_owned_servers=lambda *a, **kw: {}
        ),
    )
    response = client.get("/v1/desktop/capabilities/installations?kind=mcp")
    assert response.status_code == 200
    remote = next(i for i in response.json()["data"]["items"] if i["server_id"] == "remote")
    assert remote["enabled"] is False and remote["usable"] is False
    assert remote["resolution"]["outcome"] == "unusable"


def test_removal_preserves_referenced_run_files(client, monkeypatch):
    from core.capabilities import runtime

    item = install("skill")
    monkeypatch.setattr(runtime, "references", lambda *args: ["existing-run"])
    result = client.post(
        "/v1/desktop/capabilities/removals",
        json={"install_id": item.install_id, "target": "device"},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "revision_in_use"
    assert store.get("skill", PROFILE, item.key, item.resolved_revision) is not None


@pytest.mark.parametrize("center", ["different-account", None])
def test_signed_cloud_subject_rejects_stale_identity_header(client, monkeypatch, center):
    import base64, json
    from core.auth import desktop_bridge
    from core.services import desktop_cloud_bridge

    claims = (
        base64.urlsafe_b64encode(json.dumps({"u": "cloud-u", "c": center}).encode())
        .decode()
        .rstrip("=")
    )
    monkeypatch.setattr(
        desktop_cloud_bridge,
        "get_identity_state",
        lambda: {"user_center_id": center, "shell_user_center_id": center},
    )
    user_header = base64.b64encode(
        json.dumps({"user_center_id": "current-center"}).encode()
    ).decode()
    request = SimpleNamespace(
        headers={"x-desktop-bridge": "s", "x-desktop-bridge-user": user_header}
    )
    # No database access is allowed for a different signed subject.
    assert desktop_bridge.resolve_bridge_user(request, None) is None


def test_local_copy_edit_is_versioned_and_stale_save_is_rejected(client, monkeypatch):
    from tests.capabilities.test_desktop_capabilities_api import _iid

    monkeypatch.setattr(api, "_invalidate_capability_caches", lambda: None)
    client.post("/v1/desktop/capabilities/sync")
    iid = _iid("ppt-design")
    client.post("/v1/desktop/capabilities/preparations", json={"install_ids": [iid]})
    cloud = registry.get(iid)
    cloud_path = store.get("skill", PROFILE, cloud.key, cloud.resolved_revision).entry_file
    cloud_bytes = cloud_path.read_bytes()
    copy = client.post(f"/v1/desktop/capabilities/installations/{iid}/local-copy", json={}).json()[
        "data"
    ]["installation"]
    local = registry.get(copy["install_id"])
    previous = store.get("skill", "local", local.key, local.resolved_revision).entry_file
    url = f"/v1/desktop/capabilities/installations/{local.install_id}/files/SKILL.md"
    opened = client.get(url).json()["data"]
    updated = opened["content"] + "\nOnly this local copy is edited.\n"
    saved = client.put(url, json={"content": updated, "expected_revision": opened["revision"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["revision"] != opened["revision"]
    assert saved.json()["data"]["installation"]["derived_from"] == iid
    assert previous.read_text() == opened["content"]
    assert cloud_path.read_bytes() == cloud_bytes
    stale = client.put(url, json={"content": "stale edit", "expected_revision": opened["revision"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "install_conflict"
    assert client.get(url).json()["data"]["content"] == updated
    assert (
        client.put(
            f"/v1/desktop/capabilities/installations/{iid}/files/SKILL.md",
            json={"content": updated, "expected_revision": cloud.resolved_revision},
        ).status_code
        == 400
    )


def test_device_enable_does_not_restore_source_revocation(client, monkeypatch):
    from core.capabilities import registry

    item = install("skill")
    monkeypatch.setattr(api, "_invalidate_capability_caches", lambda: None)
    registry.set_state(item.install_id, "ready", payload_update={"source_enabled": False})
    response = client.put(
        f"/v1/desktop/capabilities/installations/{item.install_id}/enabled", json={"enabled": True}
    )
    assert response.status_code == 200
    assert registry.get(item.install_id).enabled is False


@pytest.mark.parametrize("kind", ["skill", "agent", "plugin", "mcp"])
async def test_management_lists_cannot_expose_another_bridged_account(client, monkeypatch, kind):
    from core.capabilities import skills
    from core.services import desktop_cloud_bridge as bridge
    from core.services.mcp_service import McpServerConfigService
    from core.services.user_agent_service import UserAgentService

    if kind != "mcp":
        install(kind, key="private-account-b")
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-account-b")
    monkeypatch.setattr(
        McpServerConfigService,
        "get_instance",
        lambda: SimpleNamespace(
            get_all_servers=lambda **kw: {}, get_owned_servers=lambda *args, **kw: {}
        ),
    )
    monkeypatch.setattr(
        UserAgentService,
        "__init__",
        lambda self, db: setattr(self, "repo", SimpleNamespace(list_for_user=lambda uid: [])),
    )
    monkeypatch.setattr(
        bridge,
        "get_cached_manifest",
        lambda: {
            "servers": [{"server_id": "private-account-b", "tools": [{"name": "private-tool"}]}]
        },
    )
    response = client.get("/v1/desktop/capabilities/installations", params={"kind": kind})
    assert response.status_code == 200
    assert response.json()["data"]["profile_id"] is None
    assert "private-account-b" not in response.text


@pytest.mark.parametrize("operation", ["prepare", "remove", "enable", "copy"])
def test_another_bridged_account_cannot_be_modified_or_copied(client, monkeypatch, operation):
    from core.capabilities import skills

    item = install("skill", key="private-account-b")
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-account-b")
    prefix = "/v1/desktop/capabilities"
    if operation == "prepare":
        response = client.post(prefix + "/preparations", json={"install_ids": [item.install_id]})
    elif operation == "remove":
        response = client.post(
            prefix + "/removals", json={"install_id": item.install_id, "target": "device"}
        )
    elif operation == "enable":
        response = client.put(
            prefix + "/installations/" + item.install_id + "/enabled", json={"enabled": False}
        )
    else:
        response = client.post(
            prefix + "/installations/" + item.install_id + "/local-copy", json={}
        )
    assert response.status_code == 404
    assert registry.get(item.install_id).ready
    assert registry.get(item.install_id).enabled
    assert registry.list_installations(profile_id="local") == []


def test_mcp_document_and_sync_cannot_target_another_bridged_account(client, monkeypatch):
    from core.capabilities import skills, mcp_json

    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-account-b")
    monkeypatch.setattr(
        mcp_json,
        "load",
        lambda: SimpleNamespace(
            generation=1,
            digest="digest",
            local={},
            managed={PROFILE: {"servers": {"private-account-b": {}}}},
        ),
    )
    response = client.get("/v1/desktop/capabilities/mcp-json")
    assert response.status_code == 200 and response.json()["data"]["managedProfiles"] == {}
    response = client.put(
        "/v1/desktop/capabilities/mcp-json/managed/" + PROFILE + "/private-account-b/enabled",
        json={"enabled": False},
    )
    assert response.status_code == 404
    response = client.post("/v1/desktop/capabilities/sync")
    assert response.status_code == 403


def test_local_preparation_does_not_require_a_live_cloud_account(client, monkeypatch):
    from core.services import desktop_cloud_bridge as bridge
    from core.capabilities import skills
    from core.services.desktop_capability_protocol import skill_content_hash

    content = "---\nname: offline-local\ndescription: Offline fixture\n---\nlocal content\n"
    skills.publish_local_skill(
        "offline-local",
        files={"SKILL.md": content},
        content_hash=skill_content_hash(content, {}),
        owner_user_id=USER,
    )
    item = registry.get("skill:local:offline-local")
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    response = client.post(
        "/v1/desktop/capabilities/preparations", json={"install_ids": [item.install_id]}
    )
    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["ok"] is True
