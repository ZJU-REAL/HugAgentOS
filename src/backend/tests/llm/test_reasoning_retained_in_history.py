"""Past reasoning stays in the model's history instead of being deleted at assembly.

The reference harnesses keep reasoning across turns (openai/codex counts its
tokens toward the budget, deepseek-harness passes CoT back on every
reasoning-carrying turn). Reasoning is part of what the assistant said; whether
an endpoint accepts it back is the formatter's decision, so the ThinkingBlock
has to survive replay for that decision to exist at all. Replay comes only from
the canonical step record.
"""

from core.llm.runtime.message_compat import dict_to_msg
from core.llm.runtime.model_steps import assistant_row_replay, record_assistant_step, record_tool_result_step


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
            [
                {"type": "thinking", "thinking": "再对比"},
                {"type": "text", "text": "结论是A"},
            ],
            provider="deepseek",
            model="v4",
            protocol="openai_chat",
        ),
    ]


def test_replay_with_tool_calls_keeps_each_step_reasoning():
    rows = assistant_row_replay(
        content="结论是A",
        model_steps=_steps_with_tool(),
    )
    assert rows[0]["content"][0]["thinking"] == "先查论文"
    assert rows[0]["content"][1]["type"] == "tool_call"
    assert rows[2]["content"][0]["thinking"] == "再对比"
    assert {"type": "text", "text": "结论是A"} in rows[2]["content"]


def test_replay_without_tool_calls_keeps_reasoning():
    steps = [
        record_assistant_step(
            [
                {"type": "thinking", "thinking": "先查论文"},
                {"type": "text", "text": "结论是A"},
            ],
            provider="p",
            model="m",
            protocol="openai_chat",
        )
    ]
    rows = assistant_row_replay(content="结论是A", model_steps=steps)
    assert rows[0]["content"][0]["thinking"] == "先查论文"


def test_reasoning_survives_history_replay_into_the_model_context():
    rows = assistant_row_replay(
        content="结论是A",
        model_steps=_steps_with_tool(),
    )
    assert _thinking_texts(dict_to_msg(rows[0], created_seq=0)) == ["先查论文"]
    assert _thinking_texts(dict_to_msg(rows[2], created_seq=2)) == ["再对比"]
