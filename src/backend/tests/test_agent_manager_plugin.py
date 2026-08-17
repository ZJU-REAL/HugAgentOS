"""agent-manager plugin self-contained tests: plugin persistence + all 8 MCP verbs end to end.

Coverage targets (matching the goal "create, manage, delete, and submit sub-agents for listing"):
- Installing the agent-manager plugin → AdminSkill(agent-designer) + AdminMcpServer(agent_manager)
  + InstalledPlugin persisted, and merged into that user's available set via
  resolve_all_runtime_enabled (= the agent can really get them).
- create_agent / list_my_agents / edit_agent / delete_agent / list_bindable_capabilities
  / search_agent_market / install_market_agent / submit_agent_to_market.
- Guards that actually matter: capability flag gating, cross-user isolation, ambiguous
  *_ref returning candidates instead of guessing, and bindings being replace-not-append.

No dependency on a running sandbox or mcp container: the impl layer talks to the DB directly /
reuses backend services.
"""

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.db.engine as dbe
from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin, UserAgent

OWNER = "am_test_user"
OTHER = "am_other_user"
BUNDLE_DIR = Path(__file__).resolve().parents[1] / "plugin_bundles" / "marketplace" / "agent-manager"


@pytest.fixture()
def am_env(tmp_path, monkeypatch):
    """Bind SessionLocal to an isolated sqlite file DB; allow capabilities."""
    url = f"sqlite:///{tmp_path}/am.db"
    engine = create_engine(url)
    dbe.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    # impl lazily reads the attribute via `from core.db.engine import SessionLocal` → patching here suffices
    monkeypatch.setattr(dbe, "SessionLocal", TestSession)

    import core.auth.capabilities as caps

    monkeypatch.setattr(
        caps,
        "resolve_user_capabilities",
        lambda db, uid: {"can_add_agent": True, "can_import_plugin": True, "can_add_skill": True},
    )

    return SimpleNamespace(engine=engine, Session=TestSession)


def _create(**kw):
    from mcp_servers.agent_manager_mcp import impl

    payload = {
        "user_id": OWNER,
        "name": "周报助手",
        "description": "把一周的工作记录整理成周报初稿。",
        "system_prompt": "角色：你是周报助手。不做什么：不替用户拍板。",
    }
    payload.update(kw)
    return impl.create_agent(**payload)


