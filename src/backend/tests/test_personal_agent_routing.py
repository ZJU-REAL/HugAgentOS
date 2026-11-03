"""The agent answers or delegates in its own turn; explicit task controls need no model."""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.db.engine import Base
from core.db.models import UserShadow, ChatMessage, Artifact
from personal_agent.models import PersonalFrontTurn, PersonalMessageInbox, PersonalMessageTask, PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit, pending
from personal_agent.conversation.associations import instructions
from tests.test_personal_agent import personal_agent_on  # noqa: F401  (module on, user permitted)


@pytest.fixture
def sessions(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    # Expire on commit like production: a tool that reads a row after its session closed must fail here.
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all([UserShadow(user_id=u, user_center_id=u, username=u) for u in ("owner", "other")]); db.commit()
    from personal_agent.conversation.router import context, reply, scheduler, steering, turn as router_turn
    from personal_agent.tools import front_tools
    for module in (context, reply, scheduler, steering, router_turn, front_tools): monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", factory)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    from core.runs import journal as run_journal

    @asynccontextmanager
    async def binding(**options):
        yield SimpleNamespace(run_id="run_" + uuid.uuid4().hex, owner="journal", chat_id=options["chat_id"])
    monkeypatch.setattr(run_journal, "durable_run_binding", binding)
    for name in ("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", "PERSONAL_AGENT_VOLUME_ROOT"): monkeypatch.delenv(name, raising=False)
    yield factory
    engine.dispose()


def tools_for(ident, uid="owner"):
    from personal_agent.tools.front_tools import register_front_tools
    kit = []
    register_front_tools(kit, SimpleNamespace(profile_ref=ident, current_user_id=uid))
    return {tool.name: tool._func for tool in kit}


def result(response):
    block = response.content[0]
    return json.loads(block["text"] if isinstance(block, dict) else block.text)


def scripted(*steps):
    """A stand-in model: text deltas and tool calls, in order. A failing tool is reported, as to a model."""
    async def execute(plan, binding):
        tools = tools_for(plan["turn"].id)
        for step in steps:
            if callable(step): await step(plan); continue
            if isinstance(step, str):
                plan["reply"].take("text_delta", step); continue
            name, args = step
            plan["reply"].take("tool_call", {"id": name, "name": name})
            status = "success"
            try: plan.setdefault("results", []).append(result(await tools[name](**args)))
            except Exception as exc:
                plan.setdefault("results", []).append({"error": type(exc).__name__}); status = "error"
            plan["reply"].take("tool_result", {"id": name, "status": status})
        plan["reply"].flush()
    return execute


def claim(owner="worker"):
    """The Turn a worker claims, and the message IDs of its Triggers in order; (None, []) when nothing is due."""
    from personal_agent.conversation.router import scheduler, triggers
    turn_id = scheduler.claim(owner)
    if turn_id is None: return None, []
    from core.db.engine import SessionLocal
    with SessionLocal() as db: return turn_id, [trigger.message_id for trigger in triggers.claimed(db, turn_id)]


async def turn(ident=None, execute=None, owner="worker"):
    """Claim and run one turn; ident, when given, must be its only Trigger. Returns its first Trigger."""
    from personal_agent.conversation.router import turn as router_turn
    turn_id, claimed = claim(owner)
    if ident: assert claimed == [ident]
    await router_turn.run_turn(turn_id, owner, execute)
    return claimed[0]


def prepared(turn_id, owner):
    """The plan of a claimed turn, as run_turn prepares it."""
    from personal_agent.conversation.router import turn as router_turn
    return router_turn._prepare(turn_id, owner)


def expire(factory, turn_id, **changes):
    """Interrupt a turn: its lease ends (or another worker owns it)."""
    with factory() as db:
        row = db.get(PersonalFrontTurn, turn_id)
        for name, value in ({"lease_until": 0} if not changes else changes).items(): setattr(row, name, value)
        db.commit()


def end(factory, turn_id):
    """A turn finished elsewhere: its lease no longer holds the owner's other Triggers."""
    with factory() as db:
        db.get(PersonalFrontTurn, turn_id).status = "done"
        for row in db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.turn_id == turn_id)): row.status = "done"
        db.commit()


