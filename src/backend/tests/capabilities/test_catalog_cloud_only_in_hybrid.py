"""混合模式下能力中心只列云端账号同步下来的那份，本机自带的一概不留。

用户在桌面端看到过云端账号里根本没有的连接器（数据可视化、批量执行）——那是本机
``catalog.json`` 的内置条目漏了出来，点了还没反应，因为混合模式下本机不运行内置 MCP。
内置技能同理：即便本机跑得起来，也不该出现在一台"能力以云端为准"的机器上。
"""

from __future__ import annotations

from core.capabilities import registry, skills
from core.capabilities.paths import KIND_SKILL
from core.capabilities.ref import cloud_ref, profile_id
from tests.capabilities._cloud_identity import CLOUD_BASE, CLOUD_USER, cloud_token


def _catalog(client, kind: str) -> list:
    response = client.get("/v1/catalog")
    assert response.status_code == 200
    return [item["id"] for item in response.json()["data"][kind]]


def test_builtin_skills_and_connectors_are_absent_in_hybrid(hybrid_catalog_client):
    assert _catalog(hybrid_catalog_client, "mcp") == []
    assert _catalog(hybrid_catalog_client, "skills") == []


def test_cloud_synced_capabilities_are_what_shows_up(hybrid_catalog_client):
    """砍掉的只是本机自带那份，云端同步下来的照常列出。"""
    registry.upsert(
        profile_id=profile_id(CLOUD_BASE, CLOUD_USER),
        ref=cloud_ref(CLOUD_BASE, KIND_SKILL, "officecli-docx", scope="shared"),
        display_name="OfficeCLI Word",
    )

    assert _catalog(hybrid_catalog_client, "skills") == ["officecli-docx"]


def test_builtin_skills_stay_resolvable_as_dependencies(index_db, caps_root, monkeypatch):
    """目录层清空，解析层保留：云端技能对自带技能的依赖、以及跑不了时的兜底都要还在。

    这是本次收敛的边界——「不出现在能力中心、不会被启用」由目录层负责；把自带技能从
    候选里也删掉，会把依赖图和平台兜底一起打断。
    """
    from core.capabilities import skills
    from core.services import desktop_cloud_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "get_state",
        lambda: {"cloud_base": CLOUD_BASE, "token": cloud_token(CLOUD_USER)},
    )
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: True)

    assert [c.runtime_name for c in skills.builtin_candidates()]



def test_private_capabilities_of_this_user_survive(hybrid_catalog_client):
    """用户自建的私有技能只有这台机器上有，不能被当成"本机自带"一起砍掉。"""
    from core.db.engine import get_db
    from core.db.models import AdminSkill

    session = hybrid_catalog_client.app.dependency_overrides[get_db]()
    session.add(
        AdminSkill(
            skill_id="my-own",
            display_name="我自己写的",
            description="private",
            skill_content="# mine",
            owner_user_id=CLOUD_USER,
        )
    )
    session.commit()

    assert "my-own" in _catalog(hybrid_catalog_client, "skills")
