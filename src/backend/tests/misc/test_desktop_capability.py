"""桌面双端能力桥：capability token、组件基名、混合能力解析（云端注入 + 本机抑制）。

覆盖三层：
1. token 签发/校验（篡改、过期、畸形输入都拒绝）；
2. 连接器绑定以平台登记的 server_id 为唯一身份；
3. desktop_cloud_bridge 的 enabled_mcp_ids 合并——云端接管的本机同名实现被抑制、
   无工具名称保留名单、桥未激活零行为变化。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from core.db.model_repository import assign_role, create_provider
from core.db.models import AdminMcpServer
from core.services import desktop_capability as cap
from core.services import desktop_capability_configs as configs
from core.services import desktop_capability_credentials as credentials
from core.services import desktop_capability_mcp as mcp_cap
from core.services import desktop_capability_security as security
from core.services import desktop_cloud_bridge as bridge
from core.services.desktop_capability_protocol import build_manifest, canonical_hash
from sqlalchemy.orm import sessionmaker


def _use_test_database(monkeypatch, db_session):
    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(security, "SessionLocal", factory)
    monkeypatch.setattr(mcp_cap, "SessionLocal", factory)
    monkeypatch.setattr(configs, "SessionLocal", factory)
    # A different database: drop the process-level authorization snapshots.
    cap.invalidate_model_gateway_cache()
    with configs._effective_lock:
        configs._effective_cache.clear()


# ── capability token ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    """token 测试只关心 HMAC 逻辑，密钥直接注入进程缓存（绕开 DB get-or-create）。"""
    monkeypatch.setattr(cap, "_secret_cache", "ab" * 32)
    yield


def _issue_test_token(monkeypatch, ttl_s=600):
    from core.auth import session

    monkeypatch.setattr(session, "_MEMORY_SESSIONS", {})
    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    cookie = asyncio.run(
        session.create_session({"user_id": "user-1", "user_center_id": "center-1"})
    )
    digest = session._hash_token(cookie)
    return cap.issue_capability_token(
        "user-1",
        ttl_s=ttl_s,
        device_id="test-device",
        issuer="https://test.example",
        session_hash=digest,
        user_center_id="center-1",
        authorization_epoch=cap.session_authorization_epoch(
            session._MEMORY_SESSIONS[digest]["payload"]
        ),
    )


def _verify_test_token(token):
    return asyncio.run(
        cap.verify_capability_token(token, device_id="test-device", issuer="https://test.example")
    )


def test_token_roundtrip(monkeypatch):
    data = _issue_test_token(monkeypatch)
    assert data["token"].startswith("dcap2.")
    assert _verify_test_token(data["token"]) == "user-1"
    assert data["scope"] == "desktop_runtime"


def test_token_tamper_rejected(monkeypatch):
    token = _issue_test_token(monkeypatch)["token"]
    prefix, body, sig = token.split(".", 2)
    assert _verify_test_token(f"{prefix}.{body}x.{sig}") is None
    assert _verify_test_token(f"{prefix}.{body}.{'0' * len(sig)}") is None
    assert _verify_test_token("") is None
    assert _verify_test_token("garbage") is None


def test_token_expiry(monkeypatch):
    token = _issue_test_token(monkeypatch, ttl_s=61)["token"]
    assert _verify_test_token(token) == "user-1"
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)
    assert _verify_test_token(token) is None


def _activate_bridge(monkeypatch, servers):
    """把桥置为激活态并注入假 manifest（绕开网络与 DB）。"""
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    manifest = build_manifest(servers)
    monkeypatch.setattr(bridge, "get_cached_manifest", lambda: manifest)
    monkeypatch.setattr(
        bridge,
        "get_state",
        lambda: {
            "cloud_base": "https://cloud.example",
            "token": "dcap1.x.y",
            "expires_at": time.time() + 3600,
        },
    )
    monkeypatch.setattr(
        bridge,
        "_local_server_ids",
        lambda: {
            "internet_search",
            "retrieve_dataset_content",
            "batch_runner",
            "sites-site_publish",
            "skill-manager-skill_manager",
        },
    )


def _cloud_server(server_id, source_plugin, tool_name):
    tools = [
        {
            "name": tool_name,
            "description": f"Dynamic schema for {tool_name}",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    return {
        "server_id": server_id,
        "source_plugin": source_plugin,
        "tools": tools,
        "schema_hash": canonical_hash(tools),
    }


_CLOUD_SERVERS = [
    _cloud_server("internet_search", None, "internet_search"),
    {
        "server_id": "industry-knowledge-center-ai_chain_information_mcp",
        "source_plugin": "industry-knowledge-center",
        "tools": [
            {
                "name": "ai_chain_information",
                "description": "Query industry knowledge",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        "schema_hash": canonical_hash(
            [
                {
                    "name": "ai_chain_information",
                    "description": "Query industry knowledge",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        ),
    },
    _cloud_server("skill-manager-skill_manager", "skill-manager", "skill_manager"),
    # 云端站点和批量工具与其它连接器使用相同的来源解析规则
    _cloud_server("sites-site_publish", "sites", "site_publish"),
    _cloud_server("batch_runner", None, "run_batch"),
]


def test_apply_merges_cloud_and_suppresses_local(monkeypatch):
    _activate_bridge(monkeypatch, _CLOUD_SERVERS)
    out = bridge.apply_to_enabled_mcp_ids(
        ["internet_search", "batch_runner", "sites-site_publish", "skill-manager-skill_manager"]
    )
    # 同名连接器默认选中当前账号的云端绑定
    assert "batch_runner" in out
    assert "sites-site_publish" in out
    # 云端 id 注入
    assert "industry-knowledge-center-ai_chain_information_mcp" in out
    # 同一 server_id 的云端与本机候选只留一条绑定，不重复
    assert out.count("internet_search") == 1
    assert out.count("skill-manager-skill_manager") == 1
    assert out.count("sites-site_publish") == 1


def test_apply_noop_when_bridge_inactive(monkeypatch):
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: False)
    ids = ["internet_search", "batch_runner"]
    assert bridge.apply_to_enabled_mcp_ids(list(ids)) == ids
    assert bridge.apply_to_enabled_mcp_ids(None) is None


def test_apply_noop_when_manifest_missing(monkeypatch):
    _activate_bridge(monkeypatch, [])
    monkeypatch.setattr(bridge, "get_cached_manifest", lambda: None)
    ids = ["internet_search"]
    assert bridge.apply_to_enabled_mcp_ids(list(ids)) == ids


def test_apply_is_idempotent(monkeypatch):
    _activate_bridge(monkeypatch, _CLOUD_SERVERS)
    once = bridge.apply_to_enabled_mcp_ids(["internet_search", "batch_runner"])
    twice = bridge.apply_to_enabled_mcp_ids(list(once))
    assert once == twice


def test_cloud_gateway_configs_shape(monkeypatch):
    _activate_bridge(monkeypatch, _CLOUD_SERVERS)
    cfgs = bridge.cloud_gateway_mcp_configs()
    sid = "industry-knowledge-center-ai_chain_information_mcp"
    assert sid in cfgs
    cfg = cfgs[sid]
    assert cfg["transport"] == "streamable_http"
    assert cfg["url"] == f"https://cloud.example/api/v1/desktop/capability/gateway/{sid}/call"
    assert cfg["headers"]["Authorization"].startswith("Bearer ")
    assert cfg["schema_source"] == "cloud_manifest"
    assert cfg["manifest_revision"]
    assert cfg["manifest_tools"][0]["name"] == "ai_chain_information"
    assert cfg["schema_hash"]
    # 同名本机候选不再阻止已选中的云端配置
    assert cfgs["batch_runner"]["schema_source"] == "cloud_manifest"
    # 正式站点只能云端托管，因此必须生成网关配置并标出提供它的插件。
    assert cfgs["sites-site_publish"]["gateway_plugin"] == "sites"


def test_bridge_account_switch_clears_previous_manifest(monkeypatch):
    from core.db import engine

    monkeypatch.setattr(
        bridge,
        "_state",
        {
            "cloud_base": "https://cloud-a.example",
            "token": "token-a",
            "expires_at": time.time() + 3600,
        },
    )
    monkeypatch.setattr(bridge, "_state_loaded", True)
    monkeypatch.setattr(bridge, "_manifest", build_manifest([]))
    monkeypatch.setattr(bridge, "_manifest_ts", 123.0)
    monkeypatch.setattr(bridge, "_manifest_error", "old error")
    refreshed = []
    monkeypatch.setattr(
        bridge,
        "_refresh_manifest_async",
        lambda force=False: refreshed.append(force),
    )
    monkeypatch.setattr(
        engine,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    bridge.set_state("https://cloud-b.example", "token-b", 3600)

    assert bridge._manifest is None
    assert bridge._manifest_ts == 0.0
    assert bridge._manifest_error is None
    assert refreshed == [True]


def test_capability_manifest_contains_current_sanitized_schemas(monkeypatch, db_session):
    _use_test_database(monkeypatch, db_session)
    row = AdminMcpServer(
        server_id="private-search",
        display_name="Private Search",
        description="Search without exposing its credential",
        transport="streamable_http",
        url="https://mcp.example/mcp?api_key=must-not-leak",
        headers={"Authorization": "encrypted-secret"},
        is_stable=False,
        is_enabled=True,
        tools_json=[
            {
                "name": "search",
                "description": "Search documents",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {
                    "readOnlyHint": True,
                    "privateExtension": "must-not-leak",
                },
                "credential": "must-not-leak",
            }
        ],
    )
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(
        mcp_cap,
        "_user_capability_configs",
        lambda _uid, **_kwargs: (
            ["private-search"],
            ["private-search"],
            {
                "private-search": {
                    "transport": "streamable_http",
                    "url": row.url,
                    "headers": {"Authorization": "real-secret"},
                }
            },
        ),
    )

    manifest = cap.build_user_capability_manifest("user-1")

    assert manifest["version"] == 2
    assert len(manifest["revision"]) == 64
    server = manifest["servers"][0]
    assert len(server["schema_hash"]) == 64
    assert server["tools"][0]["inputSchema"]["required"] == ["query"]
    assert server["tools"][0]["annotations"] == {"readOnlyHint": True}
    serialized = __import__("json").dumps(manifest)
    assert "must-not-leak" not in serialized
    assert "real-secret" not in serialized
    assert "mcp.example" not in serialized


def test_resolve_gateway_tool_rejects_a_stale_schema(monkeypatch, db_session):
    from core.services.desktop_capability_protocol import CapabilityManifestStaleError

    _use_test_database(monkeypatch, db_session)
    tools = [
        {
            "name": "allowed_tool",
            "description": "Allowed tool",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    db_session.add(
        AdminMcpServer(
            server_id="allowed-server",
            display_name="Allowed",
            description="",
            transport="streamable_http",
            url="https://mcp.example/mcp",
            is_stable=False,
            is_enabled=True,
            tools_json=tools,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        mcp_cap,
        "resolve_gateway_target",
        lambda uid, sid, **kwargs: (
            {"transport": "streamable_http", "url": "https://mcp.example/mcp"}
            if (uid, sid, kwargs.get("fresh")) == ("user-1", "allowed-server", True)
            else None
        ),
    )

    resolved = cap.resolve_gateway_tool(
        "user-1",
        "allowed-server",
        "allowed_tool",
        schema_hash=canonical_hash(tools),
    )
    assert resolved is not None
    with pytest.raises(CapabilityManifestStaleError):
        cap.resolve_gateway_tool(
            "user-1",
            "allowed-server",
            "allowed_tool",
            schema_hash="0" * 64,
        )


@pytest.mark.parametrize(
    "server_id", ["automation-automation_task", "batch_runner", "generate_chart_tool"]
)
def test_cloud_tool_remains_available_without_local_launcher(monkeypatch, server_id):
    _activate_bridge(monkeypatch, [_cloud_server(server_id, None, "test_tool")])
    monkeypatch.setattr(bridge, "_local_server_ids", lambda: set())
    enabled = bridge.apply_to_enabled_mcp_ids([])
    assert server_id in enabled
    configs = bridge.cloud_gateway_mcp_configs(enabled)
    assert server_id in configs
    assert configs[server_id]["schema_source"] == "cloud_manifest"


@pytest.mark.parametrize("legacy_setting", ["keep", "switch", "both"])
def test_legacy_environment_cannot_hide_authorized_cloud_tools(monkeypatch, legacy_setting):
    servers = [
        _cloud_server(sid, None, "tool_" + str(i))
        for i, sid in enumerate(
            [
                "batch_runner",
                "generate_chart_tool",
                "automation-automation_task",
                "sites-site_publish",
                "custom_connector",
            ]
        )
    ]
    _activate_bridge(monkeypatch, servers)
    if legacy_setting in ("keep", "both"):
        monkeypatch.setenv("DESKTOP_LOCAL_MCP_KEEP", ",".join(s["server_id"] for s in servers))
    if legacy_setting in ("switch", "both"):
        monkeypatch.setenv("DESKTOP_CLOUD_MCP_BRIDGE_ENABLED", "0")
    monkeypatch.setattr(bridge, "_local_server_ids", lambda: {s["server_id"] for s in servers})
    enabled = bridge.apply_to_enabled_mcp_ids([s["server_id"] for s in servers])
    configs = bridge.cloud_gateway_mcp_configs(enabled)
    assert set(configs) == {s["server_id"] for s in servers}
    assert all(c["schema_source"] == "cloud_manifest" for c in configs.values())
    assert bridge.bridge_active()


def test_fresh_manifests_bypass_worker_mcp_caches(monkeypatch, db_session):
    from core.services import mcp_service

    _use_test_database(monkeypatch, db_session)
    monkeypatch.setattr(mcp_service, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    workers = [mcp_service.McpServerConfigService(), mcp_service.McpServerConfigService()]
    row = AdminMcpServer(
        server_id="shared-new",
        display_name="Shared",
        transport="streamable_http",
        url="https://mcp.example/mcp",
        is_enabled=True,
        tools_json=[],
    )
    db_session.add(row)
    db_session.commit()
    for worker in workers:
        assert "shared-new" in worker.get_all_servers()
    db_session.delete(row)
    db_session.commit()
    for worker in workers:
        monkeypatch.setattr(mcp_service.McpServerConfigService, "_instance", worker)
        assert cap.build_user_capability_manifest("user-1", use_cache=False)["servers"] == []


def test_other_users_private_connector_does_not_change_manifest(monkeypatch, db_session):
    from core.services import mcp_service

    _use_test_database(monkeypatch, db_session)
    monkeypatch.setattr(mcp_service, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(
        mcp_service.McpServerConfigService, "_instance", mcp_service.McpServerConfigService()
    )
    before = cap.build_user_capability_manifest("user-1", use_cache=False)
    db_session.add(
        AdminMcpServer(
            server_id="other-private",
            owner_user_id="user-2",
            display_name="Other",
            transport="streamable_http",
            url="https://mcp.example/mcp",
            is_enabled=True,
            tools_json=[],
        )
    )
    db_session.commit()
    assert cap.build_user_capability_manifest("user-1", use_cache=False) == before
    assert len(cap.build_user_capability_manifest("user-2", use_cache=False)["servers"]) == 1
