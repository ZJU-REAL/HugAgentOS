"""User-visible compaction lifecycle and manual command routing."""

import asyncio

import pytest
from core.agent_runtime.integrations.compaction_stream import stream_with_compaction
from core.chat.compaction.progress import finish_progress, progress_scope, start_progress


@pytest.mark.asyncio
async def test_manual_command_bypasses_reply_model(monkeypatch):
    calls = []

    async def compact(chat_id, **kwargs):
        calls.append((chat_id, kwargs))
        event = await start_progress(chat_id, kwargs["run_id"], "manual")
        await finish_progress(event, "completed")
        return [{"role": "user", "content": "summary"}]

    monkeypatch.setattr('core.chat.compaction.service.run_compaction', compact)
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)

    async def model(**kwargs):
        raise AssertionError("manual command must not invoke reply model")
        yield {}

    chunks = [
        chunk
        async for chunk in stream_with_compaction(
            model,
            session_messages=[],
            user_message="decorated request",
            raw_user_message=" /compact ",
            context={"chat_id": "c", "run_id": "r"},
        )
    ]
    assert len(calls) == 1
    assert calls[0][1].get("token_limit") is None
    assert [c["status"] for c in chunks if c["type"] == "compaction_progress"] == [
        "running",
        "completed",
    ]
    assert chunks[-1]["type"] == "meta"
    assert "usage" not in chunks[-1]


@pytest.mark.asyncio
async def test_auto_progress_arrives_before_waiting_model_finishes(monkeypatch):
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    gate = asyncio.Event()

    async def model(**kwargs):
        event = await start_progress("c", "r", "mid_turn")
        await gate.wait()
        await finish_progress(event, "failed")
        yield {"type": "content", "delta": "answer"}

    stream = stream_with_compaction(
        model,
        session_messages=[],
        user_message="hi",
        raw_user_message="hi",
        context={"chat_id": "c", "run_id": "r"},
    )
    first = await anext(stream)
    assert first["status"] == "running"
    gate.set()
    rest = [c async for c in stream]
    assert rest[0]["status"] == "failed"
    assert rest[1]["delta"] == "answer"