def reply_of(db, ident):
    from personal_agent.conversation.router.turn import reply_id
    return db.get(ChatMessage, reply_id(ident))


# ---------------------------------------------------------------- admission and explicit controls

def test_message_admission_is_idempotent_without_creating_a_task(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        one = admit(svc, "检查服务器", "key")
        assert one == admit(svc, "检查服务器", "key") and one["status"] == "pending"
        assert not svc.tasks.list() and len(svc.conversation.list()) == 1
        with pytest.raises(ValueError): admit(svc, "检查另一服务器", "key")
        assert db.scalar(select(func.count()).select_from(PersonalMessageInbox)) == 1
        assert pending(svc) == [one]


def test_task_card_controls_apply_at_once_without_a_turn(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        a = svc.tasks.submit("部署 HugAgentOS", "a")["id"]; b = svc.tasks.submit("规划清迈旅行", "b")["id"]
        new = admit(svc, "写部署文档", "new", work_item_ids=[a], intent="new")
        update = admit(svc, "用 feature/demo 分支", "update", work_item_ids=[a], intent="update")
        paused = admit(svc, "先停", "pause", work_item_ids=[b], intent="pause")
        assert {new["status"], update["status"], paused["status"]} == {"done"} and pending(svc) == []
        assert a in new["work_item_ids"] and len(svc.tasks.list()) == 3
        assert instructions(db, svc.identity.agent().chat_id, a) == ["用 feature/demo 分支"]
        assert db.get(PersonalWorkItem, b).status == "waiting"
        assert svc.conversation.list()[-1]["role"] == "assistant" and "规划清迈旅行" in svc.conversation.list()[-1]["content"]


def test_resume_never_approves_a_pending_action(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("发送文件", "a")["id"]
        row = db.get(PersonalWorkItem, a); row.status = "waiting"; row.checkpoint = {"pending_approval": {"id": "p", "action": {"kind": "email.send"}}}; db.commit()
        count = len(svc.conversation.list())
        with pytest.raises(ValueError): admit(svc, "继续发邮件", "one", work_item_ids=[a], intent="resume")
        assert db.get(PersonalWorkItem, a).status == "waiting" and db.get(PersonalWorkItem, a).checkpoint["pending_approval"]
        assert len(svc.conversation.list()) == count


def test_foreign_tasks_and_replies_are_rejected(sessions):
    with sessions() as db:
        foreign = AgentContext(db, "other").tasks.submit("私有部署", "other")["id"]
        svc = AgentContext(db, "owner")
        with pytest.raises(LookupError): admit(svc, "停止", "one", work_item_ids=[foreign], intent="cancel")
        mine = admit(svc, "你好", "hello")
        with pytest.raises(LookupError): admit(AgentContext(db, "other"), "查询", "x", reply_to=mine["message_id"])
        assert db.get(PersonalWorkItem, foreign).status == "queued"


def test_legacy_single_task_json_context_still_reads_without_migration(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("部署 A", "a")["id"]
        svc.tasks.steer(a, "用 feature/demo 分支", "update")
        from sqlalchemy import delete
        db.execute(delete(PersonalMessageTask)); db.commit()
        assert instructions(db, svc.identity.agent().chat_id, a) == ["用 feature/demo 分支"]


# ---------------------------------------------------------------- the agent's turn

@pytest.mark.asyncio
async def test_small_talk_is_answered_in_the_conversation_without_a_task(sessions):
    with sessions() as db: entry = admit(AgentContext(db, "owner"), "你好", "hi")
    await turn(entry["message_id"], scripted("你好！", "有什么可以帮你？"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert not svc.tasks.list()
        user, reply = svc.conversation.list()
        assert (user["role"], reply["role"]) == ("user", "assistant")
        assert reply["content"] == "你好！有什么可以帮你？" and reply["reply_to"] == entry["message_id"] and not reply["streaming"]
        assert db.get(PersonalMessageInbox, entry["message_id"]).status == "done" and pending(svc) == []


@pytest.mark.asyncio
async def test_the_reply_streams_into_the_conversation(sessions):
    seen = {}

    async def look(plan):
        with sessions() as db: seen["rows"] = AgentContext(db, "owner").conversation.list()
    with sessions() as db: entry = admit(AgentContext(db, "owner"), "讲个笑话", "joke")

    async def execute(plan, binding):
        plan["reply"].take("text_delta", "从前有")
        plan["reply"].flush()
        await look(plan)
        plan["reply"].take("text_delta", "一只猫。")
    await turn(entry["message_id"], execute)
    partial = seen["rows"][-1]
    assert partial["streaming"] and partial["content"] == "从前有"
    with sessions() as db: assert reply_of(db, entry["message_id"]).content == "从前有一只猫。"


@pytest.mark.asyncio
async def test_agent_delegates_with_a_self_contained_goal_and_the_attachments(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); svc.identity.ensure()
        db.add(Artifact(artifact_id="file1", user_id="owner", chat_id=svc.identity.agent().chat_id, type="document", title="f", filename="f.pdf",
            size_bytes=3, mime_type="application/pdf", storage_key="k")); db.commit()
        entry = admit(svc, "帮我总结这份报告", "sum", artifact_ids=["file1"])
    await turn(entry["message_id"], scripted(
        ("pa_task_create", {"goal": "阅读附件 f.pdf 并写一页中文摘要，列出三条结论", "title": "总结报告"}),
        "好的，我开始整理摘要，完成后告诉你。"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        [task] = svc.tasks.list()
        assert task["title"] == "总结报告" and task["goal"].startswith("阅读附件")
        assert db.get(PersonalWorkItem, task["id"]).checkpoint["input_artifact_ids"] == ["file1"]
        user, reply = svc.conversation.list()
        assert user["work_item_ids"] == [task["id"]] and reply["content"] == "好的，我开始整理摘要，完成后告诉你。"
        assert db.get(PersonalMessageInbox, entry["message_id"]).decision["work_item_ids"] == [task["id"]]


@pytest.mark.asyncio
async def test_one_message_changes_several_tasks_with_scoped_instructions(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("部署", "a")["id"]; b = svc.tasks.submit("旅行", "b")["id"]
        entry = admit(svc, "部署用 feature/demo 分支，旅行计划先停", "multi")
    plan = {}

    async def keep(p): plan.update(p)
    await turn(entry["message_id"], scripted(
        ("pa_task_get", {"task_id": a}),
        ("pa_task_update", {"task_id": a, "instruction": "部署用 feature/demo 分支"}),
        ("pa_task_control", {"task_id": b, "action": "pause"}), keep, "部署已改用 feature/demo 分支，旅行计划先暂停了。"))
    assert plan["results"][1:] == [{"updated": True, "status": "queued"}, {"status": "waiting"}]
    with sessions() as db:
        chat = AgentContext(db, "owner").identity.agent().chat_id
        assert instructions(db, chat, a) == ["部署用 feature/demo 分支"] and instructions(db, chat, b) == []
        assert db.get(PersonalWorkItem, b).status == "waiting" and db.get(PersonalWorkItem, a).checkpoint.get("continuation_seq") is not None


@pytest.mark.asyncio
async def test_continue_in_the_conversation_cannot_approve_an_action(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("发送文件", "a")["id"]
        row = db.get(PersonalWorkItem, a); row.status = "waiting"; row.checkpoint = {"pending_approval": {"id": "p", "action": {}}}; db.commit()
        entry = admit(svc, "继续发吧", "go")
    plan = {}

    async def keep(p): plan.update(p)
    await turn(entry["message_id"], scripted(("pa_task_control", {"task_id": a, "action": "resume"}), keep, "需要你在审批卡片里批准。"))
    assert plan["results"] == [{"error": "ValueError"}]
    with sessions() as db: assert db.get(PersonalWorkItem, a).checkpoint["pending_approval"]


@pytest.mark.asyncio
async def test_retried_turn_does_not_start_the_same_task_twice(sessions):
    from personal_agent.conversation.router import turn as router_turn
    with sessions() as db: entry = admit(AgentContext(db, "owner"), "调研三款笔记软件", "research")
    ident = entry["message_id"]
    turn_id, _ = claim("crashed")
    plan = prepared(turn_id, "crashed")
    token = router_turn.current_turn.set(plan["turn"])
    try: await tools_for(turn_id)["pa_task_create"](goal="对比三款笔记软件", title="笔记软件")
    finally: router_turn.current_turn.reset(token)
    expire(sessions, turn_id)
    # The new worker takes over the same Turn, with the same Triggers.
    assert claim("worker") == (turn_id, [ident])
    await router_turn.run_turn(turn_id, "worker", scripted(("pa_task_create", {"goal": "对比三款笔记软件", "title": "笔记软件"}), "已经开始调研。"))
    with sessions() as db:
        assert len(AgentContext(db, "owner").tasks.list()) == 1
        assert db.get(PersonalFrontTurn, turn_id).attempts == 2
        assert reply_of(db, ident).content == "已经开始调研。"


@pytest.mark.asyncio
async def test_tools_stop_when_another_worker_owns_the_turn(sessions):
    from personal_agent.conversation.router import turn as router_turn
    with sessions() as db: admit(AgentContext(db, "owner"), "做个计划", "plan")
    turn_id, _ = claim("old")
    plan = prepared(turn_id, "old")
    expire(sessions, turn_id, owner="new")
    token = router_turn.current_turn.set(plan["turn"])
    try:
        with pytest.raises(router_turn.TurnLost): await tools_for(turn_id)["pa_task_create"](goal="计划")
    finally: router_turn.current_turn.reset(token)
    with sessions() as db: assert not AgentContext(db, "owner").tasks.list()
    assert not router_turn.finish(plan["turn"], plan["reply"])


@pytest.mark.asyncio
async def test_a_failed_turn_tells_the_user(sessions):
    with sessions() as db: entry = admit(AgentContext(db, "owner"), "你好", "hi")

    async def broken(plan, binding): raise RuntimeError("model offline")
    await turn(entry["message_id"], broken)
    with sessions() as db:
        from personal_agent.conversation.router.kinds import FAILED
        assert db.get(PersonalMessageInbox, entry["message_id"]).status == "failed"
        assert reply_of(db, entry["message_id"]).content == FAILED


@pytest.mark.asyncio
async def test_turns_of_one_owner_run_in_order_and_give_up_after_repeated_interruptions(sessions):
    from personal_agent.conversation.router import turn as router_turn
    from personal_agent.conversation.router.kinds import FAILED
    with sessions() as db: one = admit(AgentContext(db, "owner"), "第一条", "one")
    first, claimed = claim("a")
    assert claimed == [one["message_id"]]
    # The second message arrives while the first turn runs: it waits for the next turn.
    with sessions() as db: two = admit(AgentContext(db, "owner"), "第二条", "two")
    assert claim("b") == (None, [])
    await router_turn.run_turn(first, "a", scripted("收到第一条"))
    second = None
    for attempt in range(router_turn.MAX_ATTEMPTS):
        turn_id, claimed = claim("w" + str(attempt))
        assert claimed == [two["message_id"]] and second in (None, turn_id)
        second = turn_id
        expire(sessions, turn_id)

    async def forbidden(plan, binding): raise AssertionError("an exhausted turn must not run again")
    await turn(two["message_id"], forbidden)
    with sessions() as db:
        assert db.get(PersonalMessageInbox, two["message_id"]).status == "failed"
        assert db.get(PersonalFrontTurn, second).status == "failed"
        assert reply_of(db, two["message_id"]).content == FAILED


def test_turn_context_has_rules_tasks_and_the_conversation(sessions, monkeypatch):
    from core.db import engine as db_engine
    from personal_agent.conversation.router import context as front
    # Workspace seeding reads the agent through the default session factory.
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("部署服务器 Alpha", "a")["id"]
        svc.tasks.complete(a, "部署完成", {})
        first = admit(svc, "你好", "hi")
        svc.conversation.append(svc.identity.agent(), "assistant", "你好！", None, reply_to=first["message_id"]); db.commit()
        entry = admit(svc, "那个部署怎么样了", "status")
        prompt = front.system_prompt(entry["message_id"]) + front.turn_context(entry["message_id"])
        rows = front.history(db, entry["message_id"])
    assert "Conversation Rules" in prompt and a in prompt and "部署服务器 Alpha" in prompt
    assert rows[-1] == {"role": "user", "content": "那个部署怎么样了"}
    assert rows[0]["role"] == "user" and "[Task result: 部署服务器 Alpha]\n部署完成" in rows[0]["content"]
    assert all(x["role"] != y["role"] for x, y in zip(rows, rows[1:]))


# ---------------------------------------------------------------- task results come back through the agent

@pytest.mark.asyncio
async def test_a_finished_task_reports_through_the_agent(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("查北京天气", "a")["id"]
        svc.tasks.complete(a, "北京今天晴，25°C。", {})
        assert [m["role"] for m in svc.conversation.list()] == ["user"]
        [event] = pending(svc)
    await turn(event["message_id"], scripted("你让我查的北京天气好了：今天晴，25°C。"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        reply = svc.conversation.list()[-1]
        assert reply["content"].startswith("你让我查的北京天气好了") and reply["work_item_id"] == a and a in reply["work_item_ids"]
        assert db.get(PersonalWorkItem, a).result == "北京今天晴，25°C。"


@pytest.mark.asyncio
async def test_long_or_unreported_results_reach_the_user_unchanged(sessions):
    from personal_agent.conversation.router.triggers import LONG_RESULT
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("写长报告", "a")["id"]; b = svc.tasks.submit("短问题", "b")["id"]
        svc.tasks.complete(a, "长" * (LONG_RESULT + 1), {})
        assert svc.conversation.list()[-1]["content"] == "长" * (LONG_RESULT + 1) and not pending(svc)
        svc.tasks.complete(b, "短结果", {})
        [event] = pending(svc)

    async def broken(plan, binding): raise RuntimeError("model offline")
    await turn(event["message_id"], broken)
    with sessions() as db: assert AgentContext(db, "owner").conversation.list()[-1]["content"] == "短结果"


@pytest.mark.asyncio
async def test_only_a_scheduled_result_may_stay_quiet(sessions):
    from personal_agent.scheduling.schedules import SCHEDULE_KEY_PREFIX
    with sessions() as db:
        svc = AgentContext(db, "owner")
        asked = svc.tasks.submit("查快递", "asked")["id"]; watch = svc.tasks.submit("检查价格是否低于 100", SCHEDULE_KEY_PREFIX + "s:1")["id"]
        svc.tasks.complete(asked, "快递已签收", {})
        [first] = pending(svc)
    plan = {}

    async def keep(p): plan.update(p)
    # A requested result has no way to stay quiet: the tool refuses it.
    await turn(first["message_id"], scripted(("pa_stay_quiet", {"reason": "无"}), keep, "快递已经签收了。"))
    assert plan["results"] == [{"error": "PermissionError"}]
    with sessions() as db:
        svc = AgentContext(db, "owner"); svc.tasks.complete(watch, "价格仍为 120", {})
        [second] = pending(svc)
    await turn(second["message_id"], scripted(("pa_stay_quiet", {"reason": "价格没变"})))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert [m["content"] for m in svc.conversation.list() if m["role"] == "assistant"] == ["快递已经签收了。"]
        assert db.get(PersonalMessageInbox, second["message_id"]).decision["quiet"]


@pytest.mark.asyncio
async def test_message_turns_cannot_stay_quiet_and_reports_cannot_change_tasks(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); a = svc.tasks.submit("查", "a")["id"]
        admit(svc, "你好", "hi")
    # The tool list is the same every turn (prompt cache); the tool itself refuses a message turn.
    from personal_agent.conversation.router import turn as router_turn
    first, _ = claim("w")
    assert "pa_stay_quiet" in tools_for(first)
    # The result arrives while the message's turn runs, so it gets a turn of its own.
    with sessions() as db: AgentContext(db, "owner").tasks.complete(a, "结果", {})
    end(sessions, first)
    with sessions() as db: event = pending(AgentContext(db, "owner"))[0]["message_id"]
    turn_id, claimed = claim("w2")
    assert claimed == [event]
    plan = prepared(turn_id, "w2")
    token = router_turn.current_turn.set(plan["turn"])
    try:
        with pytest.raises(PermissionError): await tools_for(turn_id)["pa_task_create"](goal="新任务")
    finally: router_turn.current_turn.reset(token)


@pytest.mark.asyncio
async def test_im_messages_get_the_finished_reply_and_the_task_report_back_in_im(sessions):
    origin = {"channel_id": "bot", "channel_type": "lark", "msg": {"text": "hi"}, "placeholder_id": None}
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "查一下天气", "im", extra={"origin": "channel", "channel_origin": origin})
    seen = {}

    async def look(plan):
        with sessions() as db: seen["streaming"] = (reply_of(db, plan["turn"].head).extra_data or {}).get("channel_delivery")
    await turn(entry["message_id"], scripted(("pa_task_create", {"goal": "查北京天气", "title": "查天气"}), look, "好的，我去查。"))
    assert seen["streaming"] is None  # A reply still being written is never sent to IM.
    with sessions() as db:
        delivery = reply_of(db, entry["message_id"]).extra_data["channel_delivery"]
        assert delivery["status"] == "pending" and delivery["origin"] == origin
        svc = AgentContext(db, "owner"); [task] = svc.tasks.list()
        svc.tasks.complete(task["id"], "晴，25°C", {})
        [event] = pending(svc)
    await turn(event["message_id"], scripted("天气查好了：晴，25°C。"))
    with sessions() as db:
        assert reply_of(db, event["message_id"]).extra_data["channel_delivery"]["origin"] == origin


@pytest.mark.asyncio
async def test_history_shows_the_tool_calls_each_reply_made(sessions):
    """A reply that only claims an action must not read like one that performed it."""
    from personal_agent.conversation.router import context as front
    with sessions() as db: start = admit(AgentContext(db, "owner"), "查一下天气", "weather")
    await turn(start["message_id"], scripted(("pa_task_create", {"goal": "查北京天气", "title": "查天气"}), "已开始查天气。"))
    with sessions() as db: claim = admit(AgentContext(db, "owner"), "天气先暂停", "pause")
    await turn(claim["message_id"], scripted("好的，已暂停。"))
    with sessions() as db:
        last = admit(AgentContext(db, "owner"), "现在怎么样", "now")
        rows = front.history(db, last["message_id"])
    blocks = [block for row in rows if isinstance(row["content"], list) for block in row["content"]]
    assert [block.get("name") for block in blocks if block.get("type") == "tool_call"] == ["pa_task_create"]
    assert rows[-1] == {"role": "user", "content": "现在怎么样"}


# ---------------------------------------------------------------- first run (BOOTSTRAP.md)

@pytest.fixture
def fresh(sessions, monkeypatch, tmp_path):
    """A new agent whose workspace volume is configured."""
    import core.db.engine
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db: agent_id = AgentContext(db, "owner").identity.ensure()["id"]
    # Seeding opens its own sessions; point them at the test database once the agent exists.
    monkeypatch.setattr(core.db.engine, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    return tmp_path / agent_id / "workspace"


@pytest.mark.asyncio
async def test_a_new_agent_hatches_once_and_greets_first(sessions, fresh):
    from personal_agent.conversation.router import context as front
    from personal_agent.conversation.router.triggers import HATCH, hatch
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert hatch(svc) and not hatch(svc)
        [entry] = pending(svc)
        # The hatch is for the agent: the user sees only the greeting.
        assert svc.conversation.list() == []
    seen = {}

    async def look(plan):
        seen["prompt"], seen["history"] = front.system_prompt(plan["turn"].id) + front.turn_context(plan["turn"].message_ids), plan["history"]
    await turn(entry["message_id"], scripted(look, "你好，我是 Hugo。"))
    assert "# Bootstrap Pending" in seen["prompt"] and "## /workspace/BOOTSTRAP.md" in seen["prompt"]
    assert seen["history"] == [{"role": "user", "content": HATCH}]
    with sessions() as db:
        [reply] = AgentContext(db, "owner").conversation.list()
        assert reply["role"] == "assistant" and reply["content"] == "你好，我是 Hugo。"
        # The next turn still sees the hatch, so the greeting is not dropped from its history.
        second = admit(AgentContext(db, "owner"), "叫我阿伦", "name")
        hatch, greeting = front.history(db, second["message_id"])[:2]
        assert hatch == {"role": "user", "content": HATCH} and greeting["role"] == "assistant"
    assert "# Bootstrap Pending" in front.system_prompt(second["message_id"]) + front.turn_context(second["message_id"])
    # Once the agent records the user, the platform removes BOOTSTRAP.md and turns are ordinary again.
    (fresh / "USER.md").write_text("- 总是称呼用户为阿伦\n", encoding="utf-8")
    assert "Bootstrap Pending" not in front.system_prompt(second["message_id"]) + front.turn_context(second["message_id"])
    assert not (fresh / "BOOTSTRAP.md").exists()


def test_an_agent_with_a_conversation_never_hatches(sessions, fresh):
    from personal_agent.conversation.router.triggers import hatch
    with sessions() as db:
        svc = AgentContext(db, "owner")
        admit(svc, "你好", "hi")
        assert not hatch(svc) and len(pending(svc)) == 1


# ---------------------------------------------------------------- schema

@pytest.mark.parametrize("filename", [
    "src/backend/alembic/versions/personalrouter01_front_turns.py",
    "ce/overlay/src/backend/alembic/versions/ce_0037_personal_router.py",
])
def test_router_schema_moves_leases_to_turns_and_keeps_triggers(filename):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import bindparam, JSON, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("router_migration", Path(__file__).resolve().parents[3] / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    rows = [("m1", {}, "responding", {"attempts": 1}), ("m2", {"kind": "task_event", "task_id": "t", "event": "completed"}, "done", {"attempts": 2, "work_item_ids": ["t"]}),
            ("m3", {"kind": "task_event", "task_id": "t", "event": "session_run_finished"}, "pending", {}),
            ("m4", {"kind": "heartbeat", "slot": 1}, "routing", {}), ("m5", {"kind": "bootstrap"}, "failed", {"attempts": 3})]
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE personal_agents (id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE chat_messages (message_id TEXT PRIMARY KEY, chat_id TEXT)"))
        conn.execute(text("CREATE TABLE personal_message_inbox (message_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, request_key TEXT NOT NULL,"
                          " payload JSON NOT NULL, status TEXT NOT NULL, decision JSON NOT NULL, question TEXT, owner TEXT, lease_until FLOAT, created_at TIMESTAMP)"))
        insert = text("INSERT INTO personal_message_inbox (message_id, agent_id, request_key, payload, status, decision, owner, lease_until)"
                      " VALUES (:m, 'a', :m, :p, :s, :d, 'w', 1.0)").bindparams(bindparam("p", type_=JSON()), bindparam("d", type_=JSON()))
        for ident, payload, status, decision in rows:
            conn.execute(text("INSERT INTO chat_messages VALUES (:m, 'c')"), {"m": ident})
            conn.execute(insert, {"m": ident, "p": payload, "s": status, "d": decision})
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            columns = {c["name"] for c in inspect(conn).get_columns("personal_message_inbox")}
            assert {"kind", "chat_id", "turn_id"} <= columns and not {"owner", "lease_until"} & columns
            assert "personal_front_turns" in inspect(conn).get_table_names()
            found = {r[0]: r[1:] for r in conn.execute(text("SELECT message_id, kind, chat_id, status, decision FROM personal_message_inbox"))}
            assert {k: v[:3] for k, v in found.items()} == {"m1": ("message", "c", "pending"), "m2": ("task_result", "c", "done"),
                "m3": ("session_run", "c", "pending"), "m4": ("heartbeat", "c", "pending"), "m5": ("bootstrap", "c", "failed")}
            assert json.loads(found["m2"][3]) == {"work_item_ids": ["t"]}
            module.downgrade()
            columns = {c["name"] for c in inspect(conn).get_columns("personal_message_inbox")}
            assert {"owner", "lease_until"} <= columns and "kind" not in columns
            assert "personal_front_turns" not in inspect(conn).get_table_names()
            module.upgrade()
    engine.dispose()
