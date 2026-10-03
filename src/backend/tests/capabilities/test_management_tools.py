"""Declared manager plugins execute locally and expose environment-specific contracts."""
import json
from pathlib import Path
import pytest
from core.services.management_contract import tools, localize
from core.llm.mcp_pool import make_client
from core.services.desktop_capability_protocol import canonical_hash


def manifest_client(manager, specs, user="local-u1", remote=False):
    return make_client("test-manager", {"transport": "streamable_http", "url": "http://localhost/inert",
        "schema_source": "cloud_manifest" if remote else "plugin_manifest", "origin": "local_plugin", "manifest_tools": specs,
        "gateway_invoke_url": "https://cloud.example/api/v1/desktop/capability/gateway/test-manager/call" if remote else "",
        "schema_hash": canonical_hash(specs), "gateway_plugin": manager,
        "headers": {"X-Current-User-Id": user}}, is_stateful=False)


@pytest.mark.asyncio
async def test_declared_local_install_updates_page_projection(client, tmp_path, monkeypatch):
    from core.capabilities import local_plugin_runtime, device_catalog
    monkeypatch.setattr(local_plugin_runtime, "authorizes_manager", lambda user, sid, manager: user == "local-u1")
    mcp = manifest_client("skill-manager", tools("skill-manager", "cloud"))
    tool = await mcp.get_tool("install_skill")
    assert "artifact_id" not in json.dumps(tool.input_schema)
    root = tmp_path / "from-tool"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: from-tool\ndescription: Tool installed skill\n---\nBody")
    result = await tool(source={"kind": "local_path", "path": str(root)})
    body = json.loads(result.content[0].text)
    assert body["ok"], body
    assert body["install_id"] in {x["install_id"] for x in device_catalog.catalog_overlay(user_id="local-u1")["skills"]}
    rejected = await tool(source={"kind": "artifact", "artifact_id": "not-local"})
    assert json.loads(rejected.content[0].text)["ok"] is False
    assert "upload_skill_to_cloud" not in [t.name for t in await mcp.list_tools()]


@pytest.mark.asyncio
async def test_offline_plugin_package_supplies_manager_without_remote_discovery(client, monkeypatch):
    monkeypatch.setattr("core.capabilities.skills.account_authorized_for", lambda _: False)
    from core.plugins.local import service as local_plugin_service
    from core.capabilities.local_plugin_runtime import configs
    root = Path(__file__).resolve().parents[2] / "plugin_bundles/marketplace/skill-manager"
    installed = local_plugin_service.install("local-u1", str(root))
    servers = configs("local-u1")
    sid = installed["components"]["mcp"][0]
    assert sid in servers
    mcp = make_client(sid, servers[sid], is_stateful=False)
    names = [t.name for t in await mcp.list_tools()]
    assert "install_skill" in names
    assert "upload_skill_to_cloud" not in names
    assert not configs("other")


def test_unrelated_or_future_contract_never_uses_local_executor():
    plain = {"name": "install_skill", "inputSchema": {"type": "object"}}
    assert localize(plain, "external-plugin", cloud_available=True) is plain
    future = tools("skill-manager", "cloud")[0]
    future["_meta"]["org.hugagent/executor"]["version"] = 99
    with pytest.raises(ValueError, match="upgrade"):
        localize(future, "skill-manager", cloud_available=True)


@pytest.mark.asyncio
async def test_installed_local_manager_exposes_upload_only_to_bound_cloud_user(client, monkeypatch):
    from core.plugins.local import service as local_plugin_service
    from core.capabilities.local_plugin_runtime import configs
    monkeypatch.setattr("core.capabilities.skills.account_authorized_for", lambda uid: uid == "local-u1")
    root = Path(__file__).resolve().parents[2] / "plugin_bundles/marketplace/skill-manager"
    installed = local_plugin_service.install("local-u1", str(root))
    sid = installed["components"]["mcp"][0]
    mcp = make_client(sid, configs("local-u1")[sid], is_stateful=False)
    assert "upload_skill_to_cloud" in [t.name for t in await mcp.list_tools()]


@pytest.mark.asyncio
async def test_cloud_delivered_manager_contract_executes_locally(client, tmp_path, monkeypatch):
    from core.services import desktop_cloud_bridge as bridge
    specs = tools("skill-manager", "cloud")
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "require_current_account", lambda _: None)
    monkeypatch.setattr(bridge, "_bridge_context", lambda: {"servers": [{"server_id": "test-manager", "source_plugin": "skill-manager", "tools": specs}]})
    monkeypatch.setattr("core.capabilities.skills.current_local_user_id", lambda: "local-u1")
    mcp = manifest_client("skill-manager", specs, remote=True)
    tool = await mcp.get_tool("install_skill")
    root = tmp_path / "hybrid-skill"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: hybrid-skill\ndescription: Hybrid\n---\nBody")
    result = json.loads((await tool(source={"kind": "local_path", "path": str(root)})).content[0].text)
    assert result["ok"] and result["source"] == "local"
    assert "upload_skill_to_cloud" in [t.name for t in await mcp.list_tools()]
    monkeypatch.setattr(bridge, "_bridge_context", lambda: {"servers": []})
    revoked = json.loads((await tool(source={"kind": "local_path", "path": str(root)})).content[0].text)
    assert not revoked["ok"]
