"""Personal Agent usability fixes: approvals, schedules, recovery, run limits and final answers."""
from integrations.channels import inbound_attachments
import personal_agent.tasks.guards as _pa_guards
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from personal_agent.models import PersonalWorkItem
from personal_agent.context import AgentContext
from tests.test_personal_agent import personal_agent_on, sessions  # noqa: F401  (shared fixtures)


def running(db, ident, owner="worker"):
    row = db.get(PersonalWorkItem, ident)
    row.status, row.owner, row.run_id, row.lease_until = "running", owner, "run", time.time() + 90
    db.commit()
    return row


def test_task_scope_allows_same_action_class_but_never_email(sessions, monkeypatch):
    from personal_agent.tasks import approvals
    from personal_agent.tools import tools
    monkeypatch.setattr(approvals, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        service = AgentContext(db, "owner")
        row = running(db, service.tasks.submit("Use my computer", "one")["id"])
        click = {"kind": "computer.action", "target": "device:a", "action": {"type": "click", "x": 1, "y": 1}}
        digest, pending = approvals.authorize(row.id, "owner", click)
        assert pending["pending_approval"]
        db.refresh(row); row.status = "waiting"; db.commit()
        approvals.approve(service, row.id, digest, True, scope="task")
        running(db, row.id)
        other = {**click, "action": {"type": "scroll", "amount": 3}}
        assert approvals.authorize(row.id, "owner", other)[1] is None
        # Another target is a different scope and still needs approval.
        assert approvals.authorize(row.id, "owner", {**click, "target": "device:b"})[1]["pending_approval"]
        email = {"kind": "email.send", "to": "a@example.test"}
        db.refresh(row)
        row.checkpoint = {**row.checkpoint, "pending_approval": {"id": "e" * 64, "action": email}}
        row.status = "waiting"; db.commit()
        with pytest.raises(ValueError): approvals.approve(service, row.id, "e" * 64, True, scope="task")


def test_rejected_action_returns_to_model_instead_of_cancelling(sessions, monkeypatch):
    from personal_agent.tasks import approvals
    from personal_agent.tools import tools
    monkeypatch.setattr(approvals, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        service = AgentContext(db, "owner")
        row = running(db, service.tasks.submit("Create chat", "one")["id"])
        action = {"kind": "hugagent.session", "target": "cloud", "operation": "chat.create", "args": {"title": "x"}}
        digest, _ = approvals.authorize(row.id, "owner", action)
        db.refresh(row); row.status = "waiting"; db.commit()
        view = approvals.approve(service, row.id, digest, False)
        assert view["status"] == "queued" and not view["pending_approval"]
        running(db, row.id)
        assert approvals.authorize(row.id, "owner", action)[1]["status"] == "rejected"
        # A retry with a new CLI request key is the same action and stays rejected.
        assert approvals.authorize(row.id, "owner", {**action, "request_key": "new"})[1]["status"] == "rejected"
        assert approvals.authorize(row.id, "owner", {**action, "args": {"title": "y"}})[1]["pending_approval"]


def test_schedule_validation_and_single_fire_per_occurrence(sessions):
    from personal_agent.scheduling import schedules
    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        with pytest.raises(ValueError): schedules.create(db, "owner", "x", "y", cron="* * * * *")
        with pytest.raises(ValueError): schedules.create(db, "owner", "x", "y")
        with pytest.raises(ValueError): schedules.create(db, "owner", "x", "y", run_at="2000-01-01T08:00")
        daily = schedules.create(db, "owner", "Mail", "Check my mail", cron="0 8 * * *", timezone="America/Los_Angeles")
        assert daily["next_run"].endswith("-07:00") or daily["next_run"].endswith("-08:00")
        once = schedules.create(db, "owner", "Call", "Call Bob", run_at="2099-01-01T09:00")
        future = 4102444800 + 86400
        assert schedules.fire_due(db, now=future) == 2
        assert schedules.fire_due(db, now=future) == 0
        service = AgentContext(db, "owner")
        goals = sorted(task["goal"] for task in service.tasks.list())
        assert goals == ["【定时任务：Call，现在执行】Call Bob", "【定时任务：Mail，现在执行】Check my mail"]
        assert sorted(task["title"] for task in service.tasks.list()) == ["Call", "Mail"]
        left = schedules.listing(service.identity.agent().config)
        assert [item["id"] for item in left] == [daily["id"]] and once["id"] not in str(left)
        assert service.conversation.list()[0]["origin"] == "schedule"
        schedules.delete(db, "owner", daily["id"])
        assert schedules.listing(service.identity.agent().config) == []


def test_paused_agent_skips_schedule_without_backlog(sessions):
    from personal_agent.scheduling import schedules
    with sessions() as db:
        service = AgentContext(db, "owner")
        service.identity.ensure()
        schedules.create(db, "owner", "Mail", "Check my mail", cron="0 8 * * *")
        service.identity.agent().status = "paused"; db.commit()
        assert schedules.fire_due(db, now=4102444800) == 0
        assert service.tasks.list() == []
        assert schedules.listing(service.identity.agent().config)[0]["next_run"]


def test_interrupted_task_resumes_only_without_unconfirmed_effects(sessions, monkeypatch):
    from personal_agent.tasks import worker as module
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        service = AgentContext(db, "owner")
        safe = running(db, service.tasks.submit("Read only", "one")["id"])
        risky = running(db, service.tasks.submit("Send mail", "two")["id"])
        risky.checkpoint = {"action_receipts": {"a": {"status": "uncertain"}}}
        for row in (safe, risky): row.lease_until = time.time() - 1
        db.commit()
    w = module.PersonalAgentWorker()
    w.recover()
    with sessions() as db:
        assert db.get(PersonalWorkItem, safe.id).status == "queued"
        assert db.get(PersonalWorkItem, risky.id).status == "waiting"
        notices = [m["content"] for m in AgentContext(db, "owner").conversation.list() if m["role"] == "assistant"]
        assert len(notices) == 1 and "Send mail" in notices[0]
        row = db.get(PersonalWorkItem, safe.id)
        row.checkpoint = {**row.checkpoint, "auto_resumes": module.MAX_AUTO_RESUMES}
        assert not module.resumable(row)


@pytest.mark.asyncio
async def test_run_has_no_total_deadline(sessions, monkeypatch):
    from personal_agent.tasks import worker as module
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    def forbidden_timeout(*args, **kwargs):
        raise AssertionError("Personal tasks must not install a total deadline")
    monkeypatch.setattr(module.asyncio, "timeout", forbidden_timeout)

    @asynccontextmanager
    async def binding(**kwargs):
        yield SimpleNamespace(run_id=kwargs["binding_run_id"], owner="test")
    monkeypatch.setattr(module, "durable_run_binding", binding)

    async def complete(*args):
        await asyncio.sleep(0.01)
        return "Finished", {}
    w = module.PersonalAgentWorker(execute=complete)
    monkeypatch.setattr(w, "dispatch_approved", lambda *a: asyncio.sleep(0))
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Endless task", "one")
    assert w.claim() == task["id"]
    await w.run(task["id"])
    with sessions() as db:
        row = db.get(PersonalWorkItem, task["id"])
        assert row.status == "completed" and row.result == "Finished"


def test_runaway_detection_and_final_cleanup():
    from personal_agent.tasks.runtime import runaway, clean_final
    loop = "\n".join(["Let me check the operation list and see what is pending now."] * 4)
    assert runaway(loop)
    assert not runaway("短回答。\n" * 3)
    assert runaway("x" * 30001)
    leaked = "I have enough information. Let me finalize.\n\n---\n\n## 今日头条\n1. 新闻"
    assert clean_final(leaked, "帮我搜一下今天的新闻") == "## 今日头条\n1. 新闻"
    assert clean_final("The email connector is off.\n\n## 结果\n无法查看", "看看我的邮件") == "## 结果\n无法查看"
    assert clean_final("Plain English answer.", "Find news") == "Plain English answer."


def test_settings_validate_concurrency_and_timezone(sessions):
    with sessions() as db:
        service = AgentContext(db, "owner")
        service.identity.ensure()
        with pytest.raises(ValueError): service.identity.configure({"concurrency": 0})
        with pytest.raises(ValueError): service.identity.configure({"timezone": "Mars/Base"})
        assert service.identity.configure({"concurrency": 5, "timezone": "America/Los_Angeles"})["config"]["timezone"] == "America/Los_Angeles"


def test_cli_hides_server_commands_without_server(capsys):
    from hugagent_cli import main as cli
    assert cli.main(["onboard"]) == 2
    assert "lightweight client" in capsys.readouterr().err
    assert "onboard" not in cli.parser().description


def test_pending_approval_blocks_other_tools_until_user_decides(sessions, monkeypatch):
    from personal_agent.tools import tools
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        row = running(db, AgentContext(db, "owner").tasks.submit("Use computer", "one")["id"])
        row.checkpoint = {"pending_approval": {"id": "a" * 64, "action": {"kind": "computer.action"}}}
        db.commit()
    with pytest.raises(PermissionError, match="pa_finish"): _pa_guards.owned_task(row.id, "owner")
    assert _pa_guards.owned_task(row.id, "owner", allow_pending=True)["run_id"] == "run"


def test_screen_match_tolerates_clock_but_not_navigation():
    from PIL import Image, ImageDraw
    from personal_agent.device_link import driver
    base = Image.new("RGB", (1280, 800), "white")
    clock = base.copy(); ImageDraw.Draw(clock).rectangle((1220, 0, 1280, 20), fill="black")
    page = base.copy(); ImageDraw.Draw(page).rectangle((0, 100, 1280, 700), fill="black")
    action = {"observation_id": "old", "expected_signature": driver.signature(base)}
    assert driver.same_screen(action, {"observation_id": "new", "signature": driver.signature(clock)})
    assert not driver.same_screen(action, {"observation_id": "new", "signature": driver.signature(page)})
    assert not driver.same_screen({"observation_id": "old"}, {"observation_id": "new", "signature": driver.signature(base)})


@pytest.mark.asyncio
async def test_old_screenshots_are_pruned_from_context():
    from agentscope.message import DataBlock, Msg, TextBlock, ToolResultBlock
    from personal_agent.computer.screenshot_pruning import screenshot_pruning_hook, PLACEHOLDER
    from tests.orchestration.hook_support import make_adapter
    adapter = make_adapter(screenshot_pruning_hook())
    image = {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    messages = [Msg(name="agent", role="assistant", content=[ToolResultBlock(id=str(i), name="pa_computer_observe",
        output=[TextBlock(text="observation " + str(i)), DataBlock(source=image)])]) for i in range(4)]
    agent = SimpleNamespace(state=SimpleNamespace(context=messages))
    async def reasoning(**kwargs):
        if False:
            yield None
    async for _ in adapter.on_reasoning(agent, {}, reasoning):
        pass
    messages = agent.state.context
    outputs = [m.content[0].output for m in messages]
    assert [o[1].text for o in outputs[:2]] == [PLACEHOLDER, PLACEHOLDER]
    assert all(isinstance(o[1], DataBlock) for o in outputs[2:])
    assert outputs[0][0].text == "observation 0"
    before = [m.model_dump() for m in messages]
    async for _ in adapter.on_reasoning(agent, {}, reasoning):
        pass
    assert [m.model_dump() for m in agent.state.context] == before


def test_final_answer_always_links_files_from_this_run():
    from personal_agent.tasks.runtime import with_exports
    exports = [{"run_id": "r1", "name": "old.txt", "url": "/api/files/a"}, {"run_id": "r2", "name": "note.txt", "url": "/api/files/b"},
               {"run_id": "r2", "name": "shown.txt", "url": "/api/files/c"}]
    out = with_exports("文件已导出。见 [shown](/api/files/c)", exports, "r2")
    assert "[note.txt](/api/files/b)" in out and "old.txt" not in out and out.count("/api/files/c") == 1


@pytest.mark.asyncio
async def test_channel_message_enters_inbox_and_reply_returns_to_same_conversation(sessions, monkeypatch):
    from integrations.channels.protocol import InboundMsg, SendResult
    from core.db.models import ChatMessage
    from personal_agent.channels import channel_bridge
    import integrations.channels.inbound as inbound
    monkeypatch.setattr(channel_bridge, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)

    async def no_files(*args): return []
    monkeypatch.setattr(inbound_attachments, 'ingest_attachments', no_files)
    sent = []

    class Adapter:
        async def send_text(self, conn, msg, text):
            sent.append(text); return SendResult(success=True, message_id="ph-1")
    conn = SimpleNamespace(channel_id="ch1", channel_type="lark", owner_user_id="owner")
    msg = InboundMsg(channel_id="ch1", channel_type="lark", text="帮我查天气", chat_type="p2p",
                     external_conversation_id="u1", message_id="m1", raw={"large": "x"})
    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        agent = AgentContext(db, "owner").identity.agent()
        agent.config = {**agent.config, "channel_ids": ["ch1"]}; db.commit()
        assert channel_bridge.handled_by_agent(db, conn)
    await channel_bridge.admit_inbound(conn, msg, Adapter())
    assert sent == ["🤔 已收到，正在处理…"]
    with sessions() as db:
        row = db.scalars(__import__("sqlalchemy").select(ChatMessage).where(ChatMessage.role == "user")).one()
        origin = row.extra_data["channel_origin"]
        assert origin["placeholder_id"] == "ph-1" and "raw" not in origin["msg"]
        svc = AgentContext(db, "owner")
        reply = svc.conversation.append(svc.identity.agent(), "assistant", "晴天", None, reply_to=row.message_id)
        db.commit()
        assert reply.extra_data["channel_delivery"]["status"] == "pending"
        assert svc.conversation.list() == []
        assert AgentContext(db, "owner", chat_id=row.chat_id).conversation.list()[0]["origin"] == "channel"
    delivered = []

    async def fake_send(conn, msg, text):
        delivered.append((msg.external_conversation_id, text)); return SendResult.ok()
    monkeypatch.setattr('integrations.channels.registry.get_adapter', lambda kind: SimpleNamespace(
        caps=SimpleNamespace(supports_markdown=False, max_message_len=0), send_text=fake_send))
    monkeypatch.setattr(channel_bridge, "connection", lambda ident: conn)
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    assert delivered == [("u1", "晴天")]


def test_avatar_and_name_validation(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        assert svc.identity.configure({"name": " 小助手 ", "avatar": "preset:fox"})["config"]["name"] == "小助手"
        assert svc.identity.configure({"avatar": "data:image/png;base64,iVBORw0KGgo="})["config"]["avatar"].startswith("data:image/png")
        with pytest.raises(ValueError): svc.identity.configure({"avatar": "https://example.com/a.png"})
        with pytest.raises(ValueError): svc.identity.configure({"avatar": "preset:unknown"})
        with pytest.raises(ValueError): svc.identity.configure({"name": " "})


def test_schedule_is_not_duplicated_and_scheduled_run_cannot_schedule(sessions, monkeypatch):
    from types import SimpleNamespace
    from personal_agent.scheduling import schedules
    from personal_agent.tools import tools
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(schedules, "SessionLocal", sessions, raising=False)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions, raising=False)
    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        first = schedules.create(db, "owner", "Mail", "Check my mail", cron="0 8 * * *")
        again = schedules.create(db, "owner", "Mail", "Remind me about mail", cron="0 8 * * *")
        assert again["id"] == first["id"] and again["existing"]
        schedules.fire_due(db, now=4102444800)
        task = next(t for t in AgentContext(db, "owner").tasks.list() if t["goal"].startswith("【定时任务"))
        running(db, task["id"])

    class Toolkit:
        def __init__(self): self.functions = {}
        def append(self, tool):
            fn = tool._func
            self.functions[fn.__name__] = fn
    toolkit = Toolkit()
    monkeypatch.setattr("personal_agent.tools.email.register_email_tools", lambda *a: None)
    monkeypatch.setattr("personal_agent.computer.computer.register_computer_tools", lambda *a: None)
    tools.register_personal_tools(toolkit, SimpleNamespace(profile_ref=task["id"], current_user_id="owner"))
    assert "pa_schedule_list" in toolkit.functions
    assert "pa_schedule_create" not in toolkit.functions and "pa_schedule_delete" not in toolkit.functions


@pytest.mark.asyncio
async def test_approval_while_run_is_ending_requeues_instead_of_completing(sessions, monkeypatch):
    from personal_agent.tasks import approvals
    from personal_agent.tools import tools
    from personal_agent.tasks import worker as module
    for target in (approvals, tools, module): monkeypatch.setattr(target, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)

    @asynccontextmanager
    async def binding(**kwargs):
        yield SimpleNamespace(run_id=kwargs["binding_run_id"], owner="test")
    monkeypatch.setattr(module, "durable_run_binding", binding)
    action = {"kind": "computer.action", "target": "cloud", "action": {"type": "click", "x": 4, "y": 4}}

    async def ask_then_answer(ident, _binding):
        # The tool asks, then the user approves while the model is still writing its reply.
        digest, _ = approvals.authorize(ident, "owner", action)
        with sessions() as db:
            approvals.approve(AgentContext(db, "owner"), ident, digest, True)
        return "Waiting for approval", {}
    w = module.PersonalAgentWorker(execute=ask_then_answer)
    monkeypatch.setattr(w, "dispatch_approved", lambda *a: asyncio.sleep(0))
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Click the button", "one")
    assert w.claim() == task["id"]
    await w.run(task["id"])
    with sessions() as db:
        row = db.get(PersonalWorkItem, task["id"])
        assert row.status == "queued" and row.owner is None
        assert not row.checkpoint.get("pending_approval") and not row.checkpoint.get("decided_during_run")
        assert row.checkpoint["approved_action_payloads"]


def test_live_stream_drops_input_while_watching():
    import struct
    from personal_agent.computer.live_stream import ClientFilter
    handshake = b"RFB 003.008\n" + b"\x01" + b"\x01"
    request = struct.pack(">BBHHHH", 3, 1, 0, 0, 1280, 800)
    key = struct.pack(">BBxxI", 4, 1, 0x61)
    pointer = struct.pack(">BBHH", 5, 1, 10, 10)
    paste = struct.pack(">BxxxI", 6, 2) + b"hi"
    encodings = struct.pack(">BxH", 2, 2) + struct.pack(">ii", 7, 0)
    stream = handshake + encodings + request + key + pointer + paste + request
    watch = ClientFilter(control=False)
    # Bytes may arrive split anywhere.
    out = b"".join(watch.feed(stream[i:i + 5]) for i in range(0, len(stream), 5))
    assert out == handshake + encodings + request + request
    assert ClientFilter(control=True).feed(stream) == stream
    with pytest.raises(ValueError): ClientFilter(control=False).feed(handshake + b"\x63")


def test_front_turn_knows_its_own_cloud_computer():
    # An empty device list once made the agent deny having a cloud computer.
    from personal_agent.conversation.front import COMPUTER
    from personal_agent.sessions.session_tool import describe
    assert "cloud computer" in COMPUTER and "browser" in COMPUTER and "pa_task_create" in COMPUTER
    assert "never your own cloud computer" in describe(False)
