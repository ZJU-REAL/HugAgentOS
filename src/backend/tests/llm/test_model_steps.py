"""The step record is the model's own history, written as it happened.

One entry per model response with the exact blocks the provider returned, one
entry per tool result as it entered the context. Replay yields the same row
sequence the agent held in memory, so the next turn sends back what was
actually said — never a reassembly guessed from separate columns.
"""

from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from core.llm import model_steps as M


def _assistant(*blocks, provider="deepseek", model="deepseek-v4"):
    return M.record_assistant_step(blocks, provider=provider, model=model, protocol="openai_chat")


def _result(call_id, output="ok", state="success"):
    return M.record_tool_result_step(
        ToolResultBlock(type="tool_result", id=call_id, name="search", output=output, state=state)
    )


def test_assistant_step_keeps_the_provider_blocks_verbatim_with_their_origin():
    step = _assistant(
        ThinkingBlock(type="thinking", thinking="先查", signature="sig-A"),
        TextBlock(type="text", text="查一下"),
        ToolCallBlock(type="tool_call", id="c1", name="search", input='{"q": 1}'),
    )

    assert step["kind"] == "assistant"
    assert (step["provider"], step["model"], step["protocol"]) == (
        "deepseek",
        "deepseek-v4",
        "openai_chat",
    )
    assert [b["type"] for b in step["blocks"]] == ["thinking", "text", "tool_call"]
    # Provider-specific extra fields travel with the reasoning; transport ids do not.
    assert step["blocks"][0]["signature"] == "sig-A"
    assert "id" not in step["blocks"][0] and "id" not in step["blocks"][1]
    assert step["blocks"][2] == {
        "type": "tool_call",
        "id": "c1",
        "name": "search",
        "input": '{"q": 1}',
    }


def test_tool_result_step_records_the_bounded_output_and_state():
    step = _result("c1", output=[TextBlock(type="text", text="hit 3")], state="error")
    assert step["blocks"] == [
        {
            "type": "tool_result",
            "id": "c1",
            "name": "search",
            "output": [{"type": "text", "text": "hit 3"}],
            "state": "error",
        }
    ]


def test_binary_media_in_a_tool_result_is_replaced_by_an_explicit_note():
    step = M.record_tool_result_step(
        {
            "type": "tool_result",
            "id": "c1",
            "name": "shot",
            "output": [
                {"type": "text", "text": "caption"},
                {
                    "type": "data",
                    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                },
            ],
        }
    )
    output = step["blocks"][0]["output"]
    assert output[0] == {"type": "text", "text": "caption"}
    assert output[1]["type"] == "text" and "image omitted" in output[1]["text"]
    assert "AAAA" not in str(output)


def test_replay_reproduces_three_serial_steps_in_order_without_duplication():
    steps = [
        _assistant(
            ThinkingBlock(type="thinking", thinking="A"),
            ToolCallBlock(type="tool_call", id="c1", name="s", input="{}"),
        ),
        _result("c1", "rA"),
        _assistant(
            ThinkingBlock(type="thinking", thinking="B"),
            ToolCallBlock(type="tool_call", id="c2", name="s", input="{}"),
        ),
        _result("c2", "rB"),
        _assistant(
            ThinkingBlock(type="thinking", thinking="C"), TextBlock(type="text", text="done")
        ),
    ]

    rows = M.replay_rows(steps)

    assert [r["role"] for r in rows] == ["assistant", "tool", "assistant", "tool", "assistant"]
    assert [
        b["thinking"]
        for r in rows
        if r["role"] == "assistant"
        for b in r["content"]
        if b["type"] == "thinking"
    ] == ["A", "B", "C"]
    assert [b["id"] for r in rows if r["role"] == "tool" for b in r["content"]] == ["c1", "c2"]
    assert rows[-1]["content"][-1] == {"type": "text", "text": "done"}


def test_replay_keeps_two_parallel_calls_in_one_step_with_both_results_after_it():
    steps = [
        _assistant(
            ThinkingBlock(type="thinking", thinking="并行"),
            ToolCallBlock(type="tool_call", id="c1", name="a", input="{}"),
            ToolCallBlock(type="tool_call", id="c2", name="b", input="{}"),
        ),
        _result("c1", "ra"),
        _result("c2", "rb"),
        _assistant(TextBlock(type="text", text="done")),
    ]

    rows = M.replay_rows(steps)

    assert [r["role"] for r in rows] == ["assistant", "tool", "assistant"]
    assert [b["id"] for b in rows[0]["content"] if b["type"] == "tool_call"] == ["c1", "c2"]
    assert [b["id"] for b in rows[1]["content"]] == ["c1", "c2"]


def test_replayed_thinking_carries_the_provider_that_produced_it():
    rows = M.replay_rows(
        [_assistant(ThinkingBlock(type="thinking", thinking="A", signature="sig"))]
    )
    block = rows[0]["content"][0]
    assert block["thinking"] == "A"
    assert (block["provider"], block["model"]) == ("deepseek", "deepseek-v4")
    assert block["signature"] == "sig"


def test_a_call_that_never_returned_is_closed_explicitly_on_replay():
    steps = [_assistant(ToolCallBlock(type="tool_call", id="c1", name="s", input="{}"))]

    rows = M.replay_rows(steps)

    assert [r["role"] for r in rows] == ["assistant", "tool"]
    result = rows[1]["content"][0]
    assert result["id"] == "c1" and result["state"] == "interrupted"
    assert "interrupted" in result["output"]


