"""插件的技能只在插件下露面，不跟着冒到技能库里。

从 `/v1/catalog` 这一层验证：能力中心的技能列表就是这个接口返回的 ``skills``，
在这里剔干净，界面上才不会出现「启用插件后技能库里多出一条」。
"""

from __future__ import annotations

from core.capabilities import registry
from core.capabilities.paths import KIND_PLUGIN, KIND_SKILL
from core.capabilities.ref import cloud_ref, profile_id
from tests.capabilities._cloud_identity import CLOUD_BASE, CLOUD_USER


def _install(kind: str, key: str, **kwargs):
    return registry.upsert(
        profile_id=profile_id(CLOUD_BASE, CLOUD_USER),
        ref=cloud_ref(CLOUD_BASE, kind, key, scope="shared"),
        display_name=kwargs.pop("display_name", key),
        **kwargs,
    )


def _skill_ids(client) -> list:
    response = client.get("/v1/catalog")
    assert response.status_code == 200
    return [item["id"] for item in response.json()["data"]["skills"]]


def test_plugin_skill_is_not_listed_in_the_skill_library(hybrid_catalog_client):
    _install(KIND_PLUGIN, "feishu", payload={"components": {"skills": ["feishu-doc"]}})
    _install(KIND_SKILL, "feishu-doc", source_plugin="feishu")
    _install(KIND_SKILL, "market-x")

    ids = _skill_ids(hybrid_catalog_client)

    assert "market-x" in ids
    assert "feishu-doc" not in ids


def test_plugin_skill_stays_hidden_before_its_plugin_syncs(hybrid_catalog_client):
    """技能清单和插件清单是两次同步，中间这段窗口也不该漏。"""
    _install(KIND_SKILL, "feishu-doc", source_plugin="feishu")

    assert "feishu-doc" not in _skill_ids(hybrid_catalog_client)


def test_plugin_skill_stays_hidden_without_an_ownership_tag(hybrid_catalog_client):
    """历史条目没有归属标记时，插件登记的组件边仍然兜得住。"""
    _install(KIND_PLUGIN, "feishu", payload={"components": {"skills": ["feishu-doc"]}})
    _install(KIND_SKILL, "feishu-doc")

    assert "feishu-doc" not in _skill_ids(hybrid_catalog_client)
