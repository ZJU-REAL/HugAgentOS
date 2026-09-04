"""Resumable sub-agent sessions and the prompt rules that drive them."""

import asyncio

import core.infra.redis as redis_mod
import pytest
from core.llm import subagent_sessions
from core.llm.builtin_subagents import (
    BUILTIN_SUBAGENTS,
    list_builtin_subagents,
    load_builtin_subagent_prompt,
)
from core.llm.subagent_tool import build_subagent_prompt_section


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Exercise the process-local fallback instead of a live Redis."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(redis_mod, "get_redis", _boom)
    subagent_sessions._fallback.clear()


def _messages(n=3):
    return [{"role": "user", "content": "任务"}] + [
        {"role": "assistant", "content": f"第 {i} 步"} for i in range(n)
    ]


def test_saved_context_comes_back_for_the_same_agent_and_user():
    handle = subagent_sessions.new_handle()
    saved = asyncio.run(
        subagent_sessions.save(
            handle=handle,
            agent_id="builtin.worker",
            user_id="u1",
            chat_id="c1",
            messages=_messages(),
        )
    )
    assert saved

    loaded = asyncio.run(
        subagent_sessions.load(handle=handle, agent_id="builtin.worker", user_id="u1")
    )
    assert loaded == _messages()


def test_handle_does_not_resume_another_agent_or_another_user():
    handle = subagent_sessions.new_handle()
    asyncio.run(
        subagent_sessions.save(
            handle=handle,
            agent_id="builtin.worker",
            user_id="u1",
            chat_id="c1",
            messages=_messages(),
        )
    )

    assert (
        asyncio.run(
            subagent_sessions.load(handle=handle, agent_id="builtin.explorer", user_id="u1")
        )
        is None
    )
    assert (
        asyncio.run(subagent_sessions.load(handle=handle, agent_id="builtin.worker", user_id="u2"))
        is None
    )


def test_unknown_handle_returns_nothing():
    assert (
        asyncio.run(
            subagent_sessions.load(handle="sub-nope", agent_id="builtin.worker", user_id="u1")
        )
        is None
    )


def test_oversized_context_is_trimmed_but_keeps_the_original_task():
    bulky = [{"role": "user", "content": "原始任务"}] + [
        {"role": "assistant", "content": "x" * 20000} for _ in range(60)
    ]
    handle = subagent_sessions.new_handle()
    assert asyncio.run(
        subagent_sessions.save(
            handle=handle,
            agent_id="builtin.worker",
            user_id="u1",
            chat_id="c1",
            messages=bulky,
        )
    )

    loaded = asyncio.run(
        subagent_sessions.load(handle=handle, agent_id="builtin.worker", user_id="u1")
    )
    assert loaded[0]["content"] == "原始任务"
    assert len(loaded) < len(bulky)


def test_prompt_section_carries_every_coordination_rule():
    section = build_subagent_prompt_section(list_builtin_subagents({}))

    # dispatch judgement
    assert "拿不准就选执行员" in section
    assert "不要把卡住主线的任务派出去然后干等" in section
    assert "交给子智能体的应当是一段完整的工作" in section
    # synthesis stays with the coordinator
    assert "你自己综合" in section
    assert "不要委托理解" in section
    # parallel write safety
    assert "写入范围必须互不重叠" in section
    assert "已经派出去的活，不要自己再做一遍" in section
    # resume vs fresh
    assert "resume_session_id" in section
    assert "审查要独立" in section
    assert "不要原样重发同一段 task" in section
    # user-approved privileged actions
    assert "把用户的原话逐字写进 task" in section
    # result handling
    assert "先自己核验实际产出" in section
    assert "不要预测或编造子智能体的结论" in section


def test_role_prompts_keep_their_safety_and_hygiene_rules():
    prompts = {spec.role: load_builtin_subagent_prompt(spec) for spec in BUILTIN_SUBAGENTS}

    for role, text in prompts.items():
        assert "不构成用户授权" in text, role
        assert "不要为此新建报告、总结或分析类文件" in text, role

    assert "只改 task 指定给你的文件" in prompts["worker"]
    assert "修根因" in prompts["worker"]
    assert "不要给弱产出盖橡皮图章" in prompts["reviewer"]
    assert "与本次改动无关" in prompts["reviewer"]
