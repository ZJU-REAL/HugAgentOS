"""Progressive plugin loading（渐进式插件加载）单元测试。

覆盖三块：装配期延迟解析（可见性 / enabled 交集 / stdio 排除 / 激活与显式呼唤
排除 + 落库）、插件目录 prompt 段的稳定渲染、``load_plugin`` 激活工具的运行时
副作用（Toolkit basic 组追加 / allow_rules / close_list / 会话粘滞落库）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import core.db.engine as dbe
import pytest
from core.db.models import ChatSession, InstalledPlugin, UserShadow
from core.llm import plugin_loader
from core.llm.tool_collector import ToolCollector
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

USER = "ppl_user"
CHAT = "ppl_chat_1"


@pytest.fixture()
def ppl_env(tmp_path, monkeypatch):
    """Isolated sqlite DB bound to SessionLocal + stubbed MCP/skill services."""
    url = f"sqlite:///{tmp_path}/ppl.db"
    engine = create_engine(url)
    dbe.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbe, "SessionLocal", TestSession)

    with TestSession() as db:
        db.add(UserShadow(user_id=USER, username=USER))
        db.add(ChatSession(chat_id=CHAT, user_id=USER, title="t"))
        db.add(
            InstalledPlugin(
                install_id="crawler@global",
                slug="crawler",
                name="爬虫插件",
                description="抓取网页并结构化。",
                component_ids={"skills": ["crawler-scrape-a1"], "mcp": ["crawler_mcp"]},
            )
        )
        db.add(
            InstalledPlugin(
                install_id="localtool@global",
                slug="localtool",
                name="本地工具",
                description="stdio 形态的本地插件。",
                component_ids={"skills": [], "mcp": ["local_stdio_mcp"]},
            )
        )
        db.add(
            InstalledPlugin(
                install_id="secret@other",
                slug="secret",
                name="别人的私有插件",
                description="不可见。",
                owner_user_id="someone_else",
                component_ids={"skills": [], "mcp": ["crawler_mcp"]},
            )
        )
        db.commit()

    # MCP server configs: crawler_mcp is HTTP, local_stdio_mcp is stdio.
    server_cfgs = {
        "crawler_mcp": {"transport": "streamable_http", "url": "http://mcp:9999/mcp"},
        "local_stdio_mcp": {"transport": "stdio", "command": "python"},
    }
    svc = SimpleNamespace(
        get_all_servers=lambda enabled_only=True: dict(server_cfgs),
        get_owned_servers=lambda uid, enabled_only=False: {},
    )
    from core.services.mcp_service import McpServerConfigService

    monkeypatch.setattr(McpServerConfigService, "get_instance", classmethod(lambda cls: svc))

    # Skill metadata: crawler skill binds no extra MCP.
    meta = {
        "crawler-scrape-a1": SimpleNamespace(description="抓取单页", tags=[], mcp_server_ids=[])
    }
    loader_stub = SimpleNamespace(
        load_all_metadata=lambda: dict(meta),
        get_skill_dir=lambda sid: None,
    )
    import core.agent_skills.loader as skills_loader_mod

    monkeypatch.setattr(skills_loader_mod, "get_skill_loader", lambda: loader_stub)

    return SimpleNamespace(Session=TestSession, loader=loader_stub, server_cfgs=server_cfgs)


def _resolve(env, **overrides):
    kwargs = dict(
        user_id=USER,
        chat_id=CHAT,
        enabled_skill_ids=["crawler-scrape-a1", "plain-skill"],
        enabled_mcp_ids=["crawler_mcp", "local_stdio_mcp", "internet_search"],
        invoked_skill_ids=None,
        invoked_mcp_ids=None,
    )
    kwargs.update(overrides)
    return plugin_loader.resolve_progressive_plugins(**kwargs)


def test_defers_visible_http_plugin_and_keeps_stdio_eager(ppl_env):
    res = _resolve(ppl_env)
    assert [p.slug for p in res.deferred] == ["crawler"]
    assert res.deferred_skill_ids == {"crawler-scrape-a1"}
    assert res.deferred_mcp_ids == {"crawler_mcp"}
    # stdio 插件不延迟也不进目录；他人私有插件不可见。
    assert [p.slug for p in res.directory] == ["crawler"]
    # 非插件组件不受影响（调用方只做差集，这里不应出现）。
    assert "plain-skill" not in res.deferred_skill_ids
    assert "internet_search" not in res.deferred_mcp_ids


def test_disabled_components_make_plugin_ineligible(ppl_env):
    res = _resolve(ppl_env, enabled_skill_ids=[], enabled_mcp_ids=["internet_search"])
    assert res.deferred == []
    assert res.directory == []


def test_activated_plugin_stays_eager_but_remains_in_directory(ppl_env):
    with ppl_env.Session() as db:
        row = db.query(ChatSession).filter(ChatSession.chat_id == CHAT).first()
        row.extra_data = {"activated_plugins": ["crawler"]}
        db.commit()
    res = _resolve(ppl_env)
    assert res.deferred == []
    assert [p.slug for p in res.directory] == ["crawler"]
    assert res.activated_slugs == ["crawler"]


def test_explicit_invocation_pins_activation(ppl_env):
    res = _resolve(ppl_env, invoked_skill_ids=["crawler-scrape-a1"])
    assert res.deferred == []
    assert "crawler" in res.activated_slugs
    # 落库粘滞：下一轮装配读到激活态。
    assert plugin_loader.load_activated_plugin_slugs(CHAT) == ["crawler"]


def test_directory_section_stable_and_empty_safe(ppl_env):
    res = _resolve(ppl_env)
    section = plugin_loader.build_plugin_directory_section(res.directory)
    assert "`crawler`" in section and "爬虫插件" in section and "load_plugin" in section
    assert plugin_loader.build_plugin_directory_section([]) == ""
    # 激活状态不改变目录字节（前缀缓存友好）。
    with ppl_env.Session() as db:
        row = db.query(ChatSession).filter(ChatSession.chat_id == CHAT).first()
        row.extra_data = {"activated_plugins": ["crawler"]}
        db.commit()
    res2 = _resolve(ppl_env)
    assert plugin_loader.build_plugin_directory_section(res2.directory) == section


class _FakeMCPClient:
    def __init__(self, name, cfg):
        self.name = name

    async def list_tools(self):
        return [
            SimpleNamespace(
                name=name,
                description=f"{name} tool",
                input_schema={"type": "object", "properties": {}},
            )
            for name in ("crawl_url", "crawl_site")
        ]


@pytest.mark.asyncio
async def test_load_plugin_tool_activates_in_place(ppl_env, tmp_path, monkeypatch):
    import core.llm.mcp_pool as mcp_pool
    from core.llm.agent_factory import cache_compaction_execution_surface
    from core.ontology.toolkit import OntologyFilteredToolkit
    from core.services import compaction_service as compaction
    from core.services.chat_service import ChatService
    from orchestration.workflow import _compaction_budget_inputs

    monkeypatch.setattr(
        mcp_pool,
        "make_client",
        lambda key, cfg, is_stateful=True, **kw: _FakeMCPClient(key, cfg),
    )

    # 技能目录物化桩：含 SKILL.md 的临时目录。
    skill_dir = tmp_path / "crawler-scrape-a1"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: crawler-scrape-a1\ndescription: 抓取单页\n---\nbody",
        encoding="utf-8",
    )
    ppl_env.loader.get_skill_dir = lambda sid: str(skill_dir)

    res = _resolve(ppl_env)
    collector = ToolCollector()
    tk = OntologyFilteredToolkit(tools=[], mcps=[])
    budget_agent = SimpleNamespace(_jx_compaction_reserved_output_tokens=256)
    tk.set_execution_surface_listener(
        lambda surface: cache_compaction_execution_surface(
            budget_agent, "base system prompt", surface
        )
    )
    initial_surface = await tk.freeze_execution_surface()
    close_list: list = []
    permission_context = SimpleNamespace(allow_rules={})
    from core.llm.tool_permissions import ToolPermissionRegistry

    permission_registry = ToolPermissionRegistry()
    runtime = {
        "activated_slugs": set(),
        "connected_keys": {"internet_search"},
        "toolkit": tk,
        "permission_context": permission_context,
        "permission_registry": permission_registry,
        "close_list": close_list,
        "loader": ppl_env.loader,
        "chat_id": CHAT,
        "user_id": USER,
        "enabled_kb_ids": [],
        "channel_origin": None,
        "reranker_enabled": False,
        "approval_available": False,
        "ontology_runtime": {},
    }
    plugin_loader.register_load_plugin(collector, res.deferred_by_slug(), runtime)
    tool = collector.get_tool("load_plugin")
    assert tool is not None

    resp = await tool._func(plugin="crawler")
    text = "".join(getattr(b, "text", "") for b in resp.content)
    assert "已加载" in text and "crawl_url" in text and "crawler-scrape-a1" in text

    basic = tk.tool_groups[0]
    assert len(basic.mcps) == 1 and len(close_list) == 1
    assert {r for r in permission_context.allow_rules} == {"crawl_url", "crawl_site"}
    assert permission_registry.names == frozenset()
    assert len(basic.skills_or_loaders) == 1
    assert "crawler" in runtime["activated_slugs"]
    assert plugin_loader.load_activated_plugin_slugs(CHAT) == ["crawler"]

    final_surface = await tk.freeze_execution_surface()
    assert final_surface.generation > initial_surface.generation
    assert any(
        schema.get("function", {}).get("name") == "crawl_url"
        for schema in budget_agent._jx_compaction_tool_schemas
    )
    assert "crawler-scrape-a1" in budget_agent._jx_compaction_system_prompt

    # The internal workflow handoff must carry the *latest* generation into a
    # real post-turn checkpoint, not the initial pre-plugin surface.
    async def fake_summary(history, *, timeout):
        return "plugin-aware summary"

    monkeypatch.setattr(compaction, "_summarize", fake_summary)
    with ppl_env.Session() as db:
        ChatService(db).add_message(CHAT, "user", "compact after plugin load")
    inputs = _compaction_budget_inputs(budget_agent, 4096)
    assert await compaction.run_post_turn_compaction(CHAT, budget_inputs=inputs)
    with ppl_env.Session() as db:
        checkpoint = ChatService(db).get_latest_compaction_checkpoint(CHAT)
        estimate = checkpoint.extra_data["replacement_manifest"]["budget_estimate"]
    assert estimate["tool_schema_tokens"] > 0
    assert estimate["system_prompt_tokens"] > compaction.C.approx_token_count("base system prompt")
    assert estimate["reserved_output_tokens"] == 256

    # 重复加载幂等，未知插件报错并列出可用项。
    resp2 = await tool._func(plugin="crawler")
    text2 = "".join(getattr(b, "text", "") for b in resp2.content)
    assert "无需重复调用" in text2
    assert len(basic.mcps) == 1

    resp3 = await tool._func(plugin="nope")
    text3 = "".join(getattr(b, "text", "") for b in resp3.content)
    assert "未找到插件" in text3 and "`crawler`" in text3


def test_resolve_bound_defers_http_plugins_and_applies_skill_filter(ppl_env):
    res = plugin_loader.resolve_bound_progressive_plugins(
        ["crawler@global", "localtool@global", "missing@global"],
        skill_filter=lambda sids: [s for s in sids if s != "crawler-scrape-a1"],
    )
    # crawler 仍有 MCP 组件 → 延迟；skill_filter 生效（技能被属主过滤掉）。
    assert [p.slug for p in res.deferred] == ["crawler"]
    assert res.deferred_skill_ids == set()
    assert res.deferred_mcp_ids == {"crawler_mcp"}
    # stdio 插件不进延迟集（调用方按老路径 eager 展开）。
    assert all(p.slug != "localtool" for p in res.directory)


def test_load_plugin_no_persist_for_subagent_runtime(ppl_env, tmp_path, monkeypatch):
    import core.llm.mcp_pool as mcp_pool
    from agentscope.tool import Toolkit

    monkeypatch.setattr(
        mcp_pool,
        "make_client",
        lambda key, cfg, is_stateful=True, **kw: _FakeMCPClient(key, cfg),
    )
    res = plugin_loader.resolve_bound_progressive_plugins(["crawler@global"])
    collector = ToolCollector()
    from core.llm.tool_permissions import ToolPermissionRegistry

    runtime = {
        "activated_slugs": set(),
        "connected_keys": set(),
        "toolkit": Toolkit(tools=[], mcps=[]),
        "permission_context": SimpleNamespace(allow_rules={}),
        "permission_registry": ToolPermissionRegistry(),
        "close_list": [],
        "persist": False,
        "loader": ppl_env.loader,
        "chat_id": CHAT,
        "user_id": USER,
        "enabled_kb_ids": [],
        "channel_origin": None,
        "reranker_enabled": False,
        "approval_available": False,
        "ontology_runtime": {},
    }
    plugin_loader.register_load_plugin(collector, res.deferred_by_slug(), runtime)
    tool = collector.get_tool("load_plugin")
    resp = asyncio.run(tool._func(plugin="crawler"))
    text = "".join(getattr(b, "text", "") for b in resp.content)
    assert "已加载" in text
    assert "crawler" in runtime["activated_slugs"]
    # 子智能体激活不落库——父会话的激活列表保持为空。
    assert plugin_loader.load_activated_plugin_slugs(CHAT) == []


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("PLUGIN_PROGRESSIVE_LOADING", "false")
    assert plugin_loader.progressive_plugin_loading_enabled() is False
    monkeypatch.delenv("PLUGIN_PROGRESSIVE_LOADING", raising=False)
    assert plugin_loader.progressive_plugin_loading_enabled() is True


def test_pre_turn_compaction_inputs_match_the_service_signature():
    from inspect import signature

    from core.services.compaction_service import maybe_run_pre_turn_compaction
    from orchestration.workflow import _pre_turn_compaction_inputs

    agent = SimpleNamespace(
        model=SimpleNamespace(model="model-a"),
        state=SimpleNamespace(model_name="model-a", model_provider_id="provider-a"),
        _jx_compaction_system_prompt="system",
        _jx_compaction_tool_schemas=[],
        _jx_compaction_reserved_output_tokens=256,
    )
    kwargs = _pre_turn_compaction_inputs(agent, 4096, "model-a")

    signature(maybe_run_pre_turn_compaction).bind(
        "chat-1",
        [{"role": "user", "content": "hello"}],
        run_id="run-1",
        **kwargs,
    )
    assert kwargs["model_name"] == "model-a"
    assert kwargs["context_window"] == 4096
    assert "model_provider_id" not in kwargs