def test_close_dangling_calls_balances_the_record_at_rest():
    steps = [
        _assistant(
            ToolCallBlock(type="tool_call", id="c1", name="s", input="{}"),
            ToolCallBlock(type="tool_call", id="c2", name="s", input="{}"),
        ),
        _result("c1"),
    ]

    M.close_dangling_calls(steps)

    assert [s["kind"] for s in steps] == ["assistant", "tool_result", "tool_result"]
    assert steps[-1]["blocks"][0]["id"] == "c2"
    assert steps[-1]["blocks"][0]["state"] == "interrupted"
    # Idempotent: nothing left dangling.
    assert M.close_dangling_calls(list(steps)) == steps


def test_cancelled_turn_is_marked_at_the_end_of_the_last_answer():
    rows = M.replay_rows([_assistant(TextBlock(type="text", text="我先看看"))], cancelled=True)
    assert rows[-1]["content"] == [{"type": "text", "text": "我先看看\n\n[本轮回答被用户中断]"}]

    rows = M.replay_rows(
        [_assistant(ToolCallBlock(type="tool_call", id="c1", name="s", input="{}")), _result("c1")],
        cancelled=True,
    )
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == [{"type": "text", "text": "[本轮回答被用户中断]"}]


def test_replace_final_text_rewrites_only_the_last_answer_text():
    steps = [
        _assistant(
            ThinkingBlock(type="thinking", thinking="A"),
            ToolCallBlock(type="tool_call", id="c1", name="s", input="{}"),
        ),
        _result("c1"),
        _assistant(
            ThinkingBlock(type="thinking", thinking="B"), TextBlock(type="text", text="draft")
        ),
    ]

    M.replace_final_text(steps, "revised")

    assert steps[-1]["blocks"] == [
        {"type": "thinking", "thinking": "B"},
        {"type": "text", "text": "revised"},
    ]
    assert steps[0]["blocks"][1]["type"] == "tool_call"


def test_rows_from_segments_rebuilds_steps_from_the_streamed_display_order():
    rows = M.rows_from_segments(
        content="正文A正文B",
        thinking=[{"content": "想0"}, {"content": "想1"}],
        tool_calls=[
            {
                "tool_name": "bash",
                "tool_id": "call-0",
                "tool_args": {"command": "ls"},
                "result": "ok0",
            },
            {
                "tool_name": "bash",
                "tool_id": "call-1",
                "tool_args": {"command": "pwd"},
                "result": "ok1",
                "status": "error",
            },
        ],
        segments=[
            {"type": "thinking", "index": 0},
            {"type": "tool", "index": 0},
            {"type": "text", "text": "正文A"},
            {"type": "thinking", "index": 1},
            {"type": "tool", "index": 1},
            {"type": "text", "text": "正文B"},
        ],
    )

    assert [r["role"] for r in rows] == ["assistant", "tool", "assistant", "tool", "assistant"]
    assert [b["type"] for b in rows[0]["content"]] == ["thinking", "tool_call"]
    assert rows[1]["content"][0]["id"] == "call-0"
    assert [b["type"] for b in rows[2]["content"]] == ["text", "thinking", "tool_call"]
    assert rows[3]["content"][0]["output"].startswith("[status=error]")
    assert rows[4]["content"] == [{"type": "text", "text": "正文B"}]


def test_a_row_with_no_order_record_replays_as_an_explicit_digest():
    rows = M.legacy_digest_rows(
        content="已获取",
        thinking=[{"content": "先看"}],
        tool_calls=[{"tool_name": "web_fetch", "tool_args": {"url": "u"}, "result": "X" * 50}],
    )

    assert len(rows) == 1 and rows[0]["role"] == "assistant"
    types = [b["type"] for b in rows[0]["content"]]
    assert types == ["thinking", "text", "text"]
    assert "tool_call" not in types
    digest = rows[0]["content"][-1]["text"]
    assert digest.startswith("[历史工具调用摘要")
    assert "web_fetch" in digest and "X" * 50 in digest


def test_assistant_row_replay_prefers_the_step_record_then_segments_then_digest():
    steps = [_assistant(TextBlock(type="text", text="from steps"))]
    segments = [{"type": "text", "text": "from segments"}]

    assert (
        M.assistant_row_replay(
            content="c", model_steps=steps, thinking=None, tool_calls=None, segments=segments
        )[0]["content"][0]["text"]
        == "from steps"
    )
    assert (
        M.assistant_row_replay(
            content="c", model_steps=None, thinking=None, tool_calls=None, segments=segments
        )[0]["content"][0]["text"]
        == "from segments"
    )
    assert M.assistant_row_replay(
        content="c", model_steps=None, thinking=None, tool_calls=None, segments=None
    )[0]["content"] == [{"type": "text", "text": "c"}]


def test_explode_history_rows_splits_an_accumulated_assistant_row_per_step():
    history = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "A"},
                {"type": "tool_call", "id": "c1", "name": "s", "input": "{}"},
                {"type": "tool_result", "id": "c1", "name": "s", "output": "r"},
                {"type": "thinking", "thinking": "B"},
                {"type": "text", "text": "done"},
            ],
        },
    ]

    rows = M.explode_history_rows(history)

    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert [b["type"] for b in rows[1]["content"]] == ["thinking", "tool_call"]
    assert [b["type"] for b in rows[3]["content"]] == ["thinking", "text"]
