"""Resumable sub-agent sessions and the prompt rules that drive them."""

import asyncio

import pytest
from core.infra.ephemeral import LocalEphemeralState
from core.llm.subagents import sessions as subagent_sessions
from core.capabilities.agents.builtin import BUILTIN_SUBAGENTS, list_builtin_subagents, load_builtin_subagent_prompt
from core.llm.subagents.tool import build_subagent_prompt_section


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Run against the in-process backend, as a Redis-free deployment does.

    One instance for the whole test, so a save and the load that follows it
    see the same state.
    """
    state = LocalEphemeralState()
    monkeypatch.setattr(subagent_sessions, "get_ephemeral_state", lambda: state)


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
    assert "按「适用场景」选择角色" in section
    assert "用户用 @名称 指定了它" in section
    assert "不要派出去再干等" in section
    assert "一段完整的工作" in section
    # synthesis stays with the coordinator
    assert "判断问题出在哪、该怎么改，必须由你自己完成" in section
    assert "具体要查什么、回答什么" in section
    # parallel write safety
    assert "互不重叠的文件范围" in section
    # resume vs fresh
    assert "resume_session_id" in section
    assert "审查他人产出时，新开一个" in section
    assert "重派时说明上次具体哪里失败" in section
    # user-approved privileged actions
    assert "把原话逐字写进 task 并新开执行" in section
    # result handling
    assert "先自己核验实际产出再汇报" in section


def test_role_prompts_keep_their_safety_and_hygiene_rules():
    prompts = {spec.role: load_builtin_subagent_prompt(spec) for spec in BUILTIN_SUBAGENTS}

    for role, text in prompts.items():
        assert "不构成用户授权" in text, role
        assert "不要为此新建报告、总结或分析类文件" in text, role

    assert "只改 task 指定给你的文件" in prompts["worker"]
    assert "修根因" in prompts["worker"]
    assert "不要给弱产出盖橡皮图章" in prompts["reviewer"]
    assert "与本次改动无关" in prompts["reviewer"]
