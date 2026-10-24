"""Finish decisions pin artifacts only at the completed-reply boundary."""

import pytest
from agentscope.event import ToolCallEndEvent

from core.llm.context.workspace_hooks import WorkspacePinHook
from core.harness.hooks import HookSpec, HookStage
from core.llm.runtime.context_hook_adapter import finish_context_hooks
from tests.orchestration.hook_support import make_adapter
from types import SimpleNamespace


def finish_adapter(ids):
    policy = WorkspacePinHook()
    policy.seen.update(ids)
    return make_adapter(HookSpec("finish-pin", HookStage.BEFORE_FINISH, policy.finish,
                                mutable_fields=frozenset({"pin_file_ids"})))


async def _drive(adapter, *, events=()):
    async def next_handler(**kwargs):
        for event in events:
            yield event
    agent = SimpleNamespace(state=SimpleNamespace(run_id="finish-test", context=[]))
    if events:
        # A reasoning step that requests a tool is not a finish boundary.
        async for _ in adapter.on_reasoning(agent, {}, next_handler):
            pass
    else:
        await finish_context_hooks(adapter, agent, None, lambda value: value)


def _tool_call_event():
    return ToolCallEndEvent(reply_id="r1", tool_call_id="t1")


@pytest.fixture(autouse=True)
def reset_pin_state(monkeypatch):
    """Reset pin_hint ContextVar + stub workspace.pin/get_pinned/mark_active +
    stub core.files.artifacts.store.get_artifact. Returns the collected list of pin calls."""
    from core.llm.runtime.turn_state import reset_pin_hint_state
    reset_pin_hint_state()
    import core.llm.runtime.workspace as ws
    monkeypatch.setattr(ws, "get_pinned_file_ids", lambda: [])
    pinned_calls: list[dict] = []
    monkeypatch.setattr(ws, "pin", lambda fid, **kw: pinned_calls.append({"file_id": fid, **kw}) or True)
    monkeypatch.setattr(ws, "mark_active", lambda: None)
    import core.files.artifacts.store as store
    monkeypatch.setattr(store, "get_artifact", lambda fid: {
        "name": f"{fid}.docx", "mime_type": "application/octet-stream", "size": 1234,
    })
    yield pinned_calls


@pytest.mark.asyncio
async def test_no_op_when_has_tool_call(reset_pin_state):
    """This turn has a tool call (ToolCallEndEvent) → no intervention, no auto-pin."""
    mw = finish_adapter(["abc123"])
    await _drive(mw, events=[_tool_call_event()])
    assert reset_pin_state == []


@pytest.mark.asyncio
async def test_auto_pin_when_finish_with_unpinned(reset_pin_state):
    """No tool call + an unpinned file_id present → directly pin to workspace."""
    mw = finish_adapter(["abc123", "def456"])
    await _drive(mw, events=[])
    assert sorted(p["file_id"] for p in reset_pin_state) == ["abc123", "def456"]


@pytest.mark.asyncio
async def test_auto_pin_only_fires_once_per_agent(reset_pin_state):
    """The same middleware instance dumb-exits repeatedly, auto-pinning only once (_fired guards against an infinite loop)."""
    mw = finish_adapter(["abc123"])
    await _drive(mw, events=[])
    await _drive(mw, events=[])
    assert len(reset_pin_state) == 1


@pytest.mark.asyncio
async def test_failed_pin_does_not_disarm_guard(reset_pin_state, monkeypatch):
    """When pinning fails entirely, completion is not recorded — so it can retry on the next chance."""
    import core.llm.runtime.workspace as ws
    monkeypatch.setattr(ws, "pin", lambda fid, **kw: False)  # all pins fail
    mw = finish_adapter(["abc123"])
    await _drive(mw, events=[])
    # Retry after failure — now pin is changed to succeed
    succeeded: list[str] = []
    monkeypatch.setattr(ws, "pin", lambda fid, **kw: succeeded.append(fid) or True)
    await _drive(mw, events=[])
    assert succeeded == ["abc123"]  # second time actually pins (_fired was not set by the failure)


@pytest.mark.asyncio
async def test_already_pinned_no_op(monkeypatch, reset_pin_state):
    """All file_ids are already pinned → do not pin again."""
    import core.llm.runtime.workspace as ws
    monkeypatch.setattr(ws, "get_pinned_file_ids", lambda: ["abc123"])
    mw = finish_adapter(["abc123"])
    await _drive(mw, events=[])
    assert reset_pin_state == []


@pytest.mark.asyncio
async def test_missing_artifact_metadata_skipped(monkeypatch, reset_pin_state):
    """artifact metadata not found → skip that id, no crash, no pin."""
    import core.files.artifacts.store as store
    monkeypatch.setattr(store, "get_artifact", lambda fid: None)
    mw = finish_adapter(["abc123"])
    await _drive(mw, events=[])
    assert reset_pin_state == []
