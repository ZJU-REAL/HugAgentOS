"""Cancelled display tails must survive model-facing history reload."""

from types import SimpleNamespace

from core.chat.compaction.history import normalize_rows


def test_cancelled_tail_survives_without_repeating_completed_text():
    row = SimpleNamespace(
        role="assistant",
        content="finished answer\npartial answer",
        thinking=[{"content": "unfinished private reasoning"}],
        tool_calls=[],
        extra_data={"cancelled": True},
        model_steps=[
            {
                "schema": "harness.steps.v1",
                "kind": "assistant",
                "provider": "deepseek",
                "model": "m",
                "protocol": "openai_chat",
                "blocks": [{"type": "text", "text": "finished answer"}],
            }
        ],
    )
    rows = normalize_rows([row])
    text = "".join(b.get("text", "") for r in rows for b in r["content"])
    assert text == "finished answer\npartial answer\n\n[本轮回答被用户中断]"
    assert "unfinished private reasoning" not in str(rows)


def test_completed_tail_is_not_appended_twice():
    row = SimpleNamespace(
        role="assistant",
        content="answer",
        thinking=None,
        tool_calls=None,
        extra_data={"cancelled": True},
        model_steps=[
            {
                "schema": "harness.steps.v1",
                "kind": "assistant",
                "blocks": [{"type": "text", "text": "answer"}],
            }
        ],
    )
    assert normalize_rows([row]) == [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "answer\n\n[本轮回答被用户中断]"}],
        }
    ]


def test_divergent_display_is_preserved_without_guessing_a_native_step():
    from copy import deepcopy

    steps = [
        {
            "schema": "harness.steps.v1",
            "kind": "assistant",
            "blocks": [{"type": "text", "text": "recorded answer"}],
        }
    ]
    original = deepcopy(steps)
    row = SimpleNamespace(
        role="assistant",
        content="different displayed draft",
        thinking=None,
        tool_calls=None,
        model_steps=steps,
        extra_data={"cancelled": True},
    )
    result = normalize_rows([row])
    assert "recorded answer" in str(result)
    assert "different displayed draft" in str(result)
    assert "与完整步骤记录不一致" in str(result)
    assert steps == original


def test_cancelled_tail_tolerates_invalid_blocks_like_the_existing_replay_parser():
    row = SimpleNamespace(
        role="assistant",
        content="answer tail",
        thinking=None,
        tool_calls=None,
        extra_data={"cancelled": True},
        model_steps=[
            {
                "schema": "harness.steps.v1",
                "kind": "assistant",
                "blocks": [None, {"type": "text", "text": "answer"}],
            }
        ],
    )
    rows = normalize_rows([row])
    text = "".join(b.get("text", "") for r in rows for b in r["content"])
    assert text == "answer tail\n\n[本轮回答被用户中断]"


def test_cancel_after_answer_replacement_does_not_repeat_the_last_answer():
    row = SimpleNamespace(
        role="assistant",
        content="revised answer",
        thinking=None,
        tool_calls=None,
        extra_data={"cancelled": True},
        model_steps=[
            {
                "schema": "harness.steps.v1",
                "kind": "assistant",
                "blocks": [{"type": "text", "text": "earlier progress"}],
            },
            {
                "schema": "harness.steps.v1",
                "kind": "assistant",
                "blocks": [{"type": "text", "text": "revised answer"}],
            },
        ],
    )
    rows = normalize_rows([row])
    text = "".join(b.get("text", "") for r in rows for b in r["content"])
    assert text.count("revised answer") == 1
    assert "与完整步骤记录不一致" not in text
