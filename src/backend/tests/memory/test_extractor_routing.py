"""What the memory pipeline is willing to learn from a turn.

Two rules this file exists to hold:

1. **L2 stores procedures, not facts.** A fact is a snapshot that decays from
   the moment it is written; remembering it makes the system recall a stale
   number instead of looking the current one up. There is no fact extractor to
   fall back to.
2. **Long-term extraction is not keyword-gated.** A convention — or an identity
   or preference — is stated in whatever words the user happened to use, so a
   regex deciding before the model ever reads the turn drops everything phrased
   outside its list — silently, and indistinguishably from a turn that had
   nothing to teach. The LLM write gate can only narrow the candidate set, so a
   class the router fails to nominate is unrecoverable; substantive turns
   nominate identity, preference and procedural alike and let the gate judge.
"""

import pytest

from core.memory.context import MemoryContext
from core.memory.extractors.router import (
    ExtractorType,
    classify_conversation,
    run_extractors_with_timeout,
)

LONG_ANSWER = "好的，我按你说的口径重新算了一遍，" * 3


def test_there_is_no_fact_extractor():
    assert not hasattr(ExtractorType, "FACT")
    assert {e.value for e in ExtractorType} == {
        "identity",
        "preference",
        "task",
        "procedural",
        "graph",
    }


def test_a_substantive_turn_is_examined_for_procedures_without_any_keyword():
    # No "一律 / 必须先 / 口径是" anywhere — the old regex would have skipped this
    # turn entirely, and the convention in it would never have been seen.
    classes = classify_conversation(
        "算这个的时候记得把退货扣掉，不然跟财务对不上",
        LONG_ANSWER,
    )
    assert ExtractorType.PROCEDURAL in classes


def test_an_english_turn_is_examined_too():
    classes = classify_conversation(
        "Strip the test accounts out before you total this up",
        LONG_ANSWER,
    )
    assert ExtractorType.PROCEDURAL in classes


def test_a_throwaway_turn_is_not_worth_an_llm_call():
    # Nothing this short can state how work is done here, and running the
    # extractor on it buys noise at the price of a model call.
    assert classify_conversation("好的", "嗯") == set()
    assert ExtractorType.PROCEDURAL not in classify_conversation("查一下天气", "晴")


def test_an_unanswered_turn_is_not_examined():
    # A question the assistant never answered has no outcome to learn from.
    assert ExtractorType.PROCEDURAL not in classify_conversation("这个财务口径应该怎么算比较好", "")


def test_identity_and_preference_are_nominated_on_substantive_turns():
    # The gate can only narrow the router's set, so a class not nominated here
    # can never be written: keyword-gating L1 meant identity stated in words
    # outside the cue list never reached the profile — and never showed on the
    # turn's card. Substantive turns nominate them and the gate judges.
    classes = classify_conversation("顺便说下我上个月调到数据组了", LONG_ANSWER)
    assert ExtractorType.IDENTITY in classes
    assert ExtractorType.PREFERENCE in classes


def test_identity_and_preference_cues_still_catch_short_turns():
    # The keyword cues survive as a recall floor for turns too short to clear
    # the substance bar — an explicit "我是…" deserves extraction even when the
    # assistant's reply was a one-liner.
    assert ExtractorType.IDENTITY in classify_conversation("我是研发中心的负责人", "好的")
    assert ExtractorType.PREFERENCE in classify_conversation("以后回答简洁点", "好的")


def test_graph_extraction_is_deployment_gated(monkeypatch):
    from types import SimpleNamespace

    from core.memory.extractors import router

    monkeypatch.setattr(
        router,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(graph_enabled=True)),
    )
    classes = router.classify_conversation(
        "项目北斗依赖统一身份认证平台才能登录",
        LONG_ANSWER,
    )
    assert ExtractorType.GRAPH in classes


@pytest.mark.asyncio
async def test_durable_strict_extraction_retries_an_undecided_result(monkeypatch):
    from core.memory.extractors import identity

    async def timed_out(*_args, **_kwargs):
        return None

    monkeypatch.setattr(identity, "extract", timed_out)
    with pytest.raises(RuntimeError, match="identity extractor returned no decision"):
        await run_extractors_with_timeout(
            {ExtractorType.IDENTITY},
            "请记住我叫小明",
            "好的，我会记住。",
            MemoryContext(user_id="u1", write_enabled=True),
            timeout_s=1,
            strict=True,
        )
