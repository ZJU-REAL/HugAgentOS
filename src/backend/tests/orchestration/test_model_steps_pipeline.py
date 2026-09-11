"""The step record travels hook → stream → executor → row → replay unchanged.

The hook adapter records each completed model response, the streaming adapter
emits it (and every tool result as saved into context) as ``model_step``
events, the executor persists the list with the assistant row, and history
loading replays it. A run stopped mid-tool closes its dangling call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from core.db.engine import Base
from core.db.models import ChatMessage, ChatSession
from core.harness.hooks import HookBus
from core.llm.agentscope_hook_adapter import AgentScopeHookAdapter
from core.llm.model_steps import assistant_row_replay
from core.services.compaction_service import _normalize_rows
from orchestration import chat_run_executor as executor
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
    from orchestration.streaming import StreamingAgent

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
                    type="tool_result", id="c1", name="search", output="hit", state="success"
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
        _fake("ToolResultEndEvent", tool_call_id="c1", tool_call_name="search", state="success"),
    ]

    out = await _stream_events(events, state)

    steps = [payload for kind, payload in out if kind == "model_step"]
    assert steps[0] == recorded
    assert len(steps) == 2
    result_step = steps[1]
    assert result_step["kind"] == "tool_result"
    assert result_step["blocks"] == [
        {"type": "tool_result", "id": "c1", "name": "search", "output": "hit", "state": "success"}
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
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    executor._active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    executor._active_runs.clear()
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

    monkeypatch.setattr(executor, "astream_chat_workflow", gated)
    run = await executor.start_run(
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
    with executor.SessionLocal() as db:
        db.add(
            ChatMessage(message_id=run.message_id, chat_id="chat-1", role="assistant", content="")
        )
        db.commit()
    start.set()
    worker = executor._active_runs.get(run.run_id)
    if worker is not None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=10)
    return run


@pytest.mark.asyncio
async def test_executor_persists_the_step_record_and_history_replays_it(run_env, monkeypatch):
    async def workflow(**_kwargs):
        yield {"type": "thinking", "delta": "先想"}
        yield _step(
            {"type": "thinking", "thinking": "先想"},
            {"type": "tool_call", "id": "call-0", "name": "bash", "input": "{}"},
        )
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-0",
            "tool_args": {"command": "ls"},
        }
        yield {"type": "tool_result", "tool_id": "call-0", "content": "ok"}
        yield _result("call-0")
        yield {"type": "ai_message", "delta": "答案"}
        yield _step({"type": "thinking", "thinking": "再想"}, {"type": "text", "text": "答案"})
        yield {"type": "meta"}

    run = await _run(workflow, monkeypatch)

    with run_env() as db:
        stored = db.get(ChatMessage, run.message_id)
        rows = _normalize_rows([db.get(ChatMessage, run.message_id)])
    assert [s["kind"] for s in stored.model_steps] == ["assistant", "tool_result", "assistant"]
    assert stored.content == "答案" and stored.thinking == [{"content": "先想"}]
    assert [r["role"] for r in rows] == ["assistant", "tool", "assistant"]
    assert rows[0]["content"][0]["thinking"] == "先想"
    assert rows[1]["content"][0]["id"] == "call-0"
    assert rows[2]["content"] == [
        {
            "type": "thinking",
            "thinking": "再想",
            "provider": "p",
            "model": "m",
            "protocol": "openai_chat",
        },
        {"type": "text", "text": "答案"},
    ]


@pytest.mark.asyncio
async def test_a_redrafted_answer_rewrites_only_the_last_step_text(run_env, monkeypatch):
    async def workflow(**_kwargs):
        yield _step({"type": "tool_call", "id": "call-0", "name": "bash", "input": "{}"})
        yield _result("call-0")
        yield {"type": "ai_message", "delta": "草稿"}
        yield _step({"type": "thinking", "thinking": "想"}, {"type": "text", "text": "草稿"})
        yield {"type": "content_replace", "content": "修订稿", "reason": "review"}
        yield {"type": "meta"}

    run = await _run(workflow, monkeypatch)

    with run_env() as db:
        stored = db.get(ChatMessage, run.message_id)
    assert stored.content == "修订稿"
    assert stored.model_steps[-1]["blocks"] == [
        {"type": "thinking", "thinking": "想"},
        {"type": "text", "text": "修订稿"},
    ]
    assert stored.model_steps[0]["blocks"][0]["type"] == "tool_call"


@pytest.mark.asyncio
async def test_cancelling_mid_tool_closes_the_dangling_call_in_the_record(run_env, monkeypatch):
    streamed = asyncio.Event()

    async def workflow(**_kwargs):
        yield _step(
            {"type": "thinking", "thinking": "想"},
            {"type": "tool_call", "id": "call-1", "name": "bash", "input": "{}"},
        )
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "call-1",
            "tool_args": {"command": "sleep 100"},
        }
        streamed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello",
        raw_user_message="hello",
        context={"user_id": "user-1"},
        request_payload={"message": "hello"},
        model_name="test-model",
    )
    await asyncio.wait_for(streamed.wait(), timeout=2)
    worker = executor._active_runs.get(run.run_id)
    assert await executor.cancel_run(run.run_id, user_id="user-1")
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(worker, timeout=3)

    with run_env() as db:
        stored = db.get(ChatMessage, run.message_id)
        rows = _normalize_rows([stored])
    assert [s["kind"] for s in stored.model_steps] == ["assistant", "tool_result"]
    assert stored.model_steps[1]["blocks"][0]["state"] == "interrupted"
    assert [r["role"] for r in rows] == ["assistant", "tool", "assistant"]
    assert rows[1]["content"][0]["id"] == "call-1"
    assert rows[-1]["content"] == [{"type": "text", "text": "[本轮回答被用户中断]"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [True, False])
@pytest.mark.parametrize("fail_once", [True, False])
async def test_tool_success_checkpoint_contains_canonical_result(
    run_env, monkeypatch, combined, fail_once
):
    original_emit = executor._xadd_event
    published_results = []

    async def emit(run_id, offset, event):
        if combined and event.get("type") == "tool_result":
            with run_env() as db:
                stored = db.get(ChatMessage, run.message_id)
                assert stored.model_steps[-1]["kind"] == "tool_result"
                assert stored.model_steps[-1]["blocks"][0]["output"] == "ACTUAL_SUCCESS"
            assert "model_step" not in event
            published_results.append(event)
        await original_emit(run_id, offset, event)

    monkeypatch.setattr(executor, "_xadd_event", emit)
    original_refresh = executor.ChatService.refresh_streaming_message
    failed = []

    def refresh(self, **kwargs):
        if (
            fail_once
            and not failed
            and any(step["kind"] == "tool_result" for step in kwargs.get("model_steps", []))
        ):
            failed.append(True)
            raise RuntimeError("injected checkpoint failure")
        return original_refresh(self, **kwargs)

    monkeypatch.setattr(executor.ChatService, "refresh_streaming_message", refresh)
    reached = asyncio.Event()
    release = asyncio.Event()
    start = asyncio.Event()

    async def workflow(**kwargs):
        await start.wait()
        yield _step(
            {"type": "thinking", "thinking": "A"},
            {"type": "tool_call", "id": "c1", "name": "bash", "input": "{}"},
        )
        yield {"type": "tool_call", "tool_name": "bash", "tool_id": "c1", "tool_args": {}}
        result = {"type": "tool_result", "tool_id": "c1", "result": "ACTUAL_SUCCESS"}
        if combined:
            result["model_step"] = _result("c1", "ACTUAL_SUCCESS")["step"]
        yield result
        if not combined:
            yield _result("c1", "ACTUAL_SUCCESS")

        reached.set()
        await release.wait()
        yield {"type": "meta"}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "q"}],
        effective_user_message="q",
        raw_user_message="q",
        context={"user_id": "user-1"},
        request_payload={"message": "q"},
        model_name="test-model",
    )
    with run_env() as db:
        db.add(
            ChatMessage(message_id=run.message_id, chat_id="chat-1", role="assistant", content="")
        )
        db.commit()
    start.set()
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await asyncio.sleep(2.5)
        with run_env() as db:
            row = db.get(ChatMessage, run.message_id)

            replay = assistant_row_replay(
                content=row.content,
                model_steps=row.model_steps,
                thinking=row.thinking,
                tool_calls=row.tool_calls,
                segments=None,
            )

            assert [s["kind"] for s in row.model_steps] == ["assistant", "tool_result"]
            assert replay[-1]["content"][0]["output"] == "ACTUAL_SUCCESS"
            if combined:
                assert published_results
    finally:
        release.set()
        task = executor._active_runs.get(run.run_id)
        if task is not None:
            await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True])
async def test_failed_tool_checkpoint_does_not_publish_success(run_env, monkeypatch, combined):
    start = asyncio.Event()
    original_refresh = executor.ChatService.refresh_streaming_message
    original_emit = executor._xadd_event
    published = []

    def refresh(self, **kwargs):
        if any(s["kind"] == "tool_result" for s in kwargs.get("model_steps", [])):
            raise RuntimeError("database unavailable")
        return original_refresh(self, **kwargs)

    async def emit(run_id, offset, event):
        published.append(event)
        await original_emit(run_id, offset, event)

    async def workflow(**kwargs):
        await start.wait()
        yield _step({"type": "tool_call", "id": "c1", "name": "bash", "input": "{}"})
        yield {"type": "tool_call", "tool_id": "c1", "tool_name": "bash", "tool_args": {}}
        if not combined:
            yield _result("c1")
        yield {
            "type": "tool_result",
            "tool_id": "c1",
            "result": "ok",
            **({"model_step": _result("c1")["step"]} if combined else {}),
        }
        pytest.fail("must stop after an undurable completed tool result")

    monkeypatch.setattr(executor.ChatService, "refresh_streaming_message", refresh)
    monkeypatch.setattr(executor, "_xadd_event", emit)
    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "q"}],
        effective_user_message="q",
        raw_user_message="q",
        context={"user_id": "user-1"},
        request_payload={"message": "q"},
        model_name="test-model",
    )
    with run_env() as db:
        db.add(
            ChatMessage(message_id=run.message_id, chat_id="chat-1", role="assistant", content="")
        )
        db.commit()
    task = executor._active_runs[run.run_id]
    start.set()
    await asyncio.wait_for(task, 10)
    assert not any(e["type"] == "tool_result" for e in published)
    assert any(e["type"] == "error" for e in published)


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_result", [True, False])
async def test_plan_bar_and_canonical_results_survive_workflow_and_persistence(
    run_env, monkeypatch, persist_result
):
    from core.db import engine as db_engine
    from core.llm import builtin_subagents
    from core.services import compaction_service, user_agent_service, user_service
    from orchestration import workflow

    class DummySession:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, *_args):
            return False

    class FakeStreamingAgent:
        def __init__(self, agent, _clients):
            self.agent = agent

        async def stream(self, _messages, _context):
            plan = {"title": "Plan", "steps": [{"title": "Read", "status": "in_progress"}]}
            result = ToolResultBlock(
                type="tool_result",
                id="plan-1",
                name="update_plan",
                output="Plan updated: 1 in progress",
                state="success",
            )
            state = SimpleNamespace(
                user_id="",
                chat_id="",
                run_id="",
                tool_effect_links={},
                apply_request_context=lambda ctx, text: None,
                context=[Msg(name="assistant", role="assistant", content=[result])],
                pending_model_steps=[
                    _step(
                        {"type": "tool_call", "id": "plan-1", "name": "update_plan", "input": "{}"}
                    )["step"]
                ],
            )
            events = [
                _fake("ModelCallEndEvent", input_tokens=1, output_tokens=1),
                _fake("ToolCallStartEvent", tool_call_id="plan-1", tool_call_name="update_plan"),
                _fake("ToolCallDeltaEvent", tool_call_id="plan-1", delta=json.dumps(plan)),
                _fake("ToolCallEndEvent", tool_call_id="plan-1"),
                _fake(
                    "ToolResultEndEvent",
                    tool_call_id="plan-1",
                    tool_call_name="update_plan",
                    state="success",
                ),
            ]
            for item in await _stream_events(events, state):
                yield item

        async def aget_usage(self):
            return {}

        def get_context_usage(self, _usage):
            return None

        async def shutdown(self):
            return None

    async def create_agent(**_kwargs):
        return (
            SimpleNamespace(
                model=SimpleNamespace(model="test-model", context_size=32_768),
                state=SimpleNamespace(ontology_runtime={}),
            ),
            [],
        )

    async def no_memory(*_args, **_kwargs):
        return None

    async def no_identity(_user_id):
        return ""

    async def no_compaction(_chat_id, messages, **_kwargs):
        return messages, None

    monkeypatch.setattr(db_engine, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(
        user_agent_service,
        "UserAgentService",
        lambda _db: SimpleNamespace(list_for_user=lambda _user_id: []),
    )
    monkeypatch.setattr(
        user_service,
        "UserService",
        lambda _db: SimpleNamespace(get_disabled_builtin_subagent_ids=lambda _user_id: set()),
    )
    monkeypatch.setattr(builtin_subagents, "merge_builtin_subagents", lambda *_a, **_kw: [])
    monkeypatch.setattr(compaction_service, "maybe_run_pre_turn_compaction", no_compaction)
    monkeypatch.setattr(workflow, "create_agent_executor", create_agent)
    monkeypatch.setattr(workflow, "launch_memory_retrieval", no_memory)
    monkeypatch.setattr(workflow, "build_user_identity_block", no_identity)
    monkeypatch.setattr(workflow, "anchor_start_for_chat", lambda _chat_id: 0)
    monkeypatch.setattr(workflow, "enabled_skill_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "enabled_mcp_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "enabled_kb_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "_resolve_mode_spec", lambda _ctx: None)
    monkeypatch.setattr(workflow, "StreamingAgent", FakeStreamingAgent)
    monkeypatch.setattr(workflow, "_persistent_clients", [])

    if not persist_result:
        original_refresh = executor.ChatService.refresh_streaming_message

        def fail_result(self, **kwargs):
            if any(s["kind"] == "tool_result" for s in kwargs.get("model_steps", [])):
                raise RuntimeError("injected result persistence failure")
            return original_refresh(self, **kwargs)

        monkeypatch.setattr(executor.ChatService, "refresh_streaming_message", fail_result)
    saved_plans = []
    monkeypatch.setattr(
        workflow, "_save_plan_progress", lambda chat, plan: saved_plans.append(plan)
    )
    published = []
    original_emit = executor._xadd_event

    async def emit(run_id, offset, event):
        published.append(event)
        await original_emit(run_id, offset, event)

    monkeypatch.setattr(executor, "_xadd_event", emit)
    run = await _run(workflow.astream_chat_workflow, monkeypatch)
    if not persist_result:
        assert not saved_plans
        assert not any(event["type"] == "plan_update" for event in published)
        assert any(event["type"] == "error" for event in published)
        return
    with run_env() as db:
        row = db.get(ChatMessage, run.message_id)
        replay = _normalize_rows([row])
        assert [step["kind"] for step in row.model_steps] == ["assistant", "tool_result"]
        assert row.model_steps[1]["blocks"][0]["output"] == "Plan updated: 1 in progress"
    assert [row["role"] for row in replay] == ["assistant", "tool"]
    assert saved_plans and saved_plans[0]["title"] == "Plan"
    assert any(event["type"] == "plan_update" for event in published)
    assert not any(
        event["type"] in ("tool_call", "tool_result") and event.get("tool_name") == "update_plan"
        for event in published
    )


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
            "ToolResultEndEvent", tool_call_id="plan", tool_call_name="update_plan", state="success"
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
