"""A user steer cancels a tool that is already running."""
import asyncio
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.tool._response import ToolChunk, ToolResponse

from core.chat import steer as chat_steer_service
from core.chat.steer_hook import SteeringHook
from core.chat.steer_queue import SteerQueue
from core.db.models import ChatSteerQueueItem, ToolEffectLedger
from core.llm.runtime.state import AgentRuntimeState
from core.llm.runtime.tool_interrupt import TOOL_INTERRUPTED_NOTICE, interruptible
from tests.orchestration.hook_support import durable_adapter, make_adapter
from tests.orchestration.tool_effect_support import effect_env  # noqa: F401


class _Pending:
    """A steer that becomes visible after ``after`` polls."""

    def __init__(self, after: int = 1):
        self.after = after
        self.polls = 0

    async def __call__(self, run_id: str) -> bool:
        assert run_id == "r1"
        self.polls += 1
        return self.polls >= self.after


def _hook(pending, **kwargs):
    hook = SteeringHook(has_pending=pending, **kwargs)
    hook.tool_interrupt_poll_seconds = 0.01
    return hook


def _call(name="slow_tool", call_id="call-1"):
    return {"tool_call": ToolCallBlock(id=call_id, name=name, input="{}")}


@pytest.mark.asyncio
async def test_steer_cancels_running_tool_and_marks_context_interrupted(monkeypatch):
    delivery = {"steer_id": "s1", "message": "直接用 oss 导出"}
    taken = []

    async def take_pending_steer(run_id: str):
        taken.append(run_id)
        return delivery

    monkeypatch.setattr(chat_steer_service, "take_pending_steer", take_pending_steer)
    pending = _Pending(after=2)
    hook = _hook(pending)
    adapter = make_adapter(*hook.specs())
    adapter.tool_interrupt = hook
    agent = SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def tool_handler(**_kwargs):
        started.set()
        yield ToolChunk(content=[], state=ToolResultState.RUNNING)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield ToolResponse(content=[], state=ToolResultState.SUCCESS)

    # The steer is not leased at admission: it only shows up after the tool started.
    monkeypatch.setattr(hook, "poll", _poll_none_then_real(hook))
    results = await asyncio.wait_for(
        _collect(adapter.on_acting(agent, _call(), tool_handler)), timeout=5
    )

    assert started.is_set() and cancelled.is_set()
    assert results[-1].state == ToolResultState.INTERRUPTED
    assert TOOL_INTERRUPTED_NOTICE in results[-1].content[0].text

    async def model_handler(**_kwargs):
        yield "model-started"

    assert [e async for e in adapter.on_reasoning(agent, {}, model_handler)] == ["model-started"]
    assert taken == ["r1"]
    text = agent.state.context[-1].content[0].text
    assert "直接用 oss 导出" in text
    assert "不要继续已经被中止的旧工具调用" in text


def _poll_none_then_real(hook):
    real = hook.poll
    calls = {"n": 0}

    async def poll(run_id):
        calls["n"] += 1
        if calls["n"] == 1:  # BEFORE_TOOL admission: nothing queued yet
            return None
        return await real(run_id)

    return poll


async def _collect(stream):
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_tool_that_finishes_first_is_not_interrupted():
    hook = _hook(_Pending(after=10_000))
    hook.poll = _none
    adapter = make_adapter(*hook.specs())
    adapter.tool_interrupt = hook
    agent = SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))

    async def tool_handler(**_kwargs):
        await asyncio.sleep(0.05)
        yield ToolChunk(content=[], state=ToolResultState.RUNNING)
        yield ToolResponse(content=[], state=ToolResultState.SUCCESS)

    results = await _collect(adapter.on_acting(agent, _call(), tool_handler))
    assert [r.state for r in results] == [ToolResultState.RUNNING, ToolResultState.SUCCESS]


async def _none(_run_id):
    return None


