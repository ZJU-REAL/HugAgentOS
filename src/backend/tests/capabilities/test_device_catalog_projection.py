"""能力中心在本机后端读到的，必须就是同步写入、也真正生效的那份登记表。

回归的是「登录后技能 / 插件 / 智能体 / 连接器完全不一样」：读取切到本机之后，
本机的清单接口仍在读本机业务库，而云端同步只写登记表，两边根本不是一份数据。
"""

from __future__ import annotations

import base64
import json

import pytest
from core.capabilities import device_catalog, registry
from core.capabilities.paths import KIND_AGENT, KIND_PLUGIN, KIND_SKILL
from core.capabilities.ref import cloud_ref, profile_id
from core.services import desktop_cloud_bridge as bridge

_CLOUD = "https://cloud.example"


def _state(uid: str = "u1") -> dict:
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"u": uid, "c": "center-" + uid, "a": 1, "h": "session-" + uid, "d": "device"}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return {"cloud_base": _CLOUD, "token": f"dcap2.{body}.sig"}


@pytest.fixture
def bridged(index_db, caps_root, monkeypatch):
    """桥已激活、身份已认出的本机后端。"""
    st = _state()
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: True)
    return st, profile_id(_CLOUD, "u1")


def _install(profile: str, kind: str, key: str, **kwargs):
    return registry.upsert(
        profile_id=profile,
        ref=cloud_ref(_CLOUD, kind, key, scope="shared"),
        display_name=kwargs.pop("display_name", key),
        description=kwargs.pop("description", ""),
        **kwargs,
    )


def test_inactive_off_desktop_returns_nothing(index_db, monkeypatch):
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: False)
    assert device_catalog.active() is False
    assert device_catalog.catalog_overlay()["skills"] == []
    assert device_catalog.agent_entries() == []
    assert device_catalog.plugin_entries() == []


def test_disabled_skill_is_still_listed(bridged):
    _st, profile = bridged
    _install(profile, KIND_SKILL, "market-x", display_name="Market X", initial_enabled=False)

    items = device_catalog.catalog_overlay()["skills"]

    assert [i["id"] for i in items] == ["market-x"]
    # 停用的也要出现在清单里，否则开关根本没有地方可点。
    assert items[0]["enabled"] is False
    assert items[0]["name"] == "Market X"
    assert items[0]["kind"] == "tool_bundle"


def test_plugin_projection_carries_its_components(bridged):
    _st, profile = bridged
    _install(
        profile,
        KIND_PLUGIN,
        "feishu",
        display_name="飞书",
        payload={
            "cloud_install_id": "feishu@u1",
            "category": "office",
            "components": {"skills": ["feishu-doc"], "mcp": ["feishu-mcp"]},
        },
    )

    entries = device_catalog.plugin_entries()

    assert len(entries) == 1
    # 插件下方的技能来自登记的组件边——这里空了，界面上插件就是个空壳。
    assert entries[0]["skills"] == ["feishu-doc"]
    assert entries[0]["mcp"] == ["feishu-mcp"]
    assert entries[0]["install_id"] == "feishu@u1"
    assert entries[0]["category"] == "office"


def test_agent_projection_uses_registry_enablement(bridged):
    _st, profile = bridged
    _install(profile, KIND_AGENT, "analyst", display_name="分析师", initial_enabled=False)

    entries = device_catalog.agent_entries()

    assert [e["agent_id"] for e in entries] == ["analyst"]
    assert entries[0]["is_enabled"] is False
    assert entries[0]["name"] == "分析师"


def test_removed_entries_drop_out(bridged):
    _st, profile = bridged
    inst = _install(profile, KIND_SKILL, "gone")
    registry.mark_removed(inst.install_id)

    assert device_catalog.catalog_overlay()["skills"] == []


def test_toggle_writes_the_registry(bridged):
    _st, profile = bridged
    inst = _install(profile, KIND_SKILL, "market-x", initial_enabled=True)

    assert device_catalog.set_enabled(KIND_SKILL, "market-x", False) is True
    assert registry.get(inst.install_id).enabled is False
    assert device_catalog.catalog_overlay()["skills"][0]["enabled"] is False


def test_toggle_reports_miss_for_unknown_item(bridged):
    assert device_catalog.set_enabled(KIND_SKILL, "not-installed", False) is False


def test_plugin_toggle_accepts_cloud_install_id_or_slug(bridged):
    _st, profile = bridged
    inst = _install(profile, KIND_PLUGIN, "feishu", payload={"cloud_install_id": "feishu@u1"})

    assert device_catalog.set_plugin_enabled("feishu@u1", False) is True
    assert registry.get(inst.install_id).enabled is False
    assert device_catalog.set_plugin_enabled("feishu", True) is True
    assert registry.get(inst.install_id).enabled is True


