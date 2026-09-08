"""Downloaded capability bytes are distinct from platform/dependency readiness."""

import json
from types import SimpleNamespace
import pytest
from api.routes.v1 import desktop_capabilities as api
from core.capabilities import registry, store
from core.capabilities.ref import cloud_ref
from core.services import desktop_cloud_bridge as bridge
from core.services.desktop_capability_protocol import entity_content_hash
from tests.capabilities.test_desktop_capabilities_api import client, USER, PROFILE, _Cloud


def _cloud_skill(client, monkeypatch, metadata, key="requires-runtime", sync=True):
    """同步即准备：这一次 sync 之后技能字节就已经在本机，没有手动准备这一步。"""
    md = "---\nname: " + key + "\ndescription: test\n" + metadata + "\n---\nSynthetic skill\n"
    cloud = _Cloud({key: {"SKILL.md": md}})
    monkeypatch.setattr("httpx.get", cloud.get)
    if sync:
        assert client.post("/v1/desktop/capabilities/sync").status_code == 200
    return registry.install_id("skill", PROFILE, key), cloud


def _item(client, kind, iid):
    response = client.get("/v1/desktop/capabilities/installations", params={"kind": kind})
    assert response.status_code == 200
    return next(item for item in response.json()["data"]["items"] if item["install_id"] == iid)


def test_incompatible_skill_is_downloaded_but_not_ready(client, monkeypatch):
    iid, _ = _cloud_skill(client, monkeypatch, "platforms: [not-a-real-platform]")
    assert registry.get(iid).ready
    item = _item(client, "skill", iid)
    assert item["state"] == "ready" and item["files_ready"] is True
    assert item["usable"] is False and item["readiness"]["ready"] is False
    assert item["readiness"]["errors"][0]["reason"] == "platform_incompatible"
    assert item["readiness"]["missing_required"]


def test_missing_runtime_recovers_without_redownload(client, monkeypatch):
    from core.capabilities import dependency

    iid, cloud = _cloud_skill(
        client,
        monkeypatch,
        "dependencies:\n  - kind: pip\n    id: codex-test-package-never-installed-1943",
    )
    assert not _item(client, "skill", iid)["usable"]
    revision = registry.get(iid).resolved_revision
    original = dependency.importlib.metadata.version
    monkeypatch.setattr(
        dependency.importlib.metadata,
        "version",
        lambda name: "1.0" if name == "codex-test-package-never-installed-1943" else original(name),
    )

    def no_network(*args, **kwargs):
        raise AssertionError("rechecking retained files must not download")

    monkeypatch.setattr("httpx.get", no_network)
    recovered = _item(client, "skill", iid)
    assert recovered["usable"] and recovered["readiness"]["ready"]
    assert registry.get(iid).resolved_revision == revision


def test_optional_missing_runtime_is_a_visible_warning(client, monkeypatch):
    iid, _ = _cloud_skill(
        client,
        monkeypatch,
        "dependencies:\n  - kind: pip\n    id: codex-test-package-never-installed-1943\n    required: false",
    )
    item = _item(client, "skill", iid)
    assert item["usable"] and item["readiness"]["ready"]
    assert item["readiness"]["warnings"][0]["reason"] == "runtime_dependency_missing"


def _entity(kind, key, definition):
    files = {"agent.json" if kind == "agent" else "plugin.json": json.dumps(definition)}
    if kind == "agent":
        files["instructions.md"] = "Synthetic agent"
    digest = entity_content_hash(files)
    inst = registry.upsert(
        profile_id=PROFILE,
        ref=cloud_ref("https://cloud.example", kind, key, scope="shared"),
        content_hash=digest,
        source="cloud",
    )
    store.write_from_files(kind, PROFILE, key, digest[:12], files)
    registry.set_state(inst.install_id, "ready", resolved_revision=digest[:12])
    return inst.install_id


def test_agent_list_reports_incompatible_platform(client, monkeypatch):
    from core.services import user_agent_service

    monkeypatch.setattr(
        user_agent_service,
        "UserAgentService",
        lambda db: SimpleNamespace(repo=SimpleNamespace(list_for_user=lambda uid: [])),
    )
    iid = _entity(
        "agent",
        "platform-agent",
        {
            "agent_id": "platform-agent",
            "name": "Platform agent",
            "platforms": ["not-a-real-platform"],
        },
    )
    item = _item(client, "agent", iid)
    assert item["files_ready"] and not item["usable"]
    assert item["readiness"]["errors"][0]["reason"] == "platform_incompatible"


def test_plugin_readiness_follows_its_components(client, monkeypatch):
    """插件的就绪由组件推导：同步把组件技能准备好了才算就绪，组件文件没了就立刻不就绪。"""
    sid, _ = _cloud_skill(client, monkeypatch, "")
    plugin = _entity(
        "plugin",
        "ordered-plugin",
        {"slug": "ordered-plugin", "components": {"skills": ["requires-runtime"]}},
    )
    registry.set_components(plugin, {sid: True})
    assert _item(client, "plugin", plugin)["usable"]

    store.remove_key("skill", PROFILE, "requires-runtime")
    registry.set_state(sid, "pending", resolved_revision=None)
    after = _item(client, "plugin", plugin)
    assert not after["usable"] and not after["readiness"]["ready"]


def test_local_mcp_missing_command_is_not_ready(client, monkeypatch):
    from core.services import mcp_service

    empty = SimpleNamespace(
        get_all_servers=lambda **kw: {}, get_owned_servers=lambda *args, **kw: {}
    )
    monkeypatch.setattr(mcp_service.McpServerConfigService, "get_instance", lambda: empty)
    monkeypatch.setattr(bridge, "get_cached_manifest", lambda: None)
    monkeypatch.setattr(
        bridge,
        "_mcp_json_local_declarations",
        lambda: {
            "missing-cli": {
                "transport": "stdio",
                "command": "codex-test-cli-does-not-exist-1943",
                "enabled": True,
            }
        },
    )
    item = _item(client, "mcp", "mcp:local-json:missing-cli")
    assert not item["usable"] and not item["readiness"]["ready"]
    assert item["readiness"]["errors"][0]["reason"] == "runtime_command_missing"


def test_plugin_component_reports_its_runtime_blocker(client, monkeypatch):
    sid, _ = _cloud_skill(client, monkeypatch, "platforms: [not-a-real-platform]")
    plugin = _entity(
        "plugin",
        "blocked-component",
        {"slug": "blocked-component", "components": {"skills": ["requires-runtime"]}},
    )
    registry.set_components(plugin, {sid: True})
    item = _item(client, "plugin", plugin)
    assert not item["usable"] and not item["readiness"]["ready"]
    assert item["readiness"]["components"][0]["ready"] is False
    assert registry.get(sid).ready
