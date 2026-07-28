"""Regression tests for structured reasoning SSE protocol markers."""

from types import SimpleNamespace

from core.chat.tool_log import build_thinking_event
from orchestration.streaming import StreamingAgent


def test_streaming_agent_emits_structured_reasoning_protocol_once():
    agent = SimpleNamespace(model=SimpleNamespace(structured_reasoning=True))
    streaming_agent = StreamingAgent(agent, mcp_clients=[])

    assert streaming_agent._take_reasoning_protocol() == {"structured_reasoning": True}
    assert streaming_agent._take_reasoning_protocol() is None


def test_streaming_agent_skips_protocol_for_inline_reasoning_models():
    agent = SimpleNamespace(model=SimpleNamespace(structured_reasoning=False))
    streaming_agent = StreamingAgent(agent, mcp_clients=[])

    assert streaming_agent._take_reasoning_protocol() is None


def test_thinking_event_preserves_structured_reasoning_marker():
    event = build_thinking_event(
        {"type": "thinking", "structured_reasoning": True},
        "chat_test",
    )

    assert event == {
        "type": "thinking",
        "chat_id": "chat_test",
        "structured_reasoning": True,
        "message": "正在思考...",
    }
