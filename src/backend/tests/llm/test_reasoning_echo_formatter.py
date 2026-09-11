"""Assistant history keeps the reasoning_content that thinking-mode endpoints demand.

Regression pin for "the answer is cut off right after the tools finish". DeepSeek V4 in
thinking mode rejects any request whose assistant messages lost the reasoning they
produced, and an assistant message is only ever sent back when the turn called a tool —
so the failure looked like plain truncation while ordinary Q&A stayed healthy. The stock
OpenAI formatter drops thinking on purpose; this one hands it back, for every model, with
nothing to configure.
"""

import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock

from core.llm.chat_models import ReasoningEchoChatFormatter, make_chat_model


def _assistant_with_thinking():
    return Msg(
        name="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="先查四篇论文"),
            ToolCallBlock(
                type="tool_call", id="call_1", name="paper_search", input='{"q": "agent"}'
            ),
        ],
        role="assistant",
    )


def _tool_result():
    return Msg(
        name="assistant",
        content=[
            ToolResultBlock(type="tool_result", id="call_1", name="paper_search", output="4 篇")
        ],
        role="assistant",
    )


def _mk(**kw):
    return make_chat_model(
        model="test-model",
        temperature=0.6,
        max_tokens=1024,
        timeout=60,
        base_url="http://example.invalid/v1",
        api_key="dummy",
        context_size=4096,
        **{"provider": "openai_compatible", **kw},
    )


@pytest.mark.asyncio
async def test_thinking_is_handed_back_on_the_assistant_row():
    rows = await ReasoningEchoChatFormatter().format([_assistant_with_thinking()])
    assistant = [r for r in rows if r["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == "先查四篇论文"
    assert assistant[0]["tool_calls"][0]["function"]["name"] == "paper_search"


@pytest.mark.asyncio
async def test_a_turn_without_thinking_sends_no_field_at_all():
    # Matches deepseek-harness. It also means endpoints that never emit reasoning on this
    # channel — OpenAI included — get a request identical to the stock formatter's.
    msg = Msg(name="assistant", content=[TextBlock(type="text", text="好的")], role="assistant")
    rows = await ReasoningEchoChatFormatter().format([msg])
    assert "reasoning_content" not in rows[0]
    assert rows == await OpenAIChatFormatter().format([msg])


@pytest.mark.asyncio
async def test_reasoning_lands_only_on_the_turn_that_produced_it():
    # A turn's reasoning must not bleed onto the user row before it or the tool-result
    # carrier after it — the endpoint pairs reasoning with the message that emitted it.
    msgs = [
        Msg(name="user", content=[TextBlock(type="text", text="查一下")], role="user"),
        _assistant_with_thinking(),
        _tool_result(),
    ]
    rows = await ReasoningEchoChatFormatter().format(msgs)
    carrying = [r for r in rows if "reasoning_content" in r]
    assert len(carrying) == 1
    assert carrying[0]["reasoning_content"] == "先查四篇论文"
    assert carrying[0]["tool_calls"][0]["function"]["name"] == "paper_search"


@pytest.mark.asyncio
async def test_row_structure_matches_the_plain_openai_formatter():
    # The echo only adds a field: dropping or merging rows would desync the provenance
    # row mapping that the retry path rebuilds from per-message formatting.
    msgs = [
        Msg(name="user", content=[TextBlock(type="text", text="查一下")], role="user"),
        _assistant_with_thinking(),
        _tool_result(),
    ]
    echoed = await ReasoningEchoChatFormatter().format(msgs)
    plain = await OpenAIChatFormatter().format(msgs)
    assert [r["role"] for r in echoed] == [r["role"] for r in plain]
    assert [{k: v for k, v in r.items() if k != "reasoning_content"} for r in echoed] == plain


def _accumulated_react_turn():
    """One Msg holding a whole ReAct loop, as AgentScope actually builds it.

    ``agent/_agent.py`` extends the last assistant message in place on every step, so
    think → call → result → think → call → result → think → answer all share one Msg.
    """
    return Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="思考A"),
            ToolCallBlock(type="tool_call", id="c1", name="paper_search", input="{}"),
            ToolResultBlock(type="tool_result", id="c1", name="paper_search", output="结果A"),
            ThinkingBlock(type="thinking", thinking="思考B"),
            ToolCallBlock(type="tool_call", id="c2", name="patent_search", input="{}"),
            ToolResultBlock(type="tool_result", id="c2", name="patent_search", output="结果B"),
            ThinkingBlock(type="thinking", thinking="思考C"),
            TextBlock(type="text", text="最终答案"),
        ],
    )


@pytest.mark.asyncio
async def test_each_react_step_carries_only_its_own_reasoning():
    # Attaching the Msg's whole reasoning to every row would rewrite history — the second
    # tool call would look reasoned with conclusions drawn after it — and would resend the
    # same text once per step, diverging from what the IR budgeted for it once.
    rows = await ReasoningEchoChatFormatter().format([_accumulated_react_turn()])
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert len(assistants) == 3
    assert assistants[0]["reasoning_content"] == "思考A"
    assert assistants[0]["tool_calls"][0]["function"]["name"] == "paper_search"
    assert assistants[1]["reasoning_content"] == "思考B"
    assert assistants[1]["tool_calls"][0]["function"]["name"] == "patent_search"
    assert assistants[2]["reasoning_content"] == "思考C"
    assert "tool_calls" not in assistants[2]


@pytest.mark.asyncio
async def test_step_splitting_does_not_alter_the_row_structure():
    # Splitting is only about *which* row each reasoning lands on; the wire shape must stay
    # byte-identical to the stock formatter once reasoning_content is removed.
    msgs = [
        Msg(name="user", content=[TextBlock(type="text", text="查一下")], role="user"),
        _accumulated_react_turn(),
    ]
    echoed = await ReasoningEchoChatFormatter().format(msgs)
    plain = await OpenAIChatFormatter().format(msgs)
    assert [{k: v for k, v in r.items() if k != "reasoning_content"} for r in echoed] == plain


@pytest.mark.asyncio
async def test_parallel_tool_calls_in_one_step_share_that_step_reasoning():
    # Two calls issued together, then both results: one flush, so one step — the reasoning
    # belongs to the single assistant row that carries both calls.
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="并行查两处"),
            ToolCallBlock(type="tool_call", id="c1", name="a", input="{}"),
            ToolCallBlock(type="tool_call", id="c2", name="b", input="{}"),
            ToolResultBlock(type="tool_result", id="c1", name="a", output="ra"),
            ToolResultBlock(type="tool_result", id="c2", name="b", output="rb"),
        ],
    )
    rows = await ReasoningEchoChatFormatter().format([msg])
    assistants = [r for r in rows if r["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["reasoning_content"] == "并行查两处"
    assert len(assistants[0]["tool_calls"]) == 2
    assert len([r for r in rows if r["role"] == "tool"]) == 2


def test_every_openai_compatible_model_echoes_its_reasoning():
    # Not a per-model option: reasoning goes back for every model, so no configuration
    # can leave a thinking model in the state that 400s on every tool-calling turn.
    assert isinstance(_mk().formatter, ReasoningEchoChatFormatter)
    assert isinstance(_mk(provider="deepseek").formatter, ReasoningEchoChatFormatter)
    assert isinstance(_mk(provider="openai").formatter, ReasoningEchoChatFormatter)
