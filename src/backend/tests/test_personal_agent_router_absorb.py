"""Mid-turn absorption: input that arrives while a turn runs joins it, and the reply continues in a new segment
after that input (ROUTER_DESIGN §4.2, §5, §10)."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.db.models import ChatMessage
from core.harness.hooks import DecisionKind
from personal_agent.context import AgentContext
from personal_agent.conversation.router.triggers import enqueue
from personal_agent.models import PersonalFrontTurn, PersonalMessageInbox
from tests.test_personal_agent import personal_agent_on  # noqa: F401  (module on, user permitted)
from tests.test_personal_agent_router_batching import assistant_rows, counting, run, say, statuses, unattended_result
from tests.test_personal_agent_routing import claim, expire, prepared, sessions  # noqa: F401


def arrive(factory, *texts, **options):
    """A step: the user writes while the turn runs."""
    async def step(plan):
        plan.setdefault("arrived", []).extend(say(factory, *texts, **options))
    return step


def boundary():
    """A step: the next model call. The front steering hook injects what arrived, as in the real
    stream (TRANSFORM_CONTEXT, then AFTER_CONTEXT once appended), then the call starts."""
    async def step(plan):
        hook = plan.setdefault("hook", plan["turn"].steering.hook())
        decision = await hook.transform(SimpleNamespace(operation_name="agent_reasoning", run_id="run", data={}))
        if decision.kind is DecisionKind.MODIFY:
            plan.setdefault("injected", []).append(decision.patch["context_items"][-1]["content"])
            await hook.applied(SimpleNamespace(data={"steer_delivery": decision.patch["steer_delivery"]}))
        plan["reply"].take("model_call_start", None)
    return step


def seen_only():
    """A step: the hook takes the new input, but the model call never gets it into its context."""
    async def step(plan):
        hook = plan.setdefault("hook", plan["turn"].steering.hook())
        await hook.transform(SimpleNamespace(operation_name="agent_reasoning", run_id="run", data={}))
    return step


def triggers_of(factory, turn_id):
    with factory() as db:
        return [row.message_id for row in db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.turn_id == turn_id))]


def seq(factory, ident):
    with factory() as db: return db.get(ChatMessage, ident).chat_seq


def system_row(factory, ident, kind, event, content, chat_id=None):
    from core.db.repository.chat import ChatMessageRepository
    with factory() as db:
        agent = AgentContext(db, "owner").identity.agent()
        row = ChatMessageRepository(db).create({"message_id": ident, "chat_id": chat_id or agent.chat_id, "role": "system",
            "content": content, "extra_data": {"event": event, "slot": 1}}, commit=False)
        enqueue(db, agent, kind=kind, message=row, request_key=kind + ":" + ident, payload={"kind": event, "slot": 1})
        db.commit()


# ---------------------------------------------------------------- joining and segments

@pytest.mark.asyncio
async def test_messages_arriving_mid_turn_join_it_and_the_reply_continues_after_them(sessions):
    [first] = say(sessions, "调研笔记软件")
    turn_id, _ = claim()
    execute, calls = counting("我先开个任务。", ("pa_task_create", {"goal": "调研笔记软件", "title": "调研"}),
        arrive(sessions, "顺便比较价格", "要中文的"), boundary(), "价格和中文支持也一起看。")
    await run(turn_id, execute)
    plan = calls[0]
    second, third = plan["arrived"]
    assert plan["results"][0]["started"]
    # One injection, numbered after the turn's own input.
    [injected] = plan["injected"]
    assert "[#2 " in injected and "顺便比较价格" in injected and "[#3 " in injected and "要中文的" in injected
    assert [t.message_id for t in plan["turn"].triggers] == [first, second, third]
    assert statuses(sessions, [first, second, third]) == ["done"] * 3 and sorted(triggers_of(sessions, turn_id)) == sorted([first, second, third])
    early, final = assistant_rows(sessions)
    assert early.content == "我先开个任务。" and early.extra_data["front_intermediate"] and not early.extra_data["streaming"]
    assert early.extra_data["reply_to"] == first and early.tool_calls[0]["tool_name"] == "pa_task_create"
    assert final.content == "价格和中文支持也一起看。" and final.extra_data["reply_to"] == third and not final.extra_data["streaming"]
    # The conversation reads A → reply → B, C → reply.
    assert seq(sessions, first) < early.chat_seq < seq(sessions, second) < seq(sessions, third) < final.chat_seq
    with sessions() as db: assert db.get(PersonalFrontTurn, turn_id).reply_ids == [early.message_id, final.message_id]
    assert claim("next") == (None, [])


@pytest.mark.asyncio
async def test_input_absorbed_before_anything_was_written_leaves_one_reply_after_it(sessions):
    [first] = say(sessions, "在吗")
    turn_id, _ = claim()
    execute, calls = counting(arrive(sessions, "帮我查天气"), boundary(), "在，北京今天晴。")
    await run(turn_id, execute)
    [second] = calls[0]["arrived"]
    [reply] = assistant_rows(sessions)
    assert reply.content == "在，北京今天晴。" and reply.extra_data["reply_to"] == second
    assert reply.chat_seq > seq(sessions, second)
    assert statuses(sessions, [first, second]) == ["done", "done"]


@pytest.mark.asyncio
async def test_a_tool_result_still_in_flight_stays_with_its_call(sessions):
    [first] = say(sessions, "开始")
    turn_id, _ = claim()

    async def late_result(plan):
        # The previous call's tool result reaches the stream after the new input was injected.
        plan["reply"].take("tool_call", {"id": "t1", "name": "pa_task_list"})
        await arrive(sessions, "再加一条")(plan)
        hook = plan["turn"].steering.hook()
        decision = await hook.transform(SimpleNamespace(operation_name="agent_reasoning", run_id="run", data={}))
        await hook.applied(SimpleNamespace(data={"steer_delivery": decision.patch["steer_delivery"]}))
        plan["reply"].take("model_call_start", None)
        plan["reply"].take("tool_result", {"id": "t1", "status": "success", "content": "[]"})
    await run(turn_id, counting("好的。", late_result, "两条都处理了。")[0])
    early, final = assistant_rows(sessions)
    assert early.tool_calls[0]["status"] == "success" and final.tool_calls == []


def test_heartbeats_and_other_conversations_are_not_absorbed(sessions):
    from tests.test_personal_agent_channel_sessions import conversation
    from personal_agent.scheduling import heartbeat
    [first] = say(sessions, "第一")
    turn_id, _ = claim()
    with sessions() as db: bob = conversation(db, "bob")
    system_row(sessions, "pa_hb_1", "heartbeat", heartbeat.EVENT, "[Heartbeat] now")
    [elsewhere] = say(sessions, "bob 的消息", chat_id=bob)
    [second] = say(sessions, "第二")
    plan = prepared(turn_id, "worker")
    with sessions() as db: taken = plan["turn"].steering.take_in(db)
    assert [item["message_id"] for item in taken] == [second]
    assert statuses(sessions, ["pa_hb_1", elsewhere, second]) == ["pending"] * 3


def test_a_hatch_stops_absorption_and_a_heartbeat_turn_takes_nothing_in(sessions):
    from personal_agent.conversation.router.kinds import BOOTSTRAP_EVENT
    from personal_agent.scheduling import heartbeat
    [first] = say(sessions, "第一")
    turn_id, _ = claim()
    system_row(sessions, "pa_boot_1", "bootstrap", BOOTSTRAP_EVENT, "醒醒")
    say(sessions, "第二")
    plan = prepared(turn_id, "worker")
    with sessions() as db: assert plan["turn"].steering.take_in(db) == []
    expire(sessions, turn_id, status="done")
    with sessions() as db:
        for row in db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")): row.status = "done"
        db.commit()
    system_row(sessions, "pa_hb_2", "heartbeat", heartbeat.EVENT, "[Heartbeat] now")
    turn_id, claimed = claim()
    assert claimed == ["pa_hb_2"]
    say(sessions, "第三")
    plan = prepared(turn_id, "worker")
    with sessions() as db: assert plan["turn"].steering.take_in(db) == []


@pytest.mark.asyncio
async def test_absorption_stops_at_its_limits_and_the_rest_waits(sessions, monkeypatch):
    from personal_agent.conversation.router import scheduler, steering
    monkeypatch.setattr(steering, "MAX_ABSORBS", 1)
    monkeypatch.setattr(scheduler, "MAX_BATCH", 2)
    [first] = say(sessions, "开始")
    turn_id, _ = claim()
    execute, calls = counting(arrive(sessions, "a", "b", "c"), boundary(), arrive(sessions, "d"), boundary(), "好。")
    await run(turn_id, execute)
    a, b, c, d = calls[0]["arrived"]
    assert statuses(sessions, [first, a, b, c, d]) == ["done", "done", "done", "pending", "pending"]
    assert claim("next")[1] == [c, d]


# ---------------------------------------------------------------- what an absorbed Trigger changes

@pytest.mark.asyncio
async def test_issued_tool_calls_are_not_interrupted_by_new_input(sessions):
    [first] = say(sessions, "调研笔记软件")
    turn_id, _ = claim()

    async def pending_input_does_not_reject(plan):
        hook = plan.setdefault("hook", plan["turn"].steering.hook())
        decision = await hook.before_tool(SimpleNamespace(run_id="run"))
        plan["before_tool"] = decision.kind
    execute, calls = counting(arrive(sessions, "顺便比较价格"), pending_input_does_not_reject,
        ("pa_task_create", {"goal": "调研笔记软件", "title": "调研"}), boundary(), "开始了。")
    await run(turn_id, execute)
    assert calls[0]["before_tool"] is DecisionKind.CONTINUE and calls[0]["results"][0]["started"]
    assert statuses(sessions, [first, *calls[0]["arrived"]]) == ["done", "done"]


@pytest.mark.asyncio
async def test_an_absorbed_user_message_forbids_quiet_and_allows_task_changes(sessions):
    task, event = unattended_result(sessions, "价格仍为 120")
    turn_id, claimed = claim()
    assert claimed == [event]
    execute, calls = counting(("pa_stay_quiet", {"reason": "无变化"}), arrive(sessions, "帮我订机票"), boundary(),
        ("pa_stay_quiet", {"reason": "无"}), ("pa_task_create", {"goal": "订机票", "title": "机票"}), "价格没变；机票我去订。")
    await run(turn_id, execute)
    quiet, refused, started = calls[0]["results"]
    assert quiet["quiet"] and refused == {"error": "PermissionError"} and started["started"]
    early, final = assistant_rows(sessions)
    # The quiet ending chosen before the message arrived no longer holds: the reply is kept and delivered.
    assert final.content == "价格没变；机票我去订。" and final.extra_data["reply_to"] == calls[0]["arrived"][0]
    with sessions() as db:
        assert not db.get(PersonalMessageInbox, event).decision["quiet"]


# ---------------------------------------------------------------- interruption and failure

@pytest.mark.asyncio
async def test_an_absorbed_trigger_of_a_dead_turn_is_answered_once_by_the_takeover(sessions):
    [first] = say(sessions, "调研笔记软件")
    turn_id, _ = claim("crashed")

    async def crash(plan):
        expire(sessions, turn_id)
        raise RuntimeError("worker died")
    execute, calls = counting("我先看看。", arrive(sessions, "顺便比较价格"), boundary(), "价格……", crash)
    await run(turn_id, execute, owner="crashed")
    [second] = calls[0]["arrived"]
    assert statuses(sessions, [first, second]) == ["claimed", "claimed"]
    assert claim("new") == (turn_id, [first, second])
    plan = prepared(turn_id, "new")
    content = plan["history"][-1]["content"]
    assert "[#1 " in content and "[#2 " in content and content.index("调研笔记软件") < content.index("顺便比较价格")
    await run(turn_id, counting("笔记软件和价格都对比好了。")[0], owner="new")
    assert statuses(sessions, [first, second]) == ["done", "done"]
    # The earlier attempt's segments are gone; one reply answers both, after both.
    [reply] = assistant_rows(sessions)
    assert reply.content == "笔记软件和价格都对比好了。" and reply.extra_data["reply_to"] == second
    assert reply.chat_seq > seq(sessions, second)
    assert claim("later") == (None, [])


@pytest.mark.asyncio
async def test_input_the_model_never_read_goes_to_the_next_turn(sessions):
    [first] = say(sessions, "第一")
    turn_id, _ = claim()
    execute, calls = counting(arrive(sessions, "第二"), seen_only(), "好。")
    await run(turn_id, execute)
    [second] = calls[0]["arrived"]
    assert statuses(sessions, [first, second]) == ["done", "pending"]
    assert claim("next")[1] == [second]


@pytest.mark.asyncio
async def test_a_failed_turn_fails_its_absorbed_triggers_and_reprocesses_them_together(sessions):
    from personal_agent.conversation.attention import reprocess
    [first] = say(sessions, "一")
    turn_id, _ = claim()

    async def broken(plan): raise RuntimeError("model down")
    execute, calls = counting("稍等。", arrive(sessions, "二"), boundary(), broken)
    await run(turn_id, execute)
    [second] = calls[0]["arrived"]
    assert statuses(sessions, [first, second]) == ["failed", "failed"]
    with sessions() as db: assert reprocess(AgentContext(db, "owner"), second)["status"] == "pending"
    new_turn, claimed = claim()
    assert new_turn != turn_id and claimed == [first, second]
    await run(new_turn, counting("这次好了。")[0])
    assert statuses(sessions, [first, second]) == ["done", "done"]
    assert [row.content for row in assistant_rows(sessions)] == ["这次好了。"]


# ---------------------------------------------------------------- IM

@pytest.mark.asyncio
async def test_each_im_message_gets_the_segment_that_answers_it(sessions, monkeypatch):
    from personal_agent.channels import channel_bridge
    from tests.test_personal_agent_im_actions import Adapter, CONN, inbound, listed_owner
    monkeypatch.setattr(channel_bridge, "SessionLocal", sessions)
    monkeypatch.setattr(channel_bridge, "connection", lambda ident: CONN)

    async def no_files(*args): return []
    monkeypatch.setattr("integrations.channels.inbound_attachments.ingest_attachments", no_files)
    adapter = Adapter()
    monkeypatch.setattr("integrations.channels.registry.get_adapter", lambda kind: adapter)
    with sessions() as db:
        svc = AgentContext(db, "owner"); svc.identity.ensure(); agent = svc.identity.agent()
        agent.config = {**agent.config, "channel_ids": ["bot"], **listed_owner("bot", "alice")}
        db.commit()
    await channel_bridge.admit_inbound(CONN, inbound("帮我查天气", "m1"), adapter)
    turn_id, _ = claim()

    async def im_arrives(plan):
        await channel_bridge.admit_inbound(CONN, inbound("北京的", "m2"), adapter)
    await run(turn_id, counting("稍等，我查一下。", im_arrives, boundary(), "北京今天晴。")[0])
    await channel_bridge.deliver_pending()
    assert ("edit", "om_1", "稍等，我查一下。") in adapter.calls and ("edit", "om_2", "北京今天晴。") in adapter.calls
    assert ("recall", "om_1") not in adapter.calls and ("recall", "om_2") not in adapter.calls


# ── review regressions ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduled_result_alone_goes_home(sessions, monkeypatch):
    from personal_agent.channels import channel_bridge
    monkeypatch.setattr(channel_bridge, "home_origin", lambda db, chat_id: {"channel_id": "bot", "channel_type": "lark", "msg": {}})
    unattended_result(sessions, "价格 120")
    turn_id, claimed = claim()
    await run(turn_id, counting("价格 120，涨了。")[0])
    [reply] = assistant_rows(sessions)
    assert reply.extra_data.get("channel_delivery")


@pytest.mark.asyncio
async def test_scheduled_result_batched_with_web_message_goes_home(sessions, monkeypatch):
    from personal_agent.channels import channel_bridge
    monkeypatch.setattr(channel_bridge, "home_origin", lambda db, chat_id: {"channel_id": "bot", "channel_type": "lark", "msg": {}})
    _, event = unattended_result(sessions, "价格 120")
    [message] = say(sessions, "顺便问一下天气")
    turn_id, claimed = claim()
    assert claimed == [event, message]
    await run(turn_id, counting("价格涨到 120；天气晴。")[0])
    [reply] = assistant_rows(sessions)
    assert reply.extra_data.get("channel_delivery"), "scheduled result lost its home IM delivery"


def arrive_result(factory, text, key):
    async def step(plan):
        _, ident = unattended_result(factory, text, key)
        plan.setdefault("arrived", []).append(ident)
    return step


@pytest.mark.asyncio
async def test_a_quiet_choice_does_not_swallow_an_absorbed_result(sessions):
    _, first = unattended_result(sessions, "价格仍为 120", "s:1")
    turn_id, claimed = claim()
    assert claimed == [first]
    execute, calls = counting(("pa_stay_quiet", {"reason": "无变化"}), arrive_result(sessions, "库存只剩 1 件！", "s:2"),
                              boundary(), "注意：库存只剩 1 件。")
    await run(turn_id, execute)
    assert [r.content for r in assistant_rows(sessions)], "the alert written for the absorbed result was discarded"
