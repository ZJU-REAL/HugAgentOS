"""Parsing an extractor reply that a reasoning model produced.

A reasoning model narrates before it answers, and its narration quotes the
prompt's own worked examples — JSON included. Every failure below was observed
against a live reasoning endpoint, and each one is silent by construction: the
extractor returns None, nothing is written, and the product reports that the
turn had nothing worth remembering.
"""

import pytest
from core.memory.extractors import procedural
from core.memory.extractors._base import parse_json


def test_a_plain_json_reply_still_parses():
    assert parse_json('{"facts": [{"field": "dept"}]}', require_key="facts") == {
        "facts": [{"field": "dept"}]
    }


def test_a_fenced_reply_still_parses():
    raw = '```json\n{"procedures": []}\n```'
    assert parse_json(raw, require_key="procedures") == {"procedures": []}


def test_the_answer_after_a_closing_think_tag_wins():
    # Observed shape: the model narrates immediately with no opening tag and
    # emits only `</think>` before answering. Matching <think>…</think> pairs
    # alone leaves the whole narration in place.
    raw = (
        '先分析一下，示例里给的是 {"facts": [{"field": "name"}]} 这种格式。'
        '</think>{"facts": [{"field": "dept", "value": "研发中心"}]}'
    )
    assert parse_json(raw, require_key="facts") == {
        "facts": [{"field": "dept", "value": "研发中心"}]
    }


def test_a_paired_think_block_is_removed():
    raw = '<think>考虑 {"facts": []} 这个例子</think>\n{"facts": [{"field": "role"}]}'
    assert parse_json(raw, require_key="facts") == {"facts": [{"field": "role"}]}


def test_the_last_object_wins_over_examples_quoted_while_thinking():
    # A greedy `\\{.*\\}` spans from the first brace to the last and parses as
    # nothing; taking the first object returns the example the model was citing.
    raw = (
        '提示词示例是 {"facts": []}，另一个例子 {"facts": [{"field": "x"}]}，'
        '最终答案：{"facts": [{"field": "dept", "value": "研发中心"}]}'
    )
    assert parse_json(raw, require_key="facts")["facts"][0]["value"] == "研发中心"


def test_reasoning_debris_without_the_expected_key_is_rejected():
    # A response truncated mid-thought leaves half-formed objects behind. This
    # one parses cleanly and is *not* the answer — persisting it would write a
    # memory the model never committed to.
    raw = '如果我输出 {"field": "style", "value": "结论先行"} 就违反了示例值…'
    assert parse_json(raw, require_key="facts") is None


def test_a_truncated_reply_yields_nothing_rather_than_a_fragment():
    raw = '我先分析一下这轮对话，用户说以后要简洁，那么 {"field": "verbosity"'
    assert parse_json(raw, require_key="facts") is None


def test_a_non_dict_top_level_is_rejected():
    assert parse_json("[1, 2, 3]", require_key="facts") is None


def test_empty_input_is_rejected():
    assert parse_json("", require_key="facts") is None
    assert parse_json("完全没有 JSON 的一段话", require_key="facts") is None


@pytest.mark.asyncio
async def test_verified_correction_retries_with_focused_procedure_prompt(monkeypatch):
    replies = iter(
        [
            '{"procedures": [{"rule": "a"}, {"rule": "b"}, {"rule": "c"}]}',
            '{"procedures": [{"rule": "下载 PDF 时用 curl 保存到沙盒后验证完整性", '
            '"why": "网页正文抓取不能替代二进制文件下载", '
            '"applies_to": "外部 PDF 下载与交付", "strength": "strong"}]}',
        ]
    )
    prompts = []

    async def fake_llm(prompt, *, timeout_s, max_tokens=800):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(procedural, "run_llm_with_prompt", fake_llm)
    result = await procedural.extract(
        "那为什么之前一直失败？",
        "之前误把网页抓取失败当成无法下载，改用 curl 后成功。",
        5,
        recent_trajectory=(
            "[ASSISTANT] 沙盒无网络，无法下载\n"
            "[USER] 你把 OSS 链接下载到沙盒再交付\n"
            "[ASSISTANT] curl 下载成功并验证完整"
        ),
        verified_correction=True,
    )

    assert result["procedures"][0]["strength"] == "strong"
    assert len(result["procedures"]) == 1
    assert len(prompts) == 2
    assert "已经由程序确认" in prompts[1]
