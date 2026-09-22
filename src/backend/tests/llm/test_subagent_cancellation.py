"""Cancellation must stop the actual child before reporting a terminal state."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.llm import subagent_tool, _subagent_stream


class CapturingToolkit:
    def register_tool_function(self, function, **kwargs):
        self.call = function


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["factory", "reply"])
async def test_cancel_stops_child_and_finishes_log_once(monkeypatch, stage):
    from core.llm import agent_factory

    started, stopped = threading.Event(), threading.Event()
    events = []
    finish = AsyncMock()

    async def wait_for_cancel():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            stopped.set()

    class Child:
        state = SimpleNamespace(context=[])

        async def _reply(self, **kwargs):
            await wait_for_cancel()
            if False:
                yield None

    async def create(**kwargs):
        if stage == "factory":
            await wait_for_cancel()
        return Child(), []

    monkeypatch.setattr(agent_factory, "create_agent_executor", create)
    monkeypatch.setattr(
        subagent_tool.log_writer, "start_subagent_log", AsyncMock(return_value="test-log")
    )
    monkeypatch.setattr(subagent_tool.log_writer, "finish_subagent_log", finish)
    monkeypatch.setattr(_subagent_stream, "is_active", lambda chat: True)
    monkeypatch.setattr(_subagent_stream, "push", lambda chat, event: events.append(event))
    toolkit = CapturingToolkit()
    subagent_tool.register_subagent_tool(
        toolkit, [{"agent_id": "builtin.worker", "name": "worker"}], "u", chat_id="c"
    )
    task = asyncio.create_task(toolkit.call("builtin.worker", "wait"))
    async with asyncio.timeout(3):
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()  # repeated cancellation must not interrupt child cleanup
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    assert finish.await_count == 1
    assert finish.call_args.kwargs["status"] == "cancelled"
    terminal = [event for event in events if event["sub_type"] == "end"]
    assert len(terminal) == 1
    assert terminal[0]["ok"] is False


@pytest.mark.asyncio
async def test_cancel_queued_child_does_not_wait_for_a_free_worker(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    busy = pool.submit(release.wait)
    monkeypatch.setattr(subagent_tool, "_subagent_pool", pool)
    finish = AsyncMock()
    monkeypatch.setattr(subagent_tool.log_writer, "start_subagent_log", AsyncMock())
    monkeypatch.setattr(subagent_tool.log_writer, "finish_subagent_log", finish)
    toolkit = CapturingToolkit()
    subagent_tool.register_subagent_tool(toolkit, [{"agent_id": "builtin.worker"}], "u")
    task = asyncio.create_task(toolkit.call("builtin.worker", "never start"))
    try:
        await asyncio.sleep(0.05)
        task.cancel()
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not busy.done()
        assert finish.call_args.kwargs["status"] == "cancelled"
    finally:
        release.set()
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancel_during_log_creation_waits_then_finishes(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def start(record):
        started.set()
        await release.wait()
        return record["id"]

    finish = AsyncMock()
    monkeypatch.setattr(subagent_tool.log_writer, "start_subagent_log", start)
    monkeypatch.setattr(subagent_tool.log_writer, "finish_subagent_log", finish)
    toolkit = CapturingToolkit()
    subagent_tool.register_subagent_tool(toolkit, [{"agent_id": "builtin.worker"}], "u")
    task = asyncio.create_task(toolkit.call("builtin.worker", "never start"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finish.await_count == 1
    assert finish.call_args.kwargs["status"] == "cancelled"


@pytest.mark.asyncio
async def test_direct_conversation_cancel_during_log_creation(monkeypatch):
    from orchestration import workflow

    started, release = asyncio.Event(), asyncio.Event()

    async def start(record):
        started.set()
        await release.wait()
        return record["id"]

    finish = AsyncMock()
    monkeypatch.setattr(
        workflow, "_load_direct_user_agent", lambda *args: SimpleNamespace(name="test")
    )
    monkeypatch.setattr(subagent_tool.log_writer, "start_subagent_log", start)
    monkeypatch.setattr(subagent_tool.log_writer, "finish_subagent_log", finish)
    stream = workflow._astream_subagent_direct(
        agent_id="test", session_messages=[], user_message="wait", context={}
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finish.await_count == 1
    assert finish.call_args.kwargs["status"] == "cancelled"


@pytest.mark.asyncio
async def test_public_workflow_close_waits_for_direct_child(monkeypatch):
    from orchestration import workflow
    stopped = asyncio.Event()
    async def direct(**kwargs):
        try:
            yield {"type": "content", "delta": "first"}
        finally:
            await asyncio.sleep(0.01)
            stopped.set()
    monkeypatch.setattr(workflow, "_astream_subagent_direct", direct)
    stream = workflow.astream_chat_workflow(session_messages=[], user_message="test", context={"agent_id": "test"})
    assert (await anext(stream))["delta"] == "first"
    await stream.aclose()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_mcp_cleanup_survives_repeat_cancel_in_owning_task():
    from core.llm.mcp_manager import close_clients
    started, release = asyncio.Event(), asyncio.Event()
    closed = []
    owners = []
    class Client:
        def __init__(self, name): self.name = name
        async def close(self):
            owners.append(asyncio.current_task())
            if self.name == "first":
                started.set()
                await release.wait()
            closed.append(self.name)
    task = asyncio.create_task(close_clients([Client("first"), Client("second")]))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["first", "second"]
    assert all(owner is task for owner in owners)