@pytest.mark.asyncio
async def test_cancelled_compaction_has_terminal_state(monkeypatch):
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    events = []

    async def sink(event):
        events.append(event)

    with progress_scope(sink):
        event = await start_progress("c", "r", "mid_turn")
        await finish_progress(event, "interrupted")
    assert [event["status"] for event in events] == ["running", "interrupted"]
    assert events[-1]["ended_at"] >= events[0]["started_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "commit_failed", "cancelled"])
async def test_coordinator_reports_real_work_and_always_closes(monkeypatch, outcome):
    from types import SimpleNamespace

    from core.chat.compaction.coordinator import CompactionCoordinator

    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    history = [{"role": "user", "content": "old" * 100}, {"role": "assistant", "content": "answer"}]
    snapshot = SimpleNamespace(
        replacement_history=history,
        source_rows=[],
        covered_seq=2,
        source_hash="hash",
        base_checkpoint_version=0,
    )
    coordinator = CompactionCoordinator("c")
    monkeypatch.setattr(coordinator, "_acquire", lambda: snapshot)
    released = []
    monkeypatch.setattr(coordinator, "_release", lambda: released.append(True))

    def commit(*args, **kwargs):
        if outcome == "commit_failed":
            raise RuntimeError("CAS rejected")

    monkeypatch.setattr(coordinator, "_commit", commit)
    monkeypatch.setattr(
        'core.llm.context.compaction.split_history_for_compaction', lambda *args, **kwargs: (history, [])
    )
    gate = asyncio.Event()

    async def summarize(*args, **kwargs):
        gate.set()
        if outcome == "cancelled":
            await asyncio.Event().wait()
        return None if outcome == "failed" else "summary"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', summarize)
    events = []

    async def sink(event):
        events.append(event)

    with progress_scope(sink, "c", "real-run"):
        task = asyncio.create_task(coordinator.compact())
        await gate.wait()
        assert events[0]["status"] == "running"
        assert events[0]["run_id"] == "real-run"
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert bool(result) == (outcome == "completed")
    assert events[-1]["status"] == (
        "completed"
        if outcome == "completed"
        else "interrupted" if outcome == "cancelled" else "failed"
    )
    assert bool(released) == (outcome != "completed")


@pytest.mark.asyncio
async def test_short_manual_context_reports_noop_without_reply_model(monkeypatch):
    async def compact(*args, **kwargs):
        return None

    monkeypatch.setattr('core.chat.compaction.service.run_compaction', compact)
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    chunks = [
        c
        async for c in stream_with_compaction(
            None,
            session_messages=[],
            user_message="/compact",
            raw_user_message="/compact",
            context={"chat_id": "c", "run_id": "r"},
        )
    ]
    statuses = [c for c in chunks if c["type"] == "compaction_progress"]
    assert len(statuses) == 1 and statuses[0]["status"] == "skipped"
    assert statuses[0]["reason"] == "not_needed"


@pytest.mark.asyncio
async def test_preparation_failure_is_not_reported_as_noop(monkeypatch):
    from types import SimpleNamespace

    from core.chat.compaction.coordinator import CompactionCoordinator

    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    coordinator = CompactionCoordinator("c")
    snapshot = SimpleNamespace(
        replacement_history=[{"role": "user", "content": "old"}], source_rows=[]
    )
    monkeypatch.setattr(coordinator, "_acquire", lambda: snapshot)
    releases = []
    monkeypatch.setattr(coordinator, "_release", lambda: releases.append(True))

    def invalid_budget(**kwargs):
        raise RuntimeError("invalid budget")

    monkeypatch.setattr('core.llm.context.budget.estimate_context_budget', invalid_budget)
    events = []

    async def sink(event):
        events.append(event)

    with progress_scope(sink):
        assert await coordinator.compact() is None
    assert events[-1]["status"] == "failed"
    assert events[-1]["reason"] == "preparation"
    assert releases


@pytest.mark.asyncio
async def test_backpressured_consumer_close_finishes_producer(monkeypatch):
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    released = asyncio.Event()

    async def model(**kwargs):
        event = await start_progress("c", "r", "mid_turn")
        try:
            for i in range(300):
                yield {"type": "content", "delta": str(i)}
        finally:
            try:
                await finish_progress(event, "interrupted")
            finally:
                released.set()

    stream = stream_with_compaction(
        model,
        session_messages=[],
        user_message="hi",
        raw_user_message="hi",
        context={"chat_id": "c", "run_id": "r"},
    )
    assert (await anext(stream))["status"] == "running"
    await asyncio.sleep(0)
    await asyncio.wait_for(stream.aclose(), timeout=1)
    assert released.is_set()


def test_run_snapshot_preserves_compaction_order_and_tool_count():
    from core.chat.projection.reducer import fold_projection, new_projection

    state = new_projection("r")
    state = fold_projection(state, {"type": "tool_call", "tool_id": "t", "tool_name": "search"})
    running = {"type": "compaction_progress", "id": "p", "status": "running"}
    state = fold_projection(state, running)
    old = state
    state = fold_projection(state, {**running, "status": "completed"})
    assert len(state["tools"]) == 1
    assert [block["kind"] for block in state["blocks"]] == ["tool", "compaction"]
    assert state["blocks"][1]["progress"]["status"] == "completed"
    assert old["blocks"][1]["progress"]["status"] == "running"


def test_post_turn_progress_is_durable_on_message(db_session, monkeypatch):
    from contextlib import nullcontext

    from core.db.models import ChatMessage, ChatRun, ChatSession
    from core.chat.service import ChatService
    from core.chat.compaction.progress import persist_progress, read_progress

    db_session.add(
        ChatSession(chat_id="durable", user_id="u", title="t", extra_data={"other": "keep"})
    )
    db_session.add(
        ChatMessage(
            message_id="message",
            chat_id="durable",
            role="assistant",
            content="answer",
            chat_seq=1,
            extra_data={
                "other": "keep",
                "segments": [{"type": "tool", "index": 0}, {"type": "text", "text": "answer"}],
            },
        )
    )
    db_session.add(ChatRun(run_id="run", chat_id="durable", user_id="u", message_id="message"))
    db_session.commit()
    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: nullcontext(db_session))
    event = {
        "type": "compaction_progress",
        "id": "p",
        "chat_id": "durable",
        "run_id": "run",
        "phase": "post_turn",
        "status": "running",
        "started_at": 1,
        "expires_at": 2,
    }
    persist_progress(event)
    persist_progress({**event, "status": "completed", "ended_at": 3})
    persist_progress(event)  # A cancelled DB thread can deliver running late.
    message = db_session.query(ChatMessage).filter_by(message_id="message").one()
    assert message.extra_data["other"] == "keep"
    segments = message.extra_data["segments"]
    assert [s["type"] for s in segments] == ["tool", "compaction", "text"]
    assert segments[1]["progress"]["status"] == "completed"
    assert read_progress(ChatService(db_session), "durable")[0]["message_id"] == "message"


