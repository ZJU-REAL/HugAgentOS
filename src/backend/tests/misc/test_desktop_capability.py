"""桌面双端能力桥：capability token、组件基名、混合能力解析（云端注入 + 本机抑制）。

覆盖三层：
1. token 签发/校验（篡改、过期、畸形输入都拒绝）；
2. component_base_name 的 logical 去重键规则；
3. desktop_cloud_bridge 的 enabled_mcp_ids 合并——云端接管的本机同名实现被抑制、
   KEEP 基名保留本机、桥未激活零行为变化。
"""

from __future__ import annotations

import time

from core.services import desktop_capability as cap
from core.services import desktop_cloud_bridge as bridge
from core.db.model_repository import assign_role, create_provider
from sqlalchemy.orm import sessionmaker


# ── capability token ────────────────────────────────────────────────────


import pytest


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    """token 测试只关心 HMAC 逻辑，密钥直接注入进程缓存（绕开 DB get-or-create）。"""
    monkeypatch.setattr(cap, "_secret_cache", "ab" * 32)
    yield


def test_token_roundtrip():
    data = cap.issue_capability_token("user-1", ttl_s=3600)
    assert data["token"].startswith("dcap1.")
    assert cap.verify_capability_token(data["token"]) == "user-1"
    assert data["scope"] == "desktop_runtime"


def test_token_tamper_rejected():
    token = cap.issue_capability_token("user-1")["token"]
    prefix, body, sig = token.split(".", 2)
    assert cap.verify_capability_token(f"{prefix}.{body}x.{sig}") is None
    assert cap.verify_capability_token(f"{prefix}.{body}.{'0' * len(sig)}") is None
    assert cap.verify_capability_token("") is None
    assert cap.verify_capability_token("garbage") is None


def test_token_expiry(monkeypatch):
    token = cap.issue_capability_token("user-1", ttl_s=61)["token"]
    assert cap.verify_capability_token(token) == "user-1"
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)
    assert cap.verify_capability_token(token) is None


# ── 模型能力清单 / 网关授权 ───────────────────────────────────────


def _use_test_database(monkeypatch, db_session):
    monkeypatch.setattr(cap, "SessionLocal", sessionmaker(bind=db_session.get_bind()))


def test_model_manifest_contains_no_upstream_credentials(monkeypatch, db_session):
    _use_test_database(monkeypatch, db_session)
    from core.services import user_model_selection

    monkeypatch.setattr(user_model_selection, "user_can_switch_model", lambda _db, _uid: False)
    assigned = create_provider(
        db_session,
        display_name="Private DeepSeek",
        provider_type="chat",
        provider="openai_compatible",
        base_url="http://192.0.2.10:1029/v1",
        api_key="never-send-this-key",
        model_name="deepseek-private",
        extra_config={"context_length": 131072, "custom_secret": "also-private"},
    )
    unassigned = create_provider(
        db_session,
        display_name="Unassigned",
        provider_type="chat",
        provider="openai",
        base_url="https://model.example/v1",
        api_key="another-secret",
        model_name="unassigned-model",
    )
    assert assign_role(db_session, "main_agent", assigned.provider_id)

    manifest = cap.build_user_model_manifest("user-1")

    assert manifest["version"] == 1
    assert {p["provider_id"] for p in manifest["providers"]} == {
        assigned.provider_id,
        unassigned.provider_id,
    }
    provider = next(p for p in manifest["providers"] if p["provider_id"] == assigned.provider_id)
    assert "base_url" not in provider
    assert "api_key" not in provider
    assert "custom_secret" not in provider["extra_config"]
    assert provider["extra_config"]["context_length"] == 131072
    assert manifest["role_assignments"] == [
        {"role_key": "main_agent", "provider_id": assigned.provider_id}
    ]
    for item in manifest["providers"]:
        assert "base_url" not in item
        assert "api_key" not in item