@pytest.mark.asyncio
async def test_policy_without_tool_interrupt_lets_running_tool_finish():
    hook = _hook(_Pending(after=1), interrupt_tools=False)
    adapter = make_adapter(*hook.specs())
    # Same wiring rule as the middleware stack: no interrupt when the policy opts out.
    adapter.tool_interrupt = hook
    agent = SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))

    async def tool_handler(**_kwargs):
        await asyncio.sleep(0.1)
        yield ToolResponse(content=[], state=ToolResultState.SUCCESS)

    results = await _collect(adapter.on_acting(agent, _call(), tool_handler))
    assert results[-1].state == ToolResultState.SUCCESS


@pytest.mark.asyncio
async def test_broken_waiter_never_fails_the_tool():
    async def broken():
        raise RuntimeError("db down")

    async def stream():
        await asyncio.sleep(0.02)
        yield "done"

    assert [i async for i in interruptible(stream, broken)] == ["done"]


@pytest.mark.asyncio
async def test_tool_failure_still_propagates_through_interruptible():
    async def never():
        await asyncio.sleep(30)

    async def stream():
        yield "chunk"
        raise ValueError("tool failed")

    seen = []
    with pytest.raises(ValueError):
        async for item in interruptible(stream, never):
            seen.append(item)
    assert seen == ["chunk"]


@pytest.mark.asyncio
async def test_durable_tool_interrupt_commits_a_terminal_failure(effect_env):  # noqa: F811
    sessions, make_run = effect_env
    make_run("r1")
    hook = _hook(_Pending(after=1))
    hook.poll = _none
    adapter = durable_adapter(sessions)
    for spec in hook.specs():
        adapter.bus.register(spec)
    adapter.tool_interrupt = hook
    state = AgentRuntimeState(run_id="r1", journal_owner="worker")
    cancelled = asyncio.Event()

    async def tool_handler(**_kwargs):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield ToolResponse(content=[], state=ToolResultState.SUCCESS)

    results = await asyncio.wait_for(
        _collect(adapter.on_acting(SimpleNamespace(state=state), _call("Bash"), tool_handler)),
        timeout=5,
    )
    assert cancelled.is_set()
    assert results[-1].state == ToolResultState.INTERRUPTED
    with sessions() as db:
        events = [
            e.event_type
            for e in db.query(ToolEffectLedger).order_by(ToolEffectLedger.event_id)
        ]
    assert events == ["intent", "failure"]


@pytest.mark.asyncio
async def test_durable_reconcile_tool_is_never_interrupted(effect_env):  # noqa: F811
    sessions, make_run = effect_env
    make_run("r1")
    hook = _hook(_Pending(after=1))
    hook.poll = _none
    adapter = durable_adapter(sessions)
    adapter.tool_interrupt = hook
    state = AgentRuntimeState(run_id="r1", journal_owner="worker")

    async def tool_handler(**_kwargs):
        await asyncio.sleep(0.1)
        yield ToolResponse(content=[], state=ToolResultState.SUCCESS)

    results = await _collect(
        adapter.on_acting(
            SimpleNamespace(state=state), _call("delete_scheduled_task"), tool_handler
        )
    )
    assert results[-1].state == ToolResultState.SUCCESS


def test_has_claimable_sees_only_waiting_steers(effect_env):  # noqa: F811
    sessions, make_run = effect_env
    make_run("r1")
    queue = SteerQueue(sessions)
    assert queue.has_claimable("r1") is False
    queue.accept(
        target_run_id="r1", chat_id="chat-1", user_id="user-1",
        steer_id="s1", message="换方案", delivery_mode="steer",
    )
    assert queue.has_claimable("r1") is True
    assert queue.claim_next("r1", owner="w") is not None
    assert queue.has_claimable("r1") is False
    queue.accept(
        target_run_id="r1", chat_id="chat-1", user_id="user-1",
        steer_id="s2", message="下一轮再说", delivery_mode="next_run",
    )
    assert queue.has_claimable("r1") is False
    with sessions() as db:
        assert db.query(ChatSteerQueueItem).count() == 2
