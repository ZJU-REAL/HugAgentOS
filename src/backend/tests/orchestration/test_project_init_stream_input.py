"""Check command expansion at the actual AgentScope model-input boundary."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.llm.context_ir import SESSION_CONTEXT_META_KEY
from orchestration.streaming import StreamingAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/init", "/初始化指令"])
@pytest.mark.parametrize("with_metadata", [False, True])
async def test_init_expansion_reaches_model_without_rewriting_history(command, with_metadata):
    history = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": command},
    ]
    if with_metadata:
        history[-1][SESSION_CONTEXT_META_KEY] = {
            "kind": "user_input", "origin": "user:chat", "trust": "user",
        }
    original = deepcopy(history)
    captured = []
    request_texts = []
    state = SimpleNamespace(
        user_id="init-test", chat_id="init-stream-test", context=[],
        apply_request_context=lambda ctx, text: request_texts.append(text),
    )

    async def reply_stream(inputs=None):
        captured.append(inputs)
        if False:
            yield None

    agent = SimpleNamespace(state=state, model=None, reply_stream=reply_stream)
    stream = StreamingAgent(agent, mcp_clients=[])
    expanded = "初始化项目指令：必须调用 save_project_instructions 并读回核验。"
    events = [event async for event in stream.stream(
        history, {"project_init": True}, effective_user_message=expanded,
    )]
    assert not [event for event in events if event[0] == "error"]
    assert len(captured) == 1
    assert captured[0].get_text_content() == expanded
    assert request_texts == [expanded]
    assert history == original
    assert [msg.get_text_content() for msg in state.context] == [
        "Earlier question", "Earlier answer",
    ]

    # A later ordinary turn must not replay the expanded initialization request.
    captured.clear()
    state.context.clear()
    normal = original + [
        {"role": "assistant", "content": "Initialized"},
        {"role": "user", "content": "Next question"},
    ]
    _ = [event async for event in stream.stream(normal, {})]
    assert captured[0].get_text_content() == "Next question"
    assert expanded not in "\n".join(msg.get_text_content() for msg in state.context)
