"""Native snapshot + tail subscription. Transport EOF carries no task verdict."""
import asyncio
import contextlib
import json
import time

from core.llm import interaction_store
from orchestration import chat_run_executor
from orchestration.run_event_stream import START, _sort_key, get_run_event_stream


async def subscribe_run(run_id):
    stream = get_run_event_stream()

    async def snapshot_frame(state, run):
        # Confirmation decisions belong to the existing interaction store,
        # not the event log. Never resurrect a gate that has been decided.
        signals = dict(state["signals"])
        for key, signal in list(signals.items()):
            if signal.get("type") not in {"file_confirm", "design_pick"}:
                continue
            record = await interaction_store.read("confirm", run.chat_id, signal.get("confirm_id"))
            if (record is None or record.get("decision")
                    or record["expires_at"] <= time.time() or record["lease_until"] <= time.time()):
                signals.pop(key)
        state = {**state, "signals": signals}
        return {"type": "run_snapshot", "state": state, "event_offset": state["event_offset"]}

    async def frames():
        state, cursor = await stream.capture(run_id)
        run = await asyncio.to_thread(chat_run_executor.get_run, run_id)
        if run is None:
            raise RuntimeError("run no longer exists")
        if state is None and cursor == START and not run.last_event_offset:
            # Admission already owns an empty assistant row, before event 1.
            # This is the empty event prefix, not a reconstructed history row.
            from orchestration.run_projection import new_projection
            state = new_projection(run_id)
            state["message_id"] = run.message_id
            state["started_at"] = chat_run_executor._epoch_ms(run.started_at)
        if state is None or not state.get("message_id"):
            raise RuntimeError("run projection unavailable")
        if run.status in chat_run_executor._TERMINAL_STATUSES:
            state, cursor = await stream.capture(run_id)
            if state is None:
                raise RuntimeError("run projection expired")
            state["terminal"] = True
        yield await snapshot_frame(state, run)
        if state["terminal"]:
            return
        while True:
            batch = await stream.wait(run_id, after=cursor, limit=100, timeout_ms=5000)
            if not batch:
                run = await asyncio.to_thread(chat_run_executor.get_run, run_id)
                if run and run.status in chat_run_executor._TERMINAL_STATUSES:
                    state, _ = await stream.capture(run_id)
                    if state is not None:
                        state["terminal"] = True
                        yield await snapshot_frame(state, run)
                    return
            first = await stream.read(run_id, limit=1)
            if cursor != START and first and _sort_key(first[0][0]) > _sort_key(cursor):
                state, cursor = await stream.capture(run_id)
                if state is None:
                    raise RuntimeError("run projection expired")
                yield await snapshot_frame(state, run)
                if state["terminal"]:
                    return
                continue
            for cursor, event in batch:
                if event.get("type") == "__terminal__":
                    yield {"type": "run_terminal", "event_offset": event["event_offset"]}
                    return
                if event.get("type") != "model_progress":
                    yield {k: v for k, v in event.items() if not k.startswith("_")}

    queue = asyncio.Queue(maxsize=64)

    async def pump():
        try:
            async for event in frames():
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(exc)
        else:
            await queue.put(None)

    task = asyncio.create_task(pump(), name=f"run_subscription:{run_id}")
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if isinstance(event, Exception):
                raise event
            if event is None:
                await task
                return
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
