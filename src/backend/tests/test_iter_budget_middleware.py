"""IterBudgetReminderMiddleware unit tests.

Tests only the pure logic _maybe_remind (synchronous), without starting a real agent: the middleware only depends on
agent.react_config.max_iters / agent.state.{cur_iter, reply_id, context},
which can be faked with SimpleNamespace.
"""

from types import SimpleNamespace

import pytest

from core.llm.middlewares import IterBudgetReminderMiddleware


def _fake_agent(max_iters: int, cur_iter: int, reply_id: str = "r1"):
    return SimpleNamespace(
        react_config=SimpleNamespace(max_iters=max_iters),
        state=SimpleNamespace(cur_iter=cur_iter, reply_id=reply_id, context=[]),
    )


def _reminder_texts(agent) -> list:
    texts = []
    for msg in agent.state.context:
        for block in msg.content or []:
            texts.append(getattr(block, "text", ""))
    return texts


def _remind(mw, agent):
    mw._maybe_remind(agent, *mw._budget(agent))


def _force(mw, agent, input_kwargs):
    max_iters, _cur, remaining = mw._budget(agent)
    return mw._maybe_force_text(input_kwargs, max_iters, remaining)


def test_no_remind_far_from_limit():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=5)  # 5 iterations left
    _remind(mw, agent)
    assert agent.state.context == []


def test_remind_at_threshold_mentions_remaining():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)  # 2 iterations left (including this one)
    _remind(mw, agent)
    texts = _reminder_texts(agent)
    assert len(texts) == 1
    assert "system-reminder" in texts[0]
    assert "还剩 2 轮" in texts[0]


def test_last_round_forbids_tool_calls():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9)  # last iteration
    _remind(mw, agent)
    texts = _reminder_texts(agent)
    assert len(texts) == 1
    assert "最后一轮" in texts[0]
    assert "不要再调用任何工具" in texts[0]


def test_dedupe_same_round_reminds_once():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)
    _remind(mw, agent)
    _remind(mw, agent)  # same (reply_id, cur_iter) triggered again
    assert len(_reminder_texts(agent)) == 1


def test_escalates_across_rounds():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)
    _remind(mw, agent)
    agent.state.cur_iter = 9  # enter the next iteration
    _remind(mw, agent)
    texts = _reminder_texts(agent)
    assert len(texts) == 2
    assert "还剩 2 轮" in texts[0]
    assert "最后一轮" in texts[1]


def test_new_reply_resets_dedupe():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9, reply_id="r1")
    _remind(mw, agent)
    # New reply: cur_iter is reset to zero and then runs to the critical iteration again
    agent.state.reply_id = "r2"
    agent.state.cur_iter = 9
    agent.state.context.clear()
    _remind(mw, agent)
    assert len(_reminder_texts(agent)) == 1


@pytest.mark.parametrize("max_iters", [1, 2, 3])
def test_tiny_budget_never_reminds(max_iters):
    mw = IterBudgetReminderMiddleware()  # threshold=2 -> max_iters<=3 skipped
    agent = _fake_agent(max_iters=max_iters, cur_iter=max(0, max_iters - 1))
    _remind(mw, agent)
    assert agent.state.context == []


# ── _maybe_force_text ──────────────────────────────────────────────────────


def test_force_text_on_final_round():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9)  # final round
    out = _force(mw, agent, {"tool_choice": None})
    tc = out.get("tool_choice")
    assert tc is not None and tc.mode == "none"


def test_no_force_text_before_final_round():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)  # 2 left
    out = _force(mw, agent, {"tool_choice": None})
    assert out.get("tool_choice") is None


def test_force_text_respects_explicit_tool_choice():
    sentinel = object()
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9)
    out = _force(mw, agent, {"tool_choice": sentinel})
    assert out["tool_choice"] is sentinel


@pytest.mark.parametrize("max_iters", [1, 2, 3])
def test_force_text_skips_tiny_budget(max_iters):
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=max_iters, cur_iter=max(0, max_iters - 1))
    out = _force(mw, agent, {"tool_choice": None})
    assert out.get("tool_choice") is None


def test_force_text_kill_switch(monkeypatch):
    monkeypatch.setenv("CHAT_FINAL_ITER_FORCE_TEXT", "false")
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9)
    out = _force(mw, agent, {"tool_choice": None})
    assert out.get("tool_choice") is None


# ── Turn-budget hint (static policy, injected only where a budget exists) ──


def test_turn_budget_hint_states_the_actual_number():
    from core.llm.agent_factory import _render_turn_budget_hint

    hint = _render_turn_budget_hint(12)
    assert "12 轮" in hint
    # The point of the hint is the parallel fan-out strategy, not the scary
    # number — one that only announces a limit makes the model cautious
    # rather than efficient.
    assert "并行" in hint


def test_turn_budget_hint_is_byte_stable():
    """Prefix-cache safety: same budget → same bytes, every request."""
    from core.llm.agent_factory import _render_turn_budget_hint

    assert _render_turn_budget_hint(30) == _render_turn_budget_hint(30)


def test_unbounded_profile_is_valid_and_bad_bounds_are_not():
    from core.evolution import agent_profile as AP

    unbounded = AP.builtin_profile()
    assert unbounded.max_react_turns == AP.UNBOUNDED_REACT_TURNS
    ok, problems = AP.validate_profile(unbounded)
    assert ok, problems

    fenced = AP.builtin_profile()
    fenced.max_react_turns = 1  # below MIN_REACT_TURNS, and not the 0 sentinel
    ok, problems = AP.validate_profile(fenced)
    assert ok is False
    assert any("最大轮数" in p for p in problems)
