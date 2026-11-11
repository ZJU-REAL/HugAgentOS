"""KB catalog prompt section: never waits on the provider, never sticks in the prompt cache."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import features.knowledge.edition as kb_edition
from core.db import engine as db_engine
from core.db.models import AdminPromptPart, ContentBlock
from core.llm import prompt_storage as pvs
from features.knowledge.models import KBSpace
from prompts import kb_lite_section, prompt_runtime
from prompts.prompt_config import PromptConfig, SystemPromptConfig

_EXT = "92f92c25-443f-459c-a832-0f9f5ea1d572"


@pytest.fixture
def directory(monkeypatch):
    state = SimpleNamespace(enabled=True, items=None, reads=0)

    def snapshot():
        state.reads += 1
        return state.items

    monkeypatch.setattr(kb_edition, "is_enabled", lambda: state.enabled, raising=False)
    monkeypatch.setattr(kb_edition, "collection_snapshot", snapshot, raising=False)
    return state


@pytest.fixture
def local_db(monkeypatch):
    engine = create_engine("sqlite://")
    KBSpace.__table__.create(engine)
    ContentBlock.__table__.create(engine)
    AdminPromptPart.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr(pvs, "SessionLocal", sessions)
    with sessions() as db:
        db.add(KBSpace(kb_id="kb_mine", user_id="u1", name="我的库", description="私有简介", visibility="private"))
        db.add(KBSpace(kb_id="kb_gone", user_id="u1", name="已删", visibility="private",
                       deleted_at=__import__("datetime").datetime(2026, 1, 1)))
        db.commit()
    yield sessions
    engine.dispose()


def test_cold_directory_names_external_collections_by_id(directory, local_db):
    section = kb_lite_section.build_kb_catalog_section([_EXT])
    assert f"- 外部知识库（公有，dataset_id: `{_EXT}`）" in section
    assert section.startswith("## 当前启用的知识库")


def test_cached_directory_renders_names_and_drops_unknown_ids(directory, local_db):
    directory.items = [
        {"id": _EXT, "name": "碳化硅产业", "description": "行业研究" * 50},
        {"id": "other", "name": "未启用"},
    ]
    section = kb_lite_section.build_kb_catalog_section([_EXT, "missing-id"])
    assert f"- 碳化硅产业（公有，dataset_id: `{_EXT}`）：" in section
    assert "行业研究" * 30 in section and "行业研究" * 31 not in section  # 120-char cap
    assert "未启用" not in section and "missing-id" not in section


def test_private_kbs_come_from_the_database_and_skip_deleted(directory, local_db):
    section = kb_lite_section.build_kb_catalog_section(["kb_mine", "kb_gone", "kb_mine"])
    assert section.count("我的库（私有，kb_id: `kb_mine`）：私有简介") == 1
    assert "已删" not in section
    assert directory.reads == 0, "no external ids → the directory is not consulted"


def test_nothing_resolvable_renders_nothing(directory, local_db):
    directory.enabled = False
    assert kb_lite_section.build_kb_catalog_section([_EXT]) == ""
    assert kb_lite_section.build_kb_catalog_section([]) == ""
    assert kb_lite_section.build_kb_catalog_section(None) == ""


def test_directory_failure_keeps_the_private_part(directory, local_db, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(kb_edition, "collection_snapshot", broken, raising=False)
    section = kb_lite_section.build_kb_catalog_section([_EXT, "kb_mine"])
    assert "我的库" in section and _EXT not in section


def test_prompt_cache_is_keyed_by_the_rendered_section(local_db, monkeypatch):
    """A cold (ids-only) rendering must not be served for the 5-minute prompt TTL."""
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "test")
    prompt_runtime.invalidate_prompt_cache()
    config = PromptConfig(system_prompt=SystemPromptConfig(provider="inline", inline_template="BASE"))

    cold = prompt_runtime.build_system_prompt(
        config, ctx={"now": "2026-11-06", "kb_catalog_section": "## 当前启用的知识库\n- 外部知识库（id）"}
    )
    warm = prompt_runtime.build_system_prompt(
        config, ctx={"now": "2026-11-06", "kb_catalog_section": "## 当前启用的知识库\n- 碳化硅产业"}
    )
    none = prompt_runtime.build_system_prompt(config, ctx={"now": "2026-11-06"})

    assert "外部知识库（id）" in cold and "碳化硅产业" not in cold
    assert "碳化硅产业" in warm and "外部知识库（id）" not in warm
    assert "当前启用的知识库" not in none
    prompt_runtime.invalidate_prompt_cache()


def test_subagent_prompt_places_the_prerendered_section():
    agent = SimpleNamespace(system_prompt="ROLE", agent_id="a1")
    with_kb = prompt_runtime.build_subagent_system_prompt(agent, [], [], kb_catalog_section="## 当前启用的知识库\n- X")
    without = prompt_runtime.build_subagent_system_prompt(agent, [], [])
    assert "## 当前启用的知识库\n- X" in with_kb
    assert "当前启用的知识库" not in without
    assert with_kb.rstrip().splitlines()[-1].startswith("20"), "the date stays last"


@pytest.mark.asyncio
async def test_factory_renders_the_section_off_the_event_loop(monkeypatch):
    from core.llm.factory import assembly

    loop_thread = threading.get_ident()
    seen = {}

    def build(ids):
        seen["thread"] = threading.get_ident()
        seen["ids"] = ids
        return "SECTION"

    monkeypatch.setattr(kb_lite_section, "build_kb_catalog_section", build)
    request = SimpleNamespace(profile=None, enabled_kb_ids=[_EXT])

    task = assembly._start_kb_catalog_section(request)
    assert isinstance(task, asyncio.Task)
    assert await task == "SECTION"
    assert seen == {"thread": seen["thread"], "ids": [_EXT]}
    assert seen["thread"] != loop_thread

    assert assembly._start_kb_catalog_section(SimpleNamespace(profile=None, enabled_kb_ids=[])) is None
    assert assembly._start_kb_catalog_section(SimpleNamespace(profile="x", enabled_kb_ids=[_EXT])) is None


@pytest.mark.asyncio
async def test_factory_section_failure_is_consumed_when_assembly_aborts(monkeypatch):
    import gc

    from core.llm.factory import assembly

    def build(_ids):
        raise RuntimeError("boom")

    reported = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        monkeypatch.setattr(kb_lite_section, "build_kb_catalog_section", build)
        task = assembly._start_kb_catalog_section(SimpleNamespace(profile=None, enabled_kb_ids=[_EXT]))
        await asyncio.wait({task})
        del task  # assembly aborted before awaiting it
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert reported == [], "an abandoned section task must not log 'exception was never retrieved'"


def test_section_bytes_do_not_depend_on_id_order(directory, local_db):
    directory.items = [{"id": _EXT, "name": "产业研究"}, {"id": "b-ext", "name": "政策制度"}]
    forward = kb_lite_section.build_kb_catalog_section([_EXT, "kb_mine", "b-ext"])
    backward = kb_lite_section.build_kb_catalog_section(["b-ext", "kb_mine", _EXT, _EXT])
    assert forward == backward and "产业研究" in forward and "政策制度" in forward


class _Stop(Exception):
    pass


def _phase_kwargs(section):
    return dict(
        _agent_ref=None, _elapsed=lambda: "0ms", _log=SimpleNamespace(info=lambda *a, **k: None),
        _manifest_builder=SimpleNamespace(add_prompt_section=lambda *a, **k: None),
        _ontology_hidden_tools=set(), _ontology_runtime={}, _plan_tool_enabled=False,
        _plugin_runtime=None, _progressive=None, _sbx_sess=None, _subagent_progressive=None,
        _tool_approval_available=False, _turbo_code_exec=False, cfg=SimpleNamespace(),
        enabled_mcp_keys=[], kb_catalog_section=section, loader=None,
        skill_ids_to_register=[], tool_schemas=[], toolkit=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent", [False, True])
async def test_prompt_phase_hands_the_section_to_the_builder(monkeypatch, subagent):
    from core.llm.factory.prompts import system as phase

    captured = {}

    def main_builder(_cfg, ctx=None, manifest_builder=None):
        captured["section"] = ctx["kb_catalog_section"]
        raise _Stop

    def sub_builder(_agent, _tools, _mcp, kb_catalog_section=""):
        captured["section"] = kb_catalog_section
        raise _Stop

    monkeypatch.setattr(phase, "build_system_prompt", main_builder)
    monkeypatch.setattr(phase, "build_subagent_system_prompt", sub_builder)
    request = SimpleNamespace(
        profile=None, user_agent=SimpleNamespace(agent_id="a") if subagent else None,
        mode_spec=None, turbo_mode=False, chat_id="c", approval_mode=None, project_ctx=None,
        enabled_kb_ids=[_EXT],
    )
    with pytest.raises(_Stop):
        await phase.system_prompt(request, **_phase_kwargs("## 当前启用的知识库\n- 产业研究"))
    assert captured == {"section": "## 当前启用的知识库\n- 产业研究"}
