"""Tests for FinishPinGuardMiddleware (auto-pin on dumb-exit, no reminders).

AgentScope 2.0: the former ``make_finish_pin_guard_hook`` is now
``core.llm.middlewares.FinishPinGuardMiddleware``. When a reasoning turn has **no
tool call at all** (``next_handler`` emits no ``ToolCallEndEvent``, i.e. the model is
about to finish), the middleware calls finish_guard's ``_collect_unpinned`` +
``_direct_pin`` to auto-pin.
"""

import pytest
from agentscope.event import ToolCallEndEvent

from core.llm.middlewares import FinishPinGuardMiddleware


async def _drive(mw, *, events=()):
    """Drive one on_reasoning: next_handler emits the given events (empty = no tool call this turn)."""
    async def next_handler(**kwargs):
        for ev in events:
            yield ev

    async for _ in mw.on_reasoning(object(), {}, next_handler):
        pass


def _tool_call_event():
    return ToolCallEndEvent(reply_id="r1", tool_call_id="t1")


@pytest.fixture(autouse=True)
def reset_pin_state(monkeypatch):
    """Reset pin_hint ContextVar + stub workspace.pin/get_pinned/mark_active +
    stub core.artifacts.store.get_artifact. Returns the collected list of pin calls."""
    from core.llm.hooks import reset_pin_hint_state
    reset_pin_hint_state()
    import core.llm.workspace as ws
    monkeypatch.setattr(ws, "get_pinned_file_ids", lambda: [])
    pinned_calls: list[dict] = []
    monkeypatch.setattr(ws, "pin", lambda fid, **kw: pinned_calls.append({"file_id": fid, **kw}) or True)
    monkeypatch.setattr(ws, "mark_active", lambda: None)
    import core.artifacts.store as store
    monkeypatch.setattr(store, "get_artifact", lambda fid: {
        "name": f"{fid}.docx", "mime_type": "application/octet-stream", "size": 1234,
    })
    yield pinned_calls


@pytest.mark.asyncio
async def test_no_op_when_has_tool_call(reset_pin_state):
    """This turn has a tool call (ToolCallEndEvent) → no intervention, no auto-pin."""
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].add("abc123")  # an unpinned file present; would be pinned if wrongly triggered
    mw = FinishPinGuardMiddleware()
    await _drive(mw, events=[_tool_call_event()])
    assert reset_pin_state == []


@pytest.mark.asyncio
async def test_auto_pin_when_finish_with_unpinned(reset_pin_state):
    """No tool call + an unpinned file_id present → directly pin to workspace."""
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].update(["abc123", "def456"])
    mw = FinishPinGuardMiddleware()
    await _drive(mw, events=[])
    assert sorted(p["file_id"] for p in reset_pin_state) == ["abc123", "def456"]


@pytest.mark.asyncio
async def test_auto_pin_only_fires_once_per_agent(reset_pin_state):
    """The same middleware instance dumb-exits repeatedly, auto-pinning only once (_fired guards against an infinite loop)."""
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].add("abc123")
    mw = FinishPinGuardMiddleware()
    await _drive(mw, events=[])
    await _drive(mw, events=[])
    assert len(reset_pin_state) == 1


@pytest.mark.asyncio
async def test_failed_pin_does_not_disarm_guard(reset_pin_state, monkeypatch):
    """When _direct_pin fails entirely, _fired is not set — so it can retry on the next chance."""
    import core.llm.workspace as ws
    monkeypatch.setattr(ws, "pin", lambda fid, **kw: False)  # all pins fail
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].add("abc123")
    mw = FinishPinGuardMiddleware()
    await _drive(mw, events=[])
    # Retry after failure — now pin is changed to succeed
    succeeded: list[str] = []
    monkeypatch.setattr(ws, "pin", lambda fid, **kw: succeeded.append(fid) or True)
    await _drive(mw, events=[])
    assert succeeded == ["abc123"]  # second time actually pins (_fired was not set by the failure)


@pytest.mark.asyncio
async def test_already_pinned_no_op(monkeypatch, reset_pin_state):
    """All file_ids are already pinned → do not pin again."""
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].add("abc123")
    import core.llm.workspace as ws
    monkeypatch.setattr(ws, "get_pinned_file_ids", lambda: ["abc123"])
    mw = FinishPinGuardMiddleware()
    await _drive(mw, events=[])
    assert reset_pin_state == []


@pytest.mark.asyncio
async def test_missing_artifact_metadata_skipped(monkeypatch, reset_pin_state):
    """artifact metadata not found → skip that id, no crash, no pin."""
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].add("ghost123")
    import core.artifacts.store as store
    monkeypatch.setattr(store, "get_artifact", lambda fid: None)
    mw = FinishPinGuardMiddleware()
    await _drive(mw, events=[])
    assert reset_pin_state == []


@pytest.mark.asyncio
async def test_batch_mode_noop(reset_pin_state):
    """When batch_mode=True, never auto-pin."""
    from core.llm.hooks import _get_pin_hint_state
    _get_pin_hint_state()["seen"].add("abc123")
    mw = FinishPinGuardMiddleware(batch_mode=True)
    await _drive(mw, events=[])
    assert reset_pin_state == []
