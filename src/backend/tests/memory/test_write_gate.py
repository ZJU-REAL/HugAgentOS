"""LLM write gate: narrows the regex candidate set, fails open on any failure.

The gate exists because the regex classify is deliberately loose (procedural has
no keyword gate), so without it the default is to extract-and-write on nearly
every substantive turn. The two properties worth pinning are the contract's
edges: the gate may only *narrow* the candidates, and any failure must pass the
candidates through unchanged rather than silently remembering nothing.
"""

import pytest
from core.memory.extractors import gate as G
from core.memory.extractors.router import ExtractorType

CANDIDATES = {ExtractorType.PROCEDURAL, ExtractorType.PREFERENCE}


def _mock_llm(monkeypatch, response):
    async def fake_llm(_prompt, *, timeout_s, max_tokens=800):
        return response

    monkeypatch.setattr(G, "run_llm_with_prompt", fake_llm)


@pytest.mark.asyncio
async def test_the_gate_narrows_to_what_the_llm_approves(monkeypatch):
    _mock_llm(monkeypatch, '{"classes": ["procedural"]}')
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == {ExtractorType.PROCEDURAL}


@pytest.mark.asyncio
async def test_an_empty_verdict_means_nothing_is_written(monkeypatch):
    _mock_llm(monkeypatch, '{"classes": []}')
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == set()


@pytest.mark.asyncio
async def test_verified_correction_keeps_procedural_even_when_llm_drops_it(monkeypatch):
    _mock_llm(monkeypatch, '{"classes": []}')
    verdict = await G.llm_write_gate(
        "你把链接下载到沙盒再交付",
        "下载成功并验证完整",
        set(CANDIDATES),
        timeout_s=5,
        recent_trajectory="失败 → 用户改法 → 成功",
        verified_correction=True,
    )
    assert verdict == {ExtractorType.PROCEDURAL}


@pytest.mark.asyncio
async def test_the_gate_cannot_widen_the_candidate_set(monkeypatch):
    # LLM approves identity, but the regex never nominated it — it must not appear.
    _mock_llm(monkeypatch, '{"classes": ["identity", "preference"]}')
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == {ExtractorType.PREFERENCE}


@pytest.mark.asyncio
async def test_llm_failure_fails_open(monkeypatch):
    _mock_llm(monkeypatch, None)
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == CANDIDATES


@pytest.mark.asyncio
async def test_garbage_verdict_fails_open(monkeypatch):
    _mock_llm(monkeypatch, "我认为这轮对话很有价值")
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == CANDIDATES


@pytest.mark.asyncio
async def test_unknown_class_names_are_ignored_not_fatal(monkeypatch):
    _mock_llm(monkeypatch, '{"classes": ["procedural", "facts", 42]}')
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == {ExtractorType.PROCEDURAL}


@pytest.mark.asyncio
async def test_reasoning_narration_before_the_json_is_tolerated(monkeypatch):
    _mock_llm(
        monkeypatch,
        '用户提到了周报口径，这是一条做事方式……最终结论：\n{"classes": ["procedural"]}',
    )
    verdict = await G.llm_write_gate("u", "a", set(CANDIDATES), timeout_s=5)
    assert verdict == {ExtractorType.PROCEDURAL}
