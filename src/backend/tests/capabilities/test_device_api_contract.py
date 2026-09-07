"""本机能力接口只读：来源清单与 mcp.json 投影都不能越账号，同步也要认账号。"""

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
    response = client.post("/v1/desktop/capabilities/sync")
    assert response.status_code == 403


def test_local_skill_stays_listed_without_a_live_cloud_account(client, monkeypatch):
    """云端断线不影响本机技能：它不属于任何云端账号，照样列出且可用。"""
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
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    response = client.get("/v1/desktop/capabilities/installations", params={"kind": "skill"})
    assert response.status_code == 200
    item = next(
        i
        for i in response.json()["data"]["items"]
        if i["install_id"] == "skill:local:offline-local"
    )
    assert item["state"] == "ready" and item["usable"] and item["source"] == "local"
