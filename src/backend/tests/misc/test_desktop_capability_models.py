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
from core.services import desktop_capability_models as models
from core.services import desktop_capability_security as security
from core.services import desktop_cloud_bridge as bridge
from core.services import model_config
from core.services.desktop_capability_protocol import build_manifest, canonical_hash
from sqlalchemy.orm import sessionmaker

# ── 模型能力清单 / 网关授权 ───────────────────────────────────────


def _use_test_database(monkeypatch, db_session):
    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(security, "SessionLocal", factory)
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(model_config, "SessionLocal", factory)
    monkeypatch.setattr(security, "_user_capability_configs", lambda *a, **k: ([], [], {}))
    monkeypatch.setattr(configs, "SessionLocal", factory)
    # A different database: drop the process-level authorization snapshots.
    cap.invalidate_model_gateway_cache()
    with configs._effective_lock:
        configs._effective_cache.clear()


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


def test_credential_equal_to_a_public_model_identifier_is_not_a_secret(monkeypatch, db_session):
    """api_key 与 model_name 字面相同：模型名对所有登录用户可见，不是机密。
    这样的模型照常下发（生产上 main_agent 就绑在这种配置上），清单仍通过出口守卫。"""
    _use_test_database(monkeypatch, db_session)
    from core.services import user_model_selection

    monkeypatch.setattr(user_model_selection, "user_can_switch_model", lambda _db, _uid: False)
    keyless = create_provider(
        db_session,
        display_name="Keyless Vision",
        provider_type="chat",
        provider="openai_compatible",
        base_url="http://192.0.2.10:1029/v1",
        api_key="deepseekv4-flash",
        model_name="deepseekv4-flash",
    )
    assert assign_role(db_session, "main_agent", keyless.provider_id)

    manifest = cap.build_user_model_manifest("user-1")

    assert [p["provider_id"] for p in manifest["providers"]] == [keyless.provider_id]
    assert "withheld" not in manifest
    assert manifest["role_assignments"] == [
        {"role_key": "main_agent", "provider_id": keyless.provider_id}
    ]
    assert cap.guard_capability_content("user-1", manifest) is manifest
    assert "deepseekv4-flash" not in security._known_cloud_secrets("user-1")


def test_gateway_stream_secrets_exclude_the_target_model_name_but_keep_real_keys(
    monkeypatch, db_session
):
    """网关转发这条模型的输出时，每个流式分片都带 model 字段；模型名不是机密，
    不能因此把整段回复拦成 upstream content blocked。真实密钥仍被屏蔽。"""
    _use_test_database(monkeypatch, db_session)
    keyless = create_provider(
        db_session,
        display_name="Keyless Vision",
        provider_type="chat",
        provider="openai_compatible",
        base_url="http://192.0.2.10:1029/v1",
        api_key="deepseekv4-flash",
        model_name="deepseekv4-flash",
    )
    create_provider(
        db_session,
        display_name="Other",
        provider_type="chat",
        provider="openai",
        base_url="https://model.example/v1",
        api_key="sk-other-real-key-7d2c",
        model_name="other-model",
    )
    target = {
        "url": "http://192.0.2.10:1029/v1/chat/completions",
        "api_key": keyless.api_key,
        "model_name": keyless.model_name,
    }

    secrets = cap.gateway_stream_secrets("user-1", target)

    assert "deepseekv4-flash" not in secrets
    assert "sk-other-real-key-7d2c" in secrets
    chunks = [b'data: {"model":"deepseekv4-flash","choices":[{"delta":{"content":"hi"}}]}\n\n']

    async def upstream():
        for chunk in chunks:
            yield chunk

    import asyncio

    async def collect():
        return [c async for c in cap.guard_capability_stream(upstream(), secrets)]

    assert b"".join(asyncio.run(collect())) == b"".join(chunks)


def test_model_manifest_withholds_only_the_provider_that_would_leak_a_real_credential(
    monkeypatch, db_session
):
    """另一条模型的真实密钥出现在某模型的展示名里：只扣留这一条并点名字段，其余照常下发。"""
    _use_test_database(monkeypatch, db_session)
    from core.services import user_model_selection

    monkeypatch.setattr(user_model_selection, "user_can_switch_model", lambda _db, _uid: False)
    healthy = create_provider(
        db_session,
        display_name="Healthy",
        provider_type="chat",
        provider="openai",
        base_url="https://model.example/v1",
        api_key="sk-real-secret-value-9f3a",
        model_name="healthy-model",
    )
    leaking = create_provider(
        db_session,
        display_name="Notes sk-real-secret-value-9f3a",
        provider_type="chat",
        provider="openai",
        base_url="https://other.example/v1",
        api_key="sk-other-secret",
        model_name="leaky-model",
    )
    assert assign_role(db_session, "main_agent", healthy.provider_id)
    assert assign_role(db_session, "vision", leaking.provider_id)

    manifest = cap.build_user_model_manifest("user-1")

    assert [p["provider_id"] for p in manifest["providers"]] == [healthy.provider_id]
    assert manifest["withheld"] == [
        {"provider_id": leaking.provider_id, "fields": ["display_name"]}
    ]
    assert manifest["role_assignments"] == [
        {"role_key": "main_agent", "provider_id": healthy.provider_id}
    ]
    assert cap.guard_capability_content("user-1", manifest) is manifest


def test_model_gateway_target_is_role_or_user_switch_allowlisted(monkeypatch, db_session):
    _use_test_database(monkeypatch, db_session)
    from core.services import user_model_selection

    assigned = create_provider(
        db_session,
        display_name="Assigned chat",
        provider="openai_compatible",
        provider_type="chat",
        base_url="http://192.0.2.10:1029/v1/",
        api_key="cloud-only-key",
        model_name="assigned-model",
        extra_config={"api_protocol": "chat_completions"},
    )
    selectable = create_provider(
        db_session,
        display_name="Selectable chat",
        provider_type="chat",
        base_url="https://models.example/v1",
        api_key="selectable-key",
        model_name="selectable-model",
        extra_config={"api_protocol": "chat_completions"},
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
    assert (
        cap.resolve_model_gateway_target("user-1", selectable.provider_id)["path"]
        == "chat/completions"
    )


# ── 混合能力解析 ────────────────────────────────────────────────────────
