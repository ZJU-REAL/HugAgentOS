"""Past reasoning stays in the model's history instead of being deleted at assembly.

The reference harnesses keep reasoning across turns (openai/codex counts its
tokens toward the budget, deepseek-harness passes CoT back on every
reasoning-carrying turn). Reasoning is part of what the assistant said; whether
an endpoint accepts it back is the formatter's decision, so the ThinkingBlock
has to survive replay for that decision to exist at all. Replay now comes from
the step record; rows that predate it fall back to the streamed display order.
"""

from core.llm.message_compat import dict_to_msg
from core.llm.model_steps import (
    assistant_row_replay,
    record_assistant_step,
    record_tool_result_step,
)


def _thinking_texts(msg):
    return [b.thinking for b in msg.get_content_blocks() if getattr(b, "thinking", None)]


def _steps_with_tool():
    return [
        record_assistant_step(
            [
                {"type": "thinking", "thinking": "先查论文"},
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "paper_search",
                    "input": '{"q": "agent"}',
                },
            ],
            provider="deepseek",
            model="v4",
            protocol="openai_chat",
        ),
        record_tool_result_step(
            {
                "type": "tool_result",
                "id": "c1",
                "name": "paper_search",
                "output": "4 篇",
                "state": "success",
            }
        ),
        record_assistant_step(
            [{"type": "thinking", "thinking": "再对比"}, {"type": "text", "text": "结论是A"}],
            provider="deepseek",
            model="v4",
            protocol="openai_chat",
        ),
    ]


def test_replay_with_tool_calls_keeps_each_step_reasoning():
    rows = assistant_row_replay(
        content="结论是A",
        model_steps=_steps_with_tool(),
        thinking=None,
        tool_calls=None,
        segments=None,
    )
    assert rows[0]["content"][0]["thinking"] == "先查论文"
    assert rows[0]["content"][1]["type"] == "tool_call"
    assert rows[2]["content"][0]["thinking"] == "再对比"
    assert {"type": "text", "text": "结论是A"} in rows[2]["content"]


def test_replay_without_tool_calls_keeps_reasoning():
    steps = [
        record_assistant_step(
            [{"type": "thinking", "thinking": "先查论文"}, {"type": "text", "text": "结论是A"}],
            provider="p",
            model="m",
            protocol="openai_chat",
        )
    ]
    rows = assistant_row_replay(
        content="结论是A", model_steps=steps, thinking=None, tool_calls=None, segments=None
    )
    assert rows[0]["content"][0]["thinking"] == "先查论文"


def test_streamed_display_order_replays_reasoning_when_no_step_record_exists():
    rows = assistant_row_replay(
        content="结论是A",
        model_steps=None,
        thinking=[{"content": "先查论文"}],
        tool_calls=[
            {
                "tool_name": "paper_search",
                "tool_id": "c1",
                "tool_args": {"q": "agent"},
                "result": "4 篇",
            }
        ],
        segments=[
            {"type": "thinking", "index": 0},
            {"type": "tool", "index": 0},
            {"type": "text", "text": "结论是A"},
        ],
    )
    assert rows[0]["content"][0] == {"type": "thinking", "thinking": "先查论文"}
    assert rows[1]["role"] == "tool"
    assert rows[2]["content"] == [{"type": "text", "text": "结论是A"}]


def test_a_row_without_any_order_record_keeps_reasoning_in_its_digest():
    rows = assistant_row_replay(
        content="结论是A",
        model_steps=None,
        thinking=[{"content": "先查论文"}],
        tool_calls=None,
        segments=None,
    )
    assert rows == [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "先查论文"},
                {"type": "text", "text": "结论是A"},
            ],
        }
    ]


def test_reasoning_survives_history_replay_into_the_model_context():
    rows = assistant_row_replay(
        content="结论是A",
        model_steps=_steps_with_tool(),
        thinking=None,
        tool_calls=None,
        segments=None,
    )
    assert _thinking_texts(dict_to_msg(rows[0], created_seq=0)) == ["先查论文"]
    assert _thinking_texts(dict_to_msg(rows[2], created_seq=2)) == ["再对比"]
