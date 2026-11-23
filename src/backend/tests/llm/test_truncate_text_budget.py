"""truncate_text keeps its exact cut while finding it without O(n²) re-encoding."""
from __future__ import annotations

import json
import random
import time

from core.llm.context.content import truncate_text
from core.llm.subagents import sessions


def _reference_truncate_text(text: str, max_tokens: int, *, tail_only: bool = False) -> str:
    """The original shrink-one-character loop: the cut the fast version must reproduce."""
    if not text or max_tokens <= 0:
        return ""
    max_bytes = max(4, max_tokens * 4)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    max_chars = max(1, max_bytes)
    head = tail = 0
    marker = ""
    if tail_only:
        tail = max_chars
        result = text[-tail:]
    elif max_chars < 40:
        head = max_chars
        result = text[:head]
    else:
        marker = "\n[… omitted …]\n"
        body = max(2, max_chars - len(marker))
        head = max(1, int(body * 0.6))
        tail = max(1, body - head)
        result = text[:head] + marker + text[-tail:]
    while len(result.encode("utf-8")) > max_bytes and len(result) > 1:
        if tail_only:
            tail = max(1, tail - 1)
            result = text[-tail:]
        elif marker:
            if head >= tail and head > 1:
                head -= 1
            elif tail > 1:
                tail -= 1
            elif head > 1:
                head -= 1
            else:
                break
            result = text[:head] + marker + text[-tail:]
        else:
            head = max(1, head - 1)
            result = text[:head]
    return result


def test_cut_matches_the_reference_for_mixed_width_text():
    alphabet = ["a", " ", "\n", "é", "机", "构", "😀", "…", '"']
    rng = random.Random(7)
    for _ in range(3000):
        size = rng.choice([1, 5, 30, 200, 1000])
        chars = rng.sample(alphabet, rng.randint(1, len(alphabet)))
        text = "".join(rng.choice(chars) for _ in range(rng.randint(0, size)))
        max_tokens = rng.choice([0, 1, 2, 5, 9, 10, 11, 20, 100, 300])
        for tail_only in (False, True):
            assert truncate_text(text, max_tokens, tail_only=tail_only) == _reference_truncate_text(
                text, max_tokens, tail_only=tail_only
            )


def test_cjk_tool_result_just_over_budget_is_cut_quickly():
    # A 20k-token budget and a CJK result 84 tokens over it took ~1.5 s per
    # call when every removed character re-encoded the whole candidate.
    text = ("机构编制表人员信息台账查询结果，" * 6000)[: 20_084 * 4 // 3]
    started = time.perf_counter()
    for _ in range(20):
        result = truncate_text(text, 20_000)
    elapsed = time.perf_counter() - started
    assert len(result.encode("utf-8")) <= 80_000
    assert result.startswith(text[:100]) and result.endswith(text[-100:])
    assert elapsed < 1.0, f"20 cuts took {elapsed:.2f}s"


def test_subagent_payload_drops_the_same_turns_quickly():
    messages = [{"role": "user", "content": "原始任务"}] + [
        {"role": "assistant" if i % 2 else "tool", "content": f"第{i}轮" + "结果" * (200 + i % 50)}
        for i in range(4000)
    ]
    started = time.perf_counter()
    payload = sessions._fit_payload({"agent_id": "a", "messages": list(messages)})
    elapsed = time.perf_counter() - started

    kept = json.loads(payload)["messages"]
    assert len(payload.encode("utf-8")) <= sessions._MAX_PAYLOAD_BYTES
    assert kept[0] == messages[0] and kept[-1] == messages[-1]
    # Oldest turns go first, and only as many as needed: one more would fit.
    dropped = len(messages) - len(kept)
    one_fewer = [messages[0]] + messages[dropped:]
    assert kept == [messages[0]] + messages[dropped + 1:]
    assert len(json.dumps({"agent_id": "a", "messages": one_fewer}, ensure_ascii=False).encode()) > (
        sessions._MAX_PAYLOAD_BYTES
    )
    assert elapsed < 1.0, f"fitting took {elapsed:.2f}s"
