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


def test_no_remind_far_from_limit():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=5)  # 5 iterations left
    mw._maybe_remind(agent)
    assert agent.state.context == []


def test_remind_at_threshold_mentions_remaining():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)  # 2 iterations left (including this one)
    mw._maybe_remind(agent)
    texts = _reminder_texts(agent)
    assert len(texts) == 1
    assert "system-reminder" in texts[0]
    assert "还剩 2 轮" in texts[0]


def test_last_round_forbids_tool_calls():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9)  # last iteration
    mw._maybe_remind(agent)
    texts = _reminder_texts(agent)
    assert len(texts) == 1
    assert "最后一轮" in texts[0]
    assert "不要再调用任何工具" in texts[0]


def test_dedupe_same_round_reminds_once():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)
    mw._maybe_remind(agent)
    mw._maybe_remind(agent)  # same (reply_id, cur_iter) triggered again
    assert len(_reminder_texts(agent)) == 1


def test_escalates_across_rounds():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=8)
    mw._maybe_remind(agent)
    agent.state.cur_iter = 9  # enter the next iteration
    mw._maybe_remind(agent)
    texts = _reminder_texts(agent)
    assert len(texts) == 2
    assert "还剩 2 轮" in texts[0]
    assert "最后一轮" in texts[1]


def test_new_reply_resets_dedupe():
    mw = IterBudgetReminderMiddleware()
    agent = _fake_agent(max_iters=10, cur_iter=9, reply_id="r1")
    mw._maybe_remind(agent)
    # New reply: cur_iter is reset to zero and then runs to the critical iteration again
    agent.state.reply_id = "r2"
    agent.state.cur_iter = 9
    agent.state.context.clear()
    mw._maybe_remind(agent)
    assert len(_reminder_texts(agent)) == 1


@pytest.mark.parametrize("max_iters", [1, 2, 3])
def test_tiny_budget_never_reminds(max_iters):
    mw = IterBudgetReminderMiddleware()  # threshold=2 -> max_iters<=3 skipped
    agent = _fake_agent(max_iters=max_iters, cur_iter=max(0, max_iters - 1))
    mw._maybe_remind(agent)
    assert agent.state.context == []