def test_merge_keeps_local_entries_and_lets_projection_win(bridged):
    local = [{"id": "mine", "enabled": True}, {"id": "shared", "enabled": True, "name": "旧"}]
    projected = [{"id": "shared", "enabled": False, "name": "新"}, {"id": "cloud-only"}]

    merged = device_catalog.merge_items(local, projected)

    assert [item["id"] for item in merged] == ["mine", "shared", "cloud-only"]
    # 同 id 以投影为准：它才是运行时真正读的那份。
    assert merged[1]["enabled"] is False
    assert merged[1]["name"] == "新"


def test_plugin_detail_lists_its_component_skills(bridged):
    """插件详情页里的技能来自登记表；查不到就是用户看到的「插件下方什么都没有」。"""
    _st, profile = bridged
    _install(
        profile,
        KIND_PLUGIN,
        "feishu",
        display_name="飞书",
        payload={"cloud_install_id": "feishu@u1", "components": {"skills": ["feishu-doc"]}},
    )
    _install(profile, KIND_SKILL, "feishu-doc", display_name="飞书文档")

    detail = device_catalog.plugin_detail("feishu@u1")

    assert detail is not None
    assert [s["skill_id"] for s in detail["skills"]] == ["feishu-doc"]
    assert detail["skills"][0]["name"] == "飞书文档"


def test_plugin_detail_skips_components_not_installed_here(bridged):
    _st, profile = bridged
    _install(
        profile,
        KIND_PLUGIN,
        "feishu",
        payload={"cloud_install_id": "feishu@u1", "components": {"skills": ["missing"]}},
    )

    assert device_catalog.plugin_detail("feishu@u1")["skills"] == []


def test_plugin_detail_is_none_for_local_plugins(bridged):
    assert device_catalog.plugin_detail("some-local-plugin") is None


def test_projected_agent_accepts_only_enablement(bridged):
    _st, profile = bridged
    inst = _install(profile, KIND_AGENT, "analyst", initial_enabled=True)

    assert device_catalog.toggle_projected_agent("analyst", {"name": "改名"}) is None
    updated = device_catalog.toggle_projected_agent("analyst", {"is_enabled": False})

    assert updated is not None and updated["is_enabled"] is False
    assert registry.get(inst.install_id).enabled is False
    assert device_catalog.toggle_projected_agent("local-agent", {"is_enabled": False}) is None


def test_plugin_components_are_reported_as_hidden(bridged):
    """插件的技能 / 连接器只在插件下露面，不再单独进技能库和连接器库。"""
    _st, profile = bridged
    _install(
        profile,
        KIND_PLUGIN,
        "feishu",
        payload={"components": {"skills": ["feishu-doc"], "mcp": ["feishu-mcp"]}},
    )

    overlay = device_catalog.catalog_overlay()

    assert overlay["hidden_skills"] == {"feishu-doc"}
    assert overlay["hidden_mcp"] == {"feishu-mcp"}


def test_plugin_skill_stays_out_of_the_skill_library(bridged):
    """启用插件不该让它的技能同时冒到技能库里——归属记在技能自己身上。"""
    _st, profile = bridged
    _install(profile, KIND_SKILL, "feishu-doc", source_plugin="feishu")
    _install(profile, KIND_SKILL, "market-x")

    assert [item["id"] for item in device_catalog.catalog_overlay()["skills"]] == ["market-x"]


def test_ownership_applies_before_the_plugin_syncs(bridged):
    """技能先同步、插件还没到的那段窗口里，归属也已经生效。"""
    _st, profile = bridged
    _install(profile, KIND_SKILL, "feishu-doc", source_plugin="feishu")

    assert device_catalog.catalog_overlay()["hidden_skills"] == {"feishu-doc"}


def test_plugin_connector_stays_out_of_the_connector_library(bridged, monkeypatch):
    monkeypatch.setattr(
        device_catalog,
        "_managed_connectors",
        lambda: [
            {"server_id": "feishu-mcp", "display_name": "飞书", "description": "",
             "enabled": True, "source_plugin": "feishu", "tools": []},
            {"server_id": "search", "display_name": "搜索", "description": "",
             "enabled": True, "source_plugin": "", "tools": []},
        ],
    )

    overlay = device_catalog.catalog_overlay()

    assert [item["id"] for item in overlay["mcp"]] == ["search"]
    assert overlay["hidden_mcp"] == {"feishu-mcp"}
