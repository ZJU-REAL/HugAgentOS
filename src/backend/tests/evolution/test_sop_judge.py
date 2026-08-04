"""The semantic gate after the rule gates: LLM adjudication of SOP proposals.

The rules can measure recurrence, cohesion and success; they cannot tell a
distinctive procedure from a generic tool combination, or a real intent cluster
from a coincidental merge. These tests pin the adjudicator's contract — and,
more importantly, its refusal semantics: every uncertain path refuses.
"""

import asyncio

import core.memory.extractors._base as base
from core.evolution.sop_judge import SopVerdict, adjudicate_sequence_sop


def _judge(monkeypatch, reply):
    async def fake_llm(prompt, timeout_s, *, max_tokens=800):
        return reply

    monkeypatch.setattr(base, "run_llm_with_prompt", fake_llm)
    return asyncio.run(
        adjudicate_sequence_sop(
            representative="帮我生成本周销售周报",
            objectives=["帮我生成本周销售周报", "出一版这周的销售周报"],
            tool_sequence=["search", "verify", "chart", "export"],
            occasions=6,
            support=6,
            success_rate=0.9,
        )
    )


def test_a_worthy_verdict_passes_with_reason_and_trigger(monkeypatch):
    verdict = _judge(
        monkeypatch,
        '{"worthy": true, "reason": "同类周报请求，序列步骤有区分度", '
        '"trigger": "用户要求生成周期性销售报告时"}',
    )
    assert verdict.worthy is True
    assert verdict.reason and verdict.trigger
    assert verdict.judge_unavailable is False


def test_a_generic_combination_is_refused(monkeypatch):
    verdict = _judge(
        monkeypatch,
        '{"worthy": false, "reason": "搜索后写文件对任何任务都成立，不携带过程知识", '
        '"trigger": ""}',
    )
    assert verdict.worthy is False
    assert "不携带过程知识" in verdict.reason


def test_an_unreachable_model_refuses_but_is_distinguishable(monkeypatch):
    """"The model was down" and "the proposal was rejected" are different
    operational states and must not look alike in the report."""
    verdict = _judge(monkeypatch, None)
    assert verdict.worthy is False
    assert verdict.judge_unavailable is True


def test_unparseable_output_refuses_rather_than_passes(monkeypatch):
    verdict = _judge(monkeypatch, "我觉得这个提案还不错，可以通过。")
    assert verdict.worthy is False
    assert verdict.judge_unavailable is False


def test_reasoning_noise_before_the_answer_is_tolerated(monkeypatch):
    """A reasoning model narrates before it answers; the last valid object wins."""
    verdict = _judge(
        monkeypatch,
        '先看第一个问题……这些请求都是周报。{"worthy": true, "reason": "ok", "trigger": "周报请求"}',
    )
    assert verdict.worthy is True


def test_verdict_serialises_for_the_credit_record():
    verdict = SopVerdict(worthy=True, reason="r", trigger="t")
    assert verdict.to_dict() == {
        "worthy": True,
        "reason": "r",
        "trigger": "t",
        "judge_unavailable": False,
    }