# ── Bundle shape: the manifest the marketplace actually reads ────────────────
def test_bundle_manifest_is_wellformed():
    import json

    manifest = json.loads((BUNDLE_DIR / "plugin.json").read_text(encoding="utf-8"))
    mcp_json = json.loads((BUNDLE_DIR / "mcp.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "agent-manager"
    ext_mcp = manifest["extensions"]["org.hugagent"]["mcp"]
    assert set(ext_mcp) == set(mcp_json["mcpServers"]), "扩展段的服务名必须与 mcp.json 一一对应"
    assert mcp_json["mcpServers"]["agent_manager"]["url"] == "http://mcp:9115/mcp/"

    # The declared tool list must match what the server actually exposes — a manifest that
    # advertises a tool the server doesn't have is a silently broken promise to the model.
    from mcp_servers.agent_manager_mcp import server

    declared = {t["name"] for t in ext_mcp["agent_manager"]["tools"]}
    actual = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert declared == actual
    assert len(actual) == 8

    assert (BUNDLE_DIR / "skills" / "agent-designer" / "SKILL.md").is_file()


def test_routing_description_is_short_and_disambiguating():
    """plugin.json 顶层 description 是**路由信号**，不是市场文案。

    core/llm/plugin_loader.build_plugin_directory_section 把它整段（不截断）拼进插件目录，
    而目录进的是稳定提示词前缀——每个用户、每个会话、每一轮都带着。所以：
      - 必须短（长度是永久成本）；
      - 必须含用户会说的话（路由靠语义匹配用户原话）；
      - 必须点明管的是哪类对象（skill/agent/plugin 三个管理插件动词高度雷同，极易串台）；
      - 不该有"装上后就能……"这种自指废话（目录里每一条本来就是可装的插件）。
    """
    import json

    for bundle, obj_word, siblings in (
        (BUNDLE_DIR, "智能体", ("skill-manager", "plugin-manager")),
        (BUNDLE_DIR.parent / "plugin-manager", "插件", ("skill-manager", "agent-manager")),
    ):
        desc = json.loads((bundle / "plugin.json").read_text(encoding="utf-8"))["description"]
        name = bundle.name

        assert len(desc) <= 160, f"{name} 路由描述 {len(desc)} 字，太长（它每轮都在提示词里）"
        assert obj_word in desc, f"{name} 没点明管的是「{obj_word}」"
        assert "用户说" in desc, f"{name} 没写用户会怎么说——路由就是靠匹配用户原话"
        for sib in siblings:
            assert sib in desc, f"{name} 没跟 {sib} 做消歧，三个管理插件会串台"
        assert "装上后" not in desc, f"{name} 残留「装上后……」自指废话"
        assert "在对话里用自然语言" not in desc, f"{name} 残留套话开头，占字数不提供路由信息"


def test_port_is_registered():
    from mcp_servers._ports import PORTS, package_name

    assert PORTS["agent_manager"] == 9115
    assert package_name("agent_manager") == "agent_manager_mcp"


# ── Plugin persistence + merging into the user's available set ──────────────
def test_install_plugin_creates_rows_and_is_agent_visible(am_env):
    from core.services import plugin_service as ps

    with am_env.Session() as db:
        res = ps.install_plugin(db, "agent-manager", owner_user_id=OWNER, created_by=OWNER)
        assert res.get("install_id")

    with am_env.Session() as db:
        skills = db.query(AdminSkill).filter(AdminSkill.source_plugin == "agent-manager").all()
        mcps = db.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == "agent-manager").all()
        plugin = db.query(InstalledPlugin).filter(InstalledPlugin.slug == "agent-manager").first()

        assert len(skills) == 1 and skills[0].owner_user_id == OWNER
        assert len(mcps) == 1
        mcp = mcps[0]
        assert mcp.owner_user_id == OWNER
        assert mcp.transport == "streamable_http"
        assert mcp.url == "http://mcp:9115/mcp/"
        assert mcp.is_enabled is True  # http MCP is not needs_runtime → enabled upon install
        assert len(mcp.tools_json) == 8
        assert plugin is not None

        from core.config.catalog_resolver import (
            invalidate_capability_cache,
            resolve_all_runtime_enabled,
        )

        invalidate_capability_cache()
        enabled_skills, _agents, enabled_mcps = resolve_all_runtime_enabled(db, OWNER)
        assert skills[0].skill_id in (enabled_skills or [])
        assert mcp.server_id in (enabled_mcps or [])


# ── Create → list → edit → delete ───────────────────────────────────────────
def test_create_list_edit_delete_loop(am_env):
    from mcp_servers.agent_manager_mcp import impl

    res = _create()
    assert res["ok"], res
    agent_id = res["agent"]["agent_id"]
    assert agent_id.startswith("ua_")

    listed = impl.list_my_agents(user_id=OWNER)
    assert listed["ok"] and listed["count"] == 1
    assert listed["agents"][0]["agent_id"] == agent_id
    assert listed["agents"][0]["name"] == "周报助手"

    # Partial update: only the named field changes, the rest is preserved.
    edited = impl.edit_agent(
        user_id=OWNER, agent_ref=agent_id, system_prompt="角色：你是周报助手。语气正式。"
    )
    assert edited["ok"], edited
    assert edited["changed"] == ["system_prompt"]
    with am_env.Session() as db:
        row = db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()
        assert row.system_prompt == "角色：你是周报助手。语气正式。"
        assert row.name == "周报助手"  # untouched
        assert row.owner_type == "user" and row.user_id == OWNER  # never admin/global

    # Reference by name also works.
    deleted = impl.delete_agent(user_id=OWNER, agent_ref="周报助手")
    assert deleted["ok"], deleted
    assert impl.list_my_agents(user_id=OWNER)["count"] == 0


def test_create_rejects_missing_core_fields(am_env):
    from mcp_servers.agent_manager_mcp import impl

    assert not impl.create_agent(user_id=OWNER, name="", description="x", system_prompt="y")["ok"]
    assert not impl.create_agent(user_id=OWNER, name="x", description="", system_prompt="y")["ok"]
    assert not impl.create_agent(user_id=OWNER, name="x", description="y", system_prompt="  ")["ok"]
    # Nothing was written by any of the rejected calls.
    assert impl.list_my_agents(user_id=OWNER)["count"] == 0


def test_edit_requires_at_least_one_field(am_env):
    from mcp_servers.agent_manager_mcp import impl

    agent_id = _create()["agent"]["agent_id"]
    res = impl.edit_agent(user_id=OWNER, agent_ref=agent_id)
    assert not res["ok"] and "没有要改的内容" in res["message"]


def test_bindings_are_replaced_not_appended(am_env):
    """The replace-not-append semantics the skill warns about must actually hold."""
    from mcp_servers.agent_manager_mcp import impl

    agent_id = _create(skill_ids=["a", "b"])["agent"]["agent_id"]
    impl.edit_agent(user_id=OWNER, agent_ref=agent_id, skill_ids=["c"])
    with am_env.Session() as db:
        row = db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()
        assert row.skill_ids == ["c"]


def test_binding_ids_are_deduped_and_capped(am_env):
    from mcp_servers.agent_manager_mcp import impl

    res = _create(skill_ids=["a", " a ", "b", "", "b"])
    assert res["ok"]
    with am_env.Session() as db:
        row = db.query(UserAgent).filter(UserAgent.agent_id == res["agent"]["agent_id"]).first()
        assert row.skill_ids == ["a", "b"]

    too_many = impl.create_agent(
        user_id=OWNER,
        name="超绑",
        description="d",
        system_prompt="p",
        skill_ids=[f"s{i}" for i in range(200)],
    )
    assert not too_many["ok"] and "最多绑" in too_many["message"]


# ── Ambiguity: must ask, never guess ────────────────────────────────────────
def test_ambiguous_ref_returns_candidates_instead_of_guessing(am_env):
    from mcp_servers.agent_manager_mcp import impl

    _create(name="报告助手A")
    _create(name="报告助手B")

    for res in (
        impl.delete_agent(user_id=OWNER, agent_ref="报告助手"),
        impl.edit_agent(user_id=OWNER, agent_ref="报告助手", description="改"),
    ):
        assert not res["ok"]
        assert res.get("need_clarification") is True
        assert len(res["candidates"]) == 2

    # Nothing was deleted or modified by the ambiguous calls.
    assert impl.list_my_agents(user_id=OWNER)["count"] == 2


# ── Isolation: only my own agents ───────────────────────────────────────────
def test_cannot_touch_other_users_agents(am_env):
    from mcp_servers.agent_manager_mcp import impl

    mine = _create()["agent"]["agent_id"]

    assert impl.list_my_agents(user_id=OTHER)["count"] == 0
    assert not impl.delete_agent(user_id=OTHER, agent_ref=mine)["ok"]
    assert not impl.edit_agent(user_id=OTHER, agent_ref=mine, description="hijack")["ok"]

    with am_env.Session() as db:
        assert db.query(UserAgent).filter(UserAgent.agent_id == mine).first() is not None


def test_admin_agents_are_not_listed_as_mine(am_env):
    """list_for_user also returns admin-owned agents; the self-serve entry must not claim them."""
    from core.services.user_agent_service import UserAgentService
    from mcp_servers.agent_manager_mcp import impl

    with am_env.Session() as db:
        UserAgentService(db).create(
            user_id=None,
            operator_name="admin",
            owner_type="admin",
            data={"name": "管理员下发", "description": "d", "system_prompt": "p"},
        )

    listed = impl.list_my_agents(user_id=OWNER)
    assert listed["count"] == 0, "管理员下发的智能体不该出现在「我的智能体」里"


# ── Capability flag gating ──────────────────────────────────────────────────
def test_write_verbs_blocked_without_capability(am_env, monkeypatch):
    from mcp_servers.agent_manager_mcp import impl

    agent_id = _create()["agent"]["agent_id"]

    import core.auth.capabilities as caps

    monkeypatch.setattr(caps, "resolve_user_capabilities", lambda db, uid: {"can_add_agent": False})

    for res in (
        impl.create_agent(user_id=OWNER, name="n", description="d", system_prompt="p"),
        impl.edit_agent(user_id=OWNER, agent_ref=agent_id, description="d2"),
        impl.delete_agent(user_id=OWNER, agent_ref=agent_id),
    ):
        assert not res["ok"]
        assert "can_add_agent" in res["message"]

    # Read-only verbs stay usable.
    assert impl.list_my_agents(user_id=OWNER)["ok"]


def test_missing_user_header_is_refused(am_env):
    from mcp_servers.agent_manager_mcp import impl

    for res in (
        impl.list_my_agents(user_id=""),
        impl.create_agent(user_id="", name="n", description="d", system_prompt="p"),
        impl.delete_agent(user_id="", agent_ref="x"),
        impl.search_agent_market(user_id=""),
    ):
        assert not res["ok"] and "用户身份" in res["message"]


# ── Bindable capabilities / market verbs ────────────────────────────────────
def test_list_bindable_capabilities_shape(am_env):
    from mcp_servers.agent_manager_mcp import impl

    res = impl.list_bindable_capabilities(user_id=OWNER)
    assert res["ok"], res
    for key in ("skills", "mcp_servers", "plugins", "kb_spaces"):
        assert isinstance(res[key], list)


def test_search_agent_market_lists_preset_bundles(am_env):
    from mcp_servers.agent_manager_mcp import impl

    res = impl.search_agent_market(user_id=OWNER)
    assert res["ok"], res
    assert res["count"] > 0, "仓库自带的预置子智能体应当能被搜到"
    assert {"slug", "name", "installed"} <= set(res["agents"][0])

    # Keyword filtering actually narrows the set.
    narrowed = impl.search_agent_market(user_id=OWNER, query="__no_such_agent__")
    assert narrowed["count"] == 0


def test_install_market_agent_then_owned_by_me(am_env):
    from mcp_servers.agent_manager_mcp import impl

    listed = impl.search_agent_market(user_id=OWNER)
    slug = listed["agents"][0]["slug"]

    res = impl.install_market_agent(user_id=OWNER, slug=slug)
    assert res["ok"], res
    assert res["agent_id"]

    mine = impl.list_my_agents(user_id=OWNER)
    assert mine["count"] == 1
    assert mine["agents"][0]["from_market"] == slug

    with am_env.Session() as db:
        row = db.query(UserAgent).filter(UserAgent.agent_id == res["agent_id"]).first()
        assert row.owner_type == "user" and row.user_id == OWNER


def test_install_unknown_market_slug_fails_cleanly(am_env):
    from mcp_servers.agent_manager_mcp import impl

    res = impl.install_market_agent(user_id=OWNER, slug="__nope__")
    assert not res["ok"] and res["message"].startswith("❌")


def test_submit_agent_to_market_creates_pending_submission(am_env):
    from mcp_servers.agent_manager_mcp import impl

    agent_id = _create()["agent"]["agent_id"]
    res = impl.submit_agent_to_market(
        user_id=OWNER, agent_id=agent_id, category="职场办公", summary="周报", note="请审核"
    )
    assert res["ok"], res
    assert res["status"] == "pending"
    assert "审核" in res["message"], "必须让用户知道这是申请、还要等审核"


def test_submit_rejects_wrong_category_and_lists_valid_ones(am_env):
    """子智能体市场的分类是自己的一套（与技能市场的 8 类不同）。传错要挡下来并回列合法值，
    否则模型只能靠猜，每次上架都要先失败一轮。"""
    from mcp_servers.agent_manager_mcp import impl

    agent_id = _create()["agent"]["agent_id"]
    res = impl.submit_agent_to_market(user_id=OWNER, agent_id=agent_id, category="办公效率")
    assert not res["ok"]
    assert "通用助手" in res["message"]
    assert res["valid_categories"] == impl.valid_categories()


def test_tool_description_lists_the_real_categories():
    """工具描述里的分类清单必须与代码里的单一真相源一致，否则描述会悄悄过期。"""
    import json

    from mcp_servers.agent_manager_mcp import impl, server

    cats = impl.valid_categories()
    assert len(cats) == 9

    manifest = json.loads((BUNDLE_DIR / "plugin.json").read_text(encoding="utf-8"))
    tools = manifest["extensions"]["org.hugagent"]["mcp"]["agent_manager"]["tools"]
    desc = next(t["description"] for t in tools if t["name"] == "submit_agent_to_market")
    doc = server.submit_agent_to_market.__doc__ or ""
    for c in cats:
        assert c in desc, f"plugin.json 的工具描述漏了分类「{c}」"
        assert c in doc, f"server docstring 漏了分类「{c}」"


def test_submit_rejects_agent_not_mine(am_env):
    from mcp_servers.agent_manager_mcp import impl

    agent_id = _create()["agent"]["agent_id"]
    res = impl.submit_agent_to_market(user_id=OTHER, agent_id=agent_id)
    assert not res["ok"]


# ── Server layer: header plumbing + tool surface ────────────────────────────
def test_server_tools_forward_user_header(am_env, monkeypatch):
    from mcp_servers.agent_manager_mcp import server

    captured = {}

    def _fake_list(*, user_id):
        captured["user_id"] = user_id
        return {"ok": True, "agents": [], "count": 0, "message": ""}

    monkeypatch.setattr(server.impl, "list_my_agents", _fake_list)

    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-current-user-id": OWNER})
        )
    )
    asyncio.run(server.list_my_agents(ctx=ctx))
    assert captured["user_id"] == OWNER

    # No context (e.g. stdio debug) → empty user id, and impl refuses downstream.
    captured.clear()
    asyncio.run(server.list_my_agents(ctx=None))
    assert captured["user_id"] == ""


def test_every_server_tool_is_async_and_documented():
    from mcp_servers.agent_manager_mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 8
    for t in tools:
        fn = getattr(server, t.name)
        assert inspect.iscoroutinefunction(fn), f"{t.name} 必须是 async"
        assert (fn.__doc__ or "").strip(), f"{t.name} 缺 docstring（它就是模型看到的工具描述）"
