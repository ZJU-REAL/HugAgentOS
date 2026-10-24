"""The step record travels hook → stream → executor → row → replay unchanged.

The hook adapter records each completed model response, the streaming adapter
emits it (and every tool result as saved into context) as ``model_step``
events, the executor persists the list with the assistant row, and history
loading replays it. A run stopped mid-tool closes its dangling call.
"""

from __future__ import annotations

import asyncio
import contextlib

import core.agent_runtime.runner as runner
import core.chat.runtime.admission as admission
import core.runs.events.writer as writer
import core.runs.runtime as runtime
import core.runs.tasks as tasks
import core.chat.service as services_ChatService_module
import pytest
from core.db.models import ChatMessage
from core.llm.runtime.model_steps import assistant_row_replay
from core.chat.compaction.history import normalize_rows
from tests.orchestration.test_model_steps_pipeline import _result, _run, _step
from tests.orchestration.test_model_steps_pipeline import run_env as run_env


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
        rows = normalize_rows([db.get(ChatMessage, run.message_id)])
    assert [s["kind"] for s in stored.model_steps] == [
        "assistant",
        "tool_result",
        "assistant",
    ]
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

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
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
    await asyncio.wait_for(streamed.wait(), timeout=2)
    worker = tasks.process_state().active_runs.get(run.run_id)
    assert await runtime.cancel_run(run.run_id, user_id="user-1")
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(worker, timeout=3)

    with run_env() as db:
        stored = db.get(ChatMessage, run.message_id)
        rows = normalize_rows([stored])
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
    original_emit = writer.xadd_event
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

    monkeypatch.setattr(writer, 'xadd_event', emit)
    original_refresh = services_ChatService_module.ChatService.refresh_streaming_message
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

    monkeypatch.setattr(services_ChatService_module.ChatService, "refresh_streaming_message", refresh)
    reached = asyncio.Event()
    release = asyncio.Event()
    start = asyncio.Event()

    async def workflow(**kwargs):
        await start.wait()
        yield _step(
            {"type": "thinking", "thinking": "A"},
            {"type": "tool_call", "id": "c1", "name": "bash", "input": "{}"},
        )
        yield {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_id": "c1",
            "tool_args": {},
        }
        result = {"type": "tool_result", "tool_id": "c1", "result": "ACTUAL_SUCCESS"}
        if combined:
            result["model_step"] = _result("c1", "ACTUAL_SUCCESS")["step"]
        yield result
        if not combined:
            yield _result("c1", "ACTUAL_SUCCESS")

        reached.set()
        await release.wait()
        yield {"type": "meta"}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    run = await admission.start_run(
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
            ChatMessage(
                message_id=run.message_id,
                chat_id="chat-1",
                role="assistant",
                content="",
            )
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
            )

            assert [s["kind"] for s in row.model_steps] == ["assistant", "tool_result"]
            assert replay[-1]["content"][0]["output"] == "ACTUAL_SUCCESS"
            if combined:
                assert published_results
    finally:
        release.set()
        task = tasks.process_state().active_runs.get(run.run_id)
        if task is not None:
            await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True])
async def test_failed_tool_checkpoint_does_not_publish_success(run_env, monkeypatch, combined):
    start = asyncio.Event()
    original_refresh = services_ChatService_module.ChatService.refresh_streaming_message
    original_emit = writer.xadd_event
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
        yield {
            "type": "tool_call",
            "tool_id": "c1",
            "tool_name": "bash",
            "tool_args": {},
        }
        if not combined:
            yield _result("c1")
        yield {
            "type": "tool_result",
            "tool_id": "c1",
            "result": "ok",
            **({"model_step": _result("c1")["step"]} if combined else {}),
        }
        pytest.fail("must stop after an undurable completed tool result")

    monkeypatch.setattr(services_ChatService_module.ChatService, "refresh_streaming_message", refresh)
    monkeypatch.setattr(writer, 'xadd_event', emit)
    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    run = await admission.start_run(
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
            ChatMessage(
                message_id=run.message_id,
                chat_id="chat-1",
                role="assistant",
                content="",
            )
        )
        db.commit()
    task = tasks.process_state().active_runs[run.run_id]
    start.set()
    await asyncio.wait_for(task, 10)
    assert not any(e["type"] == "tool_result" for e in published)
    assert any(e["type"] == "error" for e in published)
