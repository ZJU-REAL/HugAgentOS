"""The step record travels hook → stream → executor → row → replay unchanged.

The hook adapter records each completed model response, the streaming adapter
emits it (and every tool result as saved into context) as ``model_step``
events, the executor persists the list with the assistant row, and history
loading replays it. A run stopped mid-tool closes its dangling call.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import core.agent_runtime.runner as runner
import core.chat.runtime.admission as admission
import core.db.engine as db_engine
import core.runs.tasks as tasks
import fakeredis.aioredis
import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from core.db.engine import Base
from core.db.models import ChatMessage, ChatSession
from core.harness.hooks import HookBus
from core.llm.runtime.agentscope_hook_adapter import AgentScopeHookAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── hook adapter ─────────────────────────────────────────────────────────────


def _model():
    return SimpleNamespace(model="deepseek-v4", provider_id="deepseek", wire_protocol="openai_chat")


def _agent():
    return SimpleNamespace(state=SimpleNamespace(run_id="", pending_model_steps=[], context=[]))


@pytest.mark.asyncio
async def test_hook_adapter_records_the_completed_response_for_stream_and_non_stream():
    adapter = AgentScopeHookAdapter(HookBus())
    agent = _agent()
    blocks = [
        ThinkingBlock(type="thinking", thinking="先想"),
        ToolCallBlock(type="tool_call", id="c1", name="search", input="{}"),
    ]

    async def non_stream(**_kwargs):
        return ChatResponse(content=blocks, is_last=True)

    await adapter.on_model_call(agent, {"current_model": _model(), "messages": []}, non_stream)

    async def stream(**_kwargs):
        async def chunks():
            yield ChatResponse(
                content=[ThinkingBlock(type="thinking", thinking="先")], is_last=False
            )
            yield ChatResponse(content=[TextBlock(type="text", text="答")], is_last=True)

        return chunks()

    result = await adapter.on_model_call(agent, {"current_model": _model(), "messages": []}, stream)
    consumed = [item async for item in result]

    assert (blocks[0].provider, blocks[0].model, blocks[0].protocol) == (
        "deepseek",
        "deepseek-v4",
        "openai_chat",
    )
    assert len(consumed) == 2
    steps = agent.state.pending_model_steps
    assert [s["kind"] for s in steps] == ["assistant", "assistant"]
    assert steps[0]["blocks"] == [
        {"type": "thinking", "thinking": "先想"},
        {"type": "tool_call", "id": "c1", "name": "search", "input": "{}"},
    ]
    assert (steps[0]["provider"], steps[0]["model"], steps[0]["protocol"]) == (
        "deepseek",
        "deepseek-v4",
        "openai_chat",
    )
    # Only the last (complete) chunk of a stream is the step; partial chunks are not.
    assert steps[1]["blocks"] == [{"type": "text", "text": "答"}]


# ── streaming adapter ────────────────────────────────────────────────────────


def _fake(name: str, **fields):
    event = type(name, (), {})()
    for field, value in fields.items():
        setattr(event, field, value)
    return event


async def _stream_events(events, state):
    from core.llm.runtime.streaming import StreamingAgent

    async def reply_stream(inputs=None):  # noqa: ANN001, ARG001
        for event in events:
            yield event

    agent = SimpleNamespace(state=state, model=None, reply_stream=reply_stream)
    out = []
    async for item in StreamingAgent(agent, mcp_clients=[]).stream(
        session_messages=[], context={"enable_thinking": True}
    ):
        out.append(item)
    return out


@pytest.mark.asyncio
async def test_streaming_adapter_emits_model_steps_in_event_order():
    recorded = {
        "schema": "harness.steps.v1",
        "kind": "assistant",
        "provider": "p",
        "model": "m",
        "protocol": "openai_chat",
        "blocks": [{"type": "tool_call", "id": "c1", "name": "search", "input": "{}"}],
    }
    context = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolCallBlock(type="tool_call", id="c1", name="search", input="{}"),
                ToolResultBlock(
                    type="tool_result",
                    id="c1",
                    name="search",
                    output="hit",
                    state="success",
                ),
            ],
        )
    ]
    state = SimpleNamespace(
        user_id="u",
        chat_id="c",
        run_id="",
        apply_request_context=lambda ctx, text: None,
        context=context,
        pending_model_steps=[recorded],
        tool_effect_links={},
    )
    events = [
        _fake("ModelCallEndEvent", input_tokens=1, output_tokens=1),
        _fake(
            "ToolResultEndEvent",
            tool_call_id="c1",
            tool_call_name="search",
            state="success",
        ),
    ]

    out = await _stream_events(events, state)

    steps = [payload for kind, payload in out if kind == "model_step"]
    assert steps[0] == recorded
    assert len(steps) == 2
    result_step = steps[1]
    assert result_step["kind"] == "tool_result"
    assert result_step["blocks"] == [
        {
            "type": "tool_result",
            "id": "c1",
            "name": "search",
            "output": "hit",
            "state": "success",
        }
    ]
    kinds = [kind for kind, _ in out]
    assert kinds.index("tool_result") > max(
        i for i, kind in enumerate(kinds) if kind == "model_step"
    )
    assert "model_step" not in next(payload for kind, payload in out if kind == "tool_result")
    assert state.pending_model_steps == []


# ── executor persistence + replay ────────────────────────────────────────────


@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'executor-steps.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    tasks.process_state().active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    tasks.process_state().active_runs.clear()
    engine.dispose()


def _step(*blocks):
    return {
        "type": "model_step",
        "step": {
            "schema": "harness.steps.v1",
            "kind": "assistant",
            "provider": "p",
            "model": "m",
            "protocol": "openai_chat",
            "blocks": list(blocks),
        },
    }


def _result(call_id, output="ok"):
    return {
        "type": "model_step",
        "step": {
            "schema": "harness.steps.v1",
            "kind": "tool_result",
            "blocks": [
                {
                    "type": "tool_result",
                    "id": call_id,
                    "name": "bash",
                    "output": output,
                    "state": "success",
                }
            ],
        },
    }


async def _run(workflow, monkeypatch):
    start = asyncio.Event()

    async def gated(**kwargs):
        await start.wait()
        async for event in workflow(**kwargs):
            yield event

    monkeypatch.setattr(runner, "astream_chat_workflow", gated)
    run = await admission.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        request_payload={"message": "hello"},
        model_name="test-model",
    )
    # The chat route reserves this row before the worker starts.
    with db_engine.SessionLocal() as db:
        db.add(
            ChatMessage(
                message_id=run.message_id,
                chat_id="chat-1",
                role="assistant",
                content="",
            )
        )
        db.commit()
    start.set()
    worker = tasks.process_state().active_runs.get(run.run_id)
    if worker is not None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=10)
    return run


@pytest.mark.asyncio
async def test_result_is_snapshotted_before_queued_events_outlive_context():
    state = SimpleNamespace(
        user_id="",
        chat_id="",
        run_id="",
        tool_effect_links={},
        pending_model_steps=[],
        apply_request_context=lambda ctx, text: None,
        context=[
            Msg(
                name="assistant",
                role="assistant",
                content=[
                    ToolResultBlock(
                        type="tool_result",
                        id="plan",
                        name="update_plan",
                        output="actual result",
                        state="success",
                    )
                ],
            )
        ],
    )

    def events():
        yield _fake(
            "ToolResultEndEvent",
            tool_call_id="plan",
            tool_call_name="update_plan",
            state="success",
        )
        # The producer advances without waiting for persistence/UI consumers.
        state.context[:] = [
            Msg(name="user", role="user", content=[TextBlock(type="text", text="steer")])
        ]
        yield _fake("TextBlockDeltaEvent", delta="next")

    out = await _stream_events(events(), state)
    assert not any(kind == "error" for kind, _ in out)
    steps = [payload for kind, payload in out if kind == "model_step"]
    assert steps[0]["blocks"][0]["output"] == "actual result"