def test_model_gateway_target_is_role_or_user_switch_allowlisted(monkeypatch, db_session):
    _use_test_database(monkeypatch, db_session)
    from core.services import user_model_selection

    assigned = create_provider(
        db_session,
        display_name="Assigned chat",
        provider_type="chat",
        base_url="http://192.0.2.10:1029/v1/",
        api_key="cloud-only-key",
        model_name="assigned-model",
    )
    selectable = create_provider(
        db_session,
        display_name="Selectable chat",
        provider_type="chat",
        base_url="https://models.example/v1",
        api_key="selectable-key",
        model_name="selectable-model",
    )
    embedding = create_provider(
        db_session,
        display_name="Unassigned embedding",
        provider_type="embedding",
        base_url="https://models.example/v1",
        api_key="embedding-key",
        model_name="embed-model",
    )
    assert assign_role(db_session, "main_agent", assigned.provider_id)

    monkeypatch.setattr(user_model_selection, "user_can_switch_model", lambda _db, _uid: False)
    target = cap.resolve_model_gateway_target("user-1", assigned.provider_id)
    assert target == {
        "url": "http://192.0.2.10:1029/v1/chat/completions",
        "api_key": "cloud-only-key",
        "model_name": "assigned-model",
        "provider_type": "chat",
        "path": "chat/completions",
    }
    assert cap.resolve_model_gateway_target("user-1", selectable.provider_id) is None
    assert cap.resolve_model_gateway_target("user-1", embedding.provider_id) is None

    monkeypatch.setattr(user_model_selection, "user_can_switch_model", lambda _db, _uid: True)
    assert cap.resolve_model_gateway_target("user-1", selectable.provider_id)["path"] == "chat/completions"


# ── 组件基名（logical 去重键） ──────────────────────────────────────────


def test_component_base_name():
    assert cap.component_base_name("internet_search", None) == "internet_search"
    assert cap.component_base_name("sites-site_publish", "sites") == "site_publish"
    assert (
        cap.component_base_name(
            "industry-knowledge-center-ai_chain_information_mcp", "industry-knowledge-center"
        )
        == "ai_chain_information_mcp"
    )
    # slug 不匹配前缀时保底返回原 id
    assert cap.component_base_name("sites-site_publish", "other") == "sites-site_publish"


# ── 混合能力解析 ────────────────────────────────────────────────────────


def _activate_bridge(monkeypatch, servers):
    """把桥置为激活态并注入假 manifest（绕开网络与 DB）。"""
    monkeypatch.delenv("DESKTOP_CLOUD_MCP_BRIDGE_ENABLED", raising=False)
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_cached_manifest", lambda: {"servers": servers})
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
        "_local_server_base_map",
        lambda: {
            "internet_search": "internet_search",
            "retrieve_dataset_content": "retrieve_dataset_content",
            "batch_runner": "batch_runner",
            "sites-site_publish": "site_publish",
            "skill-manager-skill_manager": "skill_manager",
        },
    )


_CLOUD_SERVERS = [
    {"server_id": "internet_search", "component": "internet_search"},
    {
        "server_id": "industry-knowledge-center-ai_chain_information_mcp",
        "component": "ai_chain_information_mcp",
    },
    {"server_id": "skill-manager-skill_manager", "component": "skill_manager"},
    # KEEP 基名：云端也有 site_publish / batch_runner，但本机保留，不得合并
    {"server_id": "sites-site_publish", "component": "site_publish"},
    {"server_id": "batch_runner", "component": "batch_runner"},
]


def test_apply_merges_cloud_and_suppresses_local(monkeypatch):
    _activate_bridge(monkeypatch, _CLOUD_SERVERS)
    out = bridge.apply_to_enabled_mcp_ids(
        ["internet_search", "batch_runner", "sites-site_publish", "skill-manager-skill_manager"]
    )
    # 本机 internet_search / skill_manager 被云端接管；KEEP 项保留本机
    assert "batch_runner" in out
    assert "sites-site_publish" in out
    # 云端 id 注入
    assert "industry-knowledge-center-ai_chain_information_mcp" in out
    assert out.count("internet_search") == 1  # 云端裸 id 顶替本机裸 id，不重复
    assert out.count("skill-manager-skill_manager") == 1
    # KEEP 的云端副本没有被合并进来（site_publish 只有本机一份）
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
    assert cfg["url"] == f"https://cloud.example/api/v1/desktop/capability/gateway/{sid}/mcp"
    assert cfg["headers"]["Authorization"].startswith("Bearer ")
    # KEEP 基名不生成云端配置
    assert "sites-site_publish" not in cfgs
    assert "batch_runner" not in cfgs


def test_keep_local_bases_env_override(monkeypatch):
    monkeypatch.setenv("DESKTOP_LOCAL_MCP_KEEP", "batch_runner, foo_bar")
    assert bridge.keep_local_bases() == {"batch_runner", "foo_bar"}
    monkeypatch.delenv("DESKTOP_LOCAL_MCP_KEEP")
    assert "site_publish" in bridge.keep_local_bases()