@pytest.mark.asyncio
async def test_steer_message_identity_tracks_context(monkeypatch):
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    events = []

    async def sink(event):
        events.append(event)

    context = {"message_id": "first"}
    with progress_scope(sink, "c", "r", context=context):
        event = await start_progress("c", "", "mid_turn")
        context["message_id"] = "second"
        await finish_progress(event, "completed")
    assert events[0]["message_id"] == "first"
    assert events[1]["message_id"] == "second"


@pytest.mark.asyncio
async def test_backpressured_close_with_throwing_cleanup_does_not_hang():
    async def model(**kwargs):
        try:
            for i in range(300):
                yield {"type": "content", "delta": str(i)}
        finally:
            raise RuntimeError("cleanup failed")

    stream = stream_with_compaction(
        model,
        session_messages=[],
        user_message="hi",
        raw_user_message="hi",
        context={"chat_id": "c", "run_id": "r"},
    )
    await anext(stream)
    await asyncio.sleep(0)
    await asyncio.wait_for(stream.aclose(), timeout=1)


@pytest.mark.asyncio
async def test_private_run_does_not_inherit_main_message_identity(monkeypatch):
    stored = []
    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', stored.append)

    async def sink(event):
        pass

    with progress_scope(sink, "c", "main", context={"message_id": "main-message"}):
        await start_progress("c", "private", "mid_turn")
    assert "message_id" not in stored[0]


@pytest.mark.asyncio
async def test_normalization_failure_releases_acquired_lease(monkeypatch):
    from types import SimpleNamespace

    from core.chat.compaction.coordinator import CompactionCoordinator

    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    coordinator = CompactionCoordinator("c")
    monkeypatch.setattr(
        coordinator, "_acquire", lambda: SimpleNamespace(replacement_history=[], source_rows=[None])
    )
    released = []
    monkeypatch.setattr(coordinator, "_release", lambda: released.append(True))

    def broken_rows(rows):
        raise ValueError("invalid source row")

    monkeypatch.setattr('core.chat.compaction.coordinator.normalize_rows', broken_rows)
    events = []

    async def sink(event):
        events.append(event)

    with progress_scope(sink):
        assert await coordinator.compact() is None
    assert released == [True]
    assert events[-1]["reason"] == "preparation"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["acquire", "commit"])
@pytest.mark.parametrize("fails", [False, True])
async def test_cancelled_database_work_settles_before_terminal_or_release(
    monkeypatch, stage, fails
):
    import threading
    from types import SimpleNamespace

    from core.llm.context import compaction as compaction
    from core.chat.compaction.coordinator import CompactionCoordinator

    monkeypatch.setattr('core.chat.compaction.progress.persist_progress', lambda event: None)
    history = [{"role": "user", "content": "old history"}]
    snapshot = SimpleNamespace(
        replacement_history=history,
        source_rows=[],
        covered_seq=1,
        source_hash="hash",
        base_checkpoint_version=0,
    )
    coordinator = CompactionCoordinator("c")
    entered = threading.Event()
    proceed = threading.Event()
    committed = []
    released = []

    def acquire():
        if stage == "acquire":
            entered.set()
            assert proceed.wait(3)
            if fails:
                raise RuntimeError("snapshot failed")
        return snapshot

    def commit(*args, **kwargs):
        if stage == "commit":
            entered.set()
            assert proceed.wait(3)
            if fails:
                raise RuntimeError("commit failed")
        committed.append(True)

    monkeypatch.setattr(coordinator, "_acquire", acquire)
    monkeypatch.setattr(coordinator, "_commit", commit)
    monkeypatch.setattr(coordinator, "_release", lambda: released.append(True))
    monkeypatch.setattr(compaction, "split_history_for_compaction", lambda *a, **k: (history, []))

    async def summarize(*args, **kwargs):
        return "summary"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', summarize)
    events = []

    async def sink(event):
        events.append(event)

    with progress_scope(sink):
        task = asyncio.create_task(coordinator.compact())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()  # Repeated cancellation must not abandon the DB thread.
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            proceed.set()
    if stage == "acquire":
        assert released == ([] if fails else [True])
        assert committed == []
        assert events == []
    else:
        assert committed == ([] if fails else [True])
        assert released == ([True] if fails else [])
        assert events[-1]["status"] == ("failed" if fails else "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("self_cancel", [False, True])
async def test_workflow_cancellation_reaches_consumer(self_cancel):
    async def model(**kwargs):
        if self_cancel:
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        raise asyncio.CancelledError
        yield {}

    stream = stream_with_compaction(
        model,
        session_messages=[],
        user_message="hi",
        raw_user_message="hi",
        context={"chat_id": "c", "run_id": "r"},
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(anext(stream), 1)
