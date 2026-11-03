"""Batched steer delivery and module-profile runs on the core chat runtime.

Covers the opt-in parameters added for the personal agent Worker: profile
passthrough in ``prepare_main``, ``SteerQueue.claim_batch`` /
``take_pending_steers``, and ``SteeringHook(interrupt_tools=..., take_batch=...)``.
The defaults stay those of ordinary chat (see test_chat_steer / test_steer_queue).
"""
from tests.orchestration.hook_support import make_adapter

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import core.agent_runtime.runner as runner
import core.chat.runtime.compaction as compaction
import core.chat.runtime.followups as followups
import core.db.engine as db_engine
import core.runs.events.writer as writer
import core.runs.runtime as runtime
import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.tool._response import ToolChunk
from core.chat import steer as chat_steer_service
from core.chat.steer_hook import SteeringHook, render_steer
from core.chat.steer_queue import SteerQueue
from core.db import models
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession, ChatSteerQueueItem
from core.runs.journal import RunJournal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture()
def sessions(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'steer-batch.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(models.UserShadow(user_id="user-1", username="test"))
        db.flush()
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    monkeypatch.setattr(chat_steer_service, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _accept_run(sessions, clock=None):
    RunJournal(sessions, **({"clock": clock} if clock else {})).accept(
        run_id="r1",
        message_id="m1",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )


def _queue(sessions, clock, messages):
    _accept_run(sessions, clock)
    queue = SteerQueue(sessions, clock=clock)
    for index, message in enumerate(messages):
        queue.accept(target_run_id="r1", chat_id="chat-1", user_id="user-1",
                     steer_id=f"s{index}", message=message, replace_latest=False)
    return queue


# ── SteerQueue.claim_batch ──────────────────────────────────────────────


def test_claim_batch_leases_every_pending_steer_in_order(sessions):
    queue = _queue(sessions, Clock(), ["one", "two", "three"])

    claimed = queue.claim_batch("r1", owner="w")

    assert [r.steer_id for r in claimed] == ["s0", "s1", "s2"]
    assert {r.status for r in claimed} == {"claimed"}
    assert {r.claim_owner for r in claimed} == {"w"}
    assert queue.claim_batch("r1", owner="other") == []
    assert queue.claim_next("r1", owner="other") is None


def test_claim_batch_respects_item_and_char_limits(sessions):
    queue = _queue(sessions, Clock(), ["aaaa", "bbbb", "cccc", "dddd"])

    assert [r.steer_id for r in queue.claim_batch("r1", owner="w", max_items=2)] == ["s0", "s1"]
    # 4 + 4 > 6: the second item waits for the next boundary.
    assert [r.steer_id for r in queue.claim_batch("r1", owner="w", max_chars=6)] == ["s2"]
    assert [r.steer_id for r in queue.claim_batch("r1", owner="w")] == ["s3"]


def test_claim_batch_always_takes_an_oversized_first_item(sessions):
    queue = _queue(sessions, Clock(), ["x" * 50, "y"])

    assert [r.steer_id for r in queue.claim_batch("r1", owner="w", max_chars=10)] == ["s0"]


def test_claim_batch_reclaims_expired_leases(sessions):
    clock = Clock()
    queue = _queue(sessions, clock, ["one", "two"])
    assert len(queue.claim_batch("r1", owner="dead", lease_seconds=30)) == 2

    clock.advance(31)
    reclaimed = queue.claim_batch("r1", owner="alive")

    assert [r.steer_id for r in reclaimed] == ["s0", "s1"]
    assert [r.delivery_attempt for r in reclaimed] == [2, 2]


def test_claim_batch_skips_other_runs_and_modes(sessions):
    clock = Clock()
    queue = _queue(sessions, clock, ["steer"])
    queue.accept(target_run_id="r1", chat_id="chat-1", user_id="user-1",
                 steer_id="f", message="later", delivery_mode="follow_up")

    assert [r.steer_id for r in queue.claim_batch("r1", owner="w")] == ["s0"]
    with sessions() as db:
        assert db.get(ChatSteerQueueItem, queue.list_for_run("r1")[1].queue_id).status == "accepted"


def test_claim_batch_rejects_bad_arguments(sessions):
    queue = _queue(sessions, Clock(), [])
    with pytest.raises(ValueError):
        queue.claim_batch("r1", owner="")
    with pytest.raises(ValueError):
        queue.claim_batch("r1", owner="w", max_items=0)


def test_batch_claims_are_each_marked_applied(sessions):
    queue = _queue(sessions, Clock(), ["one", "two", "three"])
    claimed = queue.claim_batch("r1", owner="w")

    with sessions() as db:
        for record in claimed:
            SteerQueue.mark_applied_in_session(db, queue_id=record.queue_id, claim_owner="w",
                                               applied_run_id="r1", applied_operation_seq=1)
        db.commit()

    assert [r.status for r in queue.list_for_run("r1")] == ["applied"] * 3


# ── steer service ──────────────────────────────────────────────────────


async def _no_link(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_put_pending_steer_keeps_earlier_steers_without_replace_latest(sessions, monkeypatch):
    _accept_run(sessions)
    monkeypatch.setattr(chat_steer_service, "link_request", _no_link)
    monkeypatch.setattr(chat_steer_service, "_notify", _no_link)

    for index in range(3):
        await chat_steer_service.put_pending_steer("r1", {
            "chat_id": "chat-1", "user_id": "user-1", "steer_id": f"s{index}",
            "message": f"m{index}", "replace_latest": False})

    taken = await chat_steer_service.take_pending_steers("r1")
    assert [item["message"] for item in taken] == ["m0", "m1", "m2"]
    assert len({item["claim_owner"] for item in taken}) == 1
    assert await chat_steer_service.take_pending_steers("r1") == []


@pytest.mark.asyncio
async def test_put_pending_steer_default_still_supersedes(sessions, monkeypatch):
    _accept_run(sessions)
    monkeypatch.setattr(chat_steer_service, "link_request", _no_link)
    monkeypatch.setattr(chat_steer_service, "_notify", _no_link)

    for index in range(3):
        await chat_steer_service.put_pending_steer("r1", {
            "chat_id": "chat-1", "user_id": "user-1", "steer_id": f"s{index}", "message": f"m{index}"})

    assert [item["message"] for item in await chat_steer_service.take_pending_steers("r1")] == ["m2"]


# ── SteeringHook ───────────────────────────────────────────────────────


def _agent():
    return SimpleNamespace(state=SimpleNamespace(run_id="r1", context=[], steer_delivery=None))


async def _run_tool(middleware, agent):
    executed = []

    async def tool_handler(**_kwargs):
        executed.append(True)
        yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

    results = [item async for item in middleware.on_acting(
        agent, {"tool_call": ToolCallBlock(id="t", name="Bash", input="{}")}, tool_handler)]
    return bool(executed), results[-1].state


async def _reason(middleware, agent):
    async def model_handler(**_kwargs):
        yield "model-started"

    return [item async for item in middleware.on_reasoning(agent, {}, model_handler)]


def test_default_render_matches_single_steer_text():
    assert render_steer(["改查第二个方案"], interrupted=True) == (
        "[用户在当前执行中追加的新指令]\n改查第二个方案\n"
        "请立即按这条新指令调整后续计划；不要继续已经被中止的旧工具调用。")


@pytest.mark.asyncio
async def test_interrupt_tools_false_lets_issued_tool_run_then_injects():
    delivery = {"steer_id": "s1", "message": "顺便看下日志"}

    async def take(_run_id):
        return delivery

    middleware = make_adapter(*SteeringHook(take, interrupt_tools=False).specs())
    agent = _agent()

    assert await _run_tool(middleware, agent) == (True, ToolResultState.SUCCESS)
    assert await _reason(middleware, agent) == ["model-started"]
    assert agent.state.steer_delivery == delivery
    text = agent.state.context[-1].content[0].text
    assert "顺便看下日志" in text and "上一轮工具结果已经完成" in text


@pytest.mark.asyncio
async def test_take_batch_injects_all_pending_as_one_item_and_acknowledges_once():
    items = [{"steer_id": f"s{i}", "queue_id": f"q{i}", "claim_owner": "w", "message": f"消息{i}"}
             for i in range(3)]
    polls, acknowledged = [], []

    async def take(run_id):
        polls.append(run_id)
        return list(items) if len(polls) == 1 else []

    async def acknowledge(delivery):
        acknowledged.append(delivery)

    middleware = make_adapter(*SteeringHook(take, acknowledge, interrupt_tools=False,
                                            take_batch=True).specs())
    agent = _agent()
    before = len(agent.state.context)

    await _reason(middleware, agent)

    assert len(agent.state.context) == before + 1
    text = agent.state.context[-1].content[0].text
    assert text.index("消息0") < text.index("消息1") < text.index("消息2")
    delivery = agent.state.steer_delivery
    assert delivery["message"] == "消息0\n\n消息1\n\n消息2"
    assert [item["queue_id"] for item in delivery["batch"]] == ["q0", "q1", "q2"]
    assert len(acknowledged) == 1 and len(acknowledged[0]["batch"]) == 3

    # Nothing left: the next boundary is a pass-through.
    agent.state.steer_delivery = None
    await _reason(middleware, agent)
    assert agent.state.steer_delivery is None


@pytest.mark.asyncio
async def test_take_batch_uses_injected_render():
    async def take(_run_id):
        return [{"steer_id": "a", "message": "x"}, {"steer_id": "b", "message": "y"}]

    def render(messages, *, interrupted):
        return f"<inputs interrupted={interrupted}>" + "|".join(messages)

    middleware = make_adapter(*SteeringHook(take, take_batch=True, render=render).specs())
    agent = _agent()
    await _reason(middleware, agent)

    assert agent.state.context[-1].content[0].text == "<inputs interrupted=False>x|y"


@pytest.mark.asyncio
async def test_take_batch_with_tool_interrupt_still_rejects():
    async def take(_run_id):
        return [{"steer_id": "a", "message": "停"}]

    middleware = make_adapter(*SteeringHook(take, take_batch=True).specs())
    agent = _agent()

    assert await _run_tool(middleware, agent) == (False, ToolResultState.INTERRUPTED)
    await _reason(middleware, agent)
    assert "不要继续已经被中止的旧工具调用" in agent.state.context[-1].content[0].text


# ── prepare_main profile passthrough ───────────────────────────────────


class _Built(Exception):
    pass


async def _executor_kwargs(monkeypatch, context):
    import core.agent_runtime.prepare_main as prepare_main_module
    from core.agent_runtime.main_state import MainTurn

    captured = {}

    async def create_agent_executor(**kwargs):
        captured.update(kwargs)
        raise _Built()

    async def no_subagents(*_args, **_kwargs):
        return [], set()

    monkeypatch.setattr(prepare_main_module.factory, "create_agent_executor", create_agent_executor)
    monkeypatch.setattr(prepare_main_module, "run_in_session", no_subagents)
    turn = MainTurn(context=context, session_messages=[], user_message="hi")
    with pytest.raises(_Built):
        async for _event in prepare_main_module.prepare_main(turn):
            pass
    return captured


@pytest.mark.asyncio
async def test_prepare_main_passes_context_profile_to_executor(monkeypatch):
    kwargs = await _executor_kwargs(monkeypatch, {
        "user_id": "user-1", "chat_id": "chat-1", "profile": "personal_agent", "profile_ref": "task-7"})

    assert kwargs["profile"] == "personal_agent"
    assert kwargs["profile_ref"] == "task-7"


@pytest.mark.asyncio
async def test_prepare_main_ordinary_chat_has_no_profile(monkeypatch):
    kwargs = await _executor_kwargs(monkeypatch, {"user_id": "user-1", "chat_id": "chat-1"})

    assert kwargs["profile"] is None and kwargs["profile_ref"] is None


# ── background-launched personal run, three steers, one boundary ───────


@pytest.mark.asyncio
async def test_personal_profile_run_takes_three_steers_at_one_boundary(sessions, monkeypatch):
    from core.chat.runtime import admission
    from core.chat.sequencer import ChatSequencer

    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr(chat_steer_service, "link_request", _no_link)
    monkeypatch.setattr(chat_steer_service, "_notify", _no_link)

    async def no_event_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(writer, "xadd_event", no_event_write)
    monkeypatch.setattr(writer, "expire_stream", no_event_write)
    monkeypatch.setattr(followups, "_spawn_followup_task", lambda **_kwargs: None)
    monkeypatch.setattr(compaction, "_spawn_compaction_task", lambda **_kwargs: None)
    monkeypatch.setattr("core.files.artifacts.service.persist_artifacts",
                        lambda *_args, **_kwargs: None)

    seen = {}

    async def workflow(**kwargs):
        context = kwargs["context"]
        seen["profile"] = (context.get("profile"), context.get("profile_ref"))
        run_id = context["run_id"]
        # A Worker profile installs its own hook: issued tools are not
        # interrupted and every pending input enters at the next boundary.
        middleware = make_adapter(*SteeringHook(interrupt_tools=False, take_batch=True).specs())
        agent = SimpleNamespace(state=SimpleNamespace(run_id=run_id, context=[], steer_delivery=None))
        yield {"type": "content", "delta": "开始跑命令"}

        async def long_command(**_kwargs):
            # Three user messages arrive while the command is still running.
            for index in range(3):
                await chat_steer_service.put_pending_steer(run_id, {
                    "steer_id": f"pa-{index}", "message": f"追加{index}", "replace_latest": False})
            yield ToolChunk(content=[], state=ToolResultState.SUCCESS)

        results = [item async for item in middleware.on_acting(
            agent, {"tool_call": ToolCallBlock(id="bash", name="Bash", input="{}")}, long_command)]
        seen["tool_state"] = results[-1].state

        async def model(**_kwargs):
            yield "model"

        [item async for item in middleware.on_reasoning(agent, {}, model)]
        seen["injected"] = [item.content[0].text for item in agent.state.context]
        yield {"type": "steer_applied", **agent.state.steer_delivery}
        yield {"type": "content", "delta": "三条都看到了"}
        yield {"type": "meta", "is_markdown": False, "usage": {}}

    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)

    with sessions() as db:
        accepted = ChatSequencer(db).accept_main_run(
            chat_id="chat-1", user_id="user-1", user_content="跑 sleep 30; echo done",
            request_payload={"kind": "chat"},
            user_extra_data={"hidden_in_chat": True, "system_wake": "personal_task"})
        run = accepted.run
    context = {"chat_id": "chat-1", "user_id": "user-1", "chat_mode": "fast",
               "enable_thinking": False, "profile": "personal_agent", "profile_ref": "task-1"}
    started = await admission.start_run(
        accepted_run=run, chat_id="chat-1", user_id="user-1",
        session_messages=[{"role": "user", "content": "跑 sleep 30; echo done"}],
        effective_user_message="跑 sleep 30; echo done", raw_user_message="跑 sleep 30; echo done",
        context=context, model_name="test-model")
    await runtime.wait_run(started.run_id)

    assert seen["profile"] == ("personal_agent", "task-1")
    assert seen["tool_state"] == ToolResultState.SUCCESS
    assert len(seen["injected"]) == 1
    assert all(f"追加{index}" in seen["injected"][0] for index in range(3))
    with sessions() as db:
        rows = db.query(ChatSteerQueueItem).order_by(ChatSteerQueueItem.steer_seq).all()
        assert [(row.status, row.applied_run_id) for row in rows] == [("applied", run.run_id)] * 3
        assert len({row.applied_operation_seq for row in rows}) == 1
        finished = db.get(ChatRun, run.run_id)
        assert finished.status == "completed"
        snapshot_context = finished.recovery_snapshot["worker_args"]["context"]
        assert (snapshot_context["profile"], snapshot_context["profile_ref"]) == ("personal_agent", "task-1")
        steer_messages = [m for m in db.query(ChatMessage).filter(ChatMessage.chat_id == "chat-1").all()
                          if (m.extra_data or {}).get("steer")]
        assert [m.content for m in steer_messages] == ["追加0\n\n追加1\n\n追加2"]
        assert len(steer_messages[0].extra_data["steer_queue_ids"]) == 3


def test_batch_handoff_moves_every_waiting_steer_to_the_successor(sessions):
    """A batch-steered run (Personal Agent Worker) that ends with several steers waiting
    hands them to one successor together, in order, instead of one run per steer."""
    clock = Clock()
    queue = _queue(sessions, clock, ["u1", "u2", "u3"])
    with sessions() as db:
        first = SteerQueue.consume_next_handoff_in_session(db, source_run_id="r1", root_run_id="r1", chat_id="chat-1")
        moved = SteerQueue.retarget_waiting(db, chat_id="chat-1", from_runs={"r1"}, to_run=first.applied_run_id)
        db.commit()
    assert first.message == "u1" and moved == 2
    assert [r.message for r in queue.claim_batch(first.applied_run_id, owner="w")] == ["u2", "u3"]
    assert [r.message for r in queue.list_for_run("r1") if r.status == "accepted"] == []


def test_handoff_retargets_only_when_the_run_opts_in():
    import inspect
    from core.chat.runtime import handoff
    source = inspect.getsource(handoff)
    assert 'if context.get("steer_batch_handoff"):' in source and "retarget_waiting" in source
