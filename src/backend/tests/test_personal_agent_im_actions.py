"""IM conversations: the placeholder is reused, quoted replies reach their task, and decisions happen in IM."""
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.db.models import ChatMessage
from integrations.channels.protocol import InboundMsg, SendResult
from personal_agent.channels import channel_bridge, im_actions
from personal_agent.context import AgentContext
from personal_agent.models import PersonalMessageInbox, PersonalMessageTask, PersonalWorkItem
from tests.test_personal_agent_routing import sessions  # noqa: F401
from tests.test_personal_agent import personal_agent_on  # noqa: F401

CONN = SimpleNamespace(channel_id="bot", channel_type="lark", owner_user_id="owner")


class Adapter:
    """A channel that records every call. IDs of sent messages are om_1, om_2, ..."""

    def __init__(self, markdown=False):
        self.calls, self.count = [], 0
        self.caps = SimpleNamespace(supports_markdown=markdown, max_message_len=0)
        if markdown: self.send_markdown = self._markdown

    async def send_text(self, conn, msg, text):
        self.count += 1
        self.calls.append(("text", text))
        return SendResult.ok("om_" + str(self.count))

    async def _markdown(self, conn, msg, text):
        self.calls.append(("markdown", text))
        return SendResult.ok("om_md")

    async def edit_message(self, conn, message_id, text):
        self.calls.append(("edit", message_id, text))
        return SendResult.ok(message_id)

    async def recall_message(self, conn, msg, message_id):
        self.calls.append(("recall", message_id))
        return SendResult.ok(message_id)


@pytest.fixture
def im(sessions, monkeypatch):
    monkeypatch.setattr(channel_bridge, "SessionLocal", sessions)
    monkeypatch.setattr(channel_bridge, "connection", lambda ident: CONN)
    async def no_files(*args): return []
    monkeypatch.setattr("integrations.channels.inbound_attachments.ingest_attachments", no_files)
    adapter = Adapter()
    monkeypatch.setattr("integrations.channels.registry.get_adapter", lambda kind: adapter)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        agent = svc.identity.agent()
        agent.config = {**agent.config, "channel_ids": ["bot"], **listed_owner("bot", "alice")}
        db.commit()
    return SimpleNamespace(sessions=sessions, adapter=adapter)


def listed_owner(channel_id, sender):
    """Config in which sender is on the bot's allowed list (see owner_binding)."""
    return {"channel_allowed": {channel_id: [{"sender_id": sender, "sender_name": "", "keys": ["id:" + sender],
                                              "added_at": time.time()}]}}


def inbound(text, message_id, reply_to="", chat_type="p2p"):
    return InboundMsg(channel_id="bot", channel_type="lark", text=text, chat_type=chat_type, sender_id="alice",
                      external_conversation_id="u1", message_id=message_id, reply_to_message_id=reply_to)


async def receive(im, text, message_id, reply_to="", chat_type="p2p"):
    await channel_bridge.admit_inbound(CONN, inbound(text, message_id, reply_to, chat_type), im.adapter)
    with im.sessions() as db:
        return db.scalar(select(ChatMessage).where(ChatMessage.role == "user").order_by(ChatMessage.chat_seq.desc()).limit(1))


def im_task(im, message, status="running", **checkpoint):
    """A task started from an IM message, running under worker "w"."""
    from personal_agent.tasks import task_ops
    with im.sessions() as db:
        svc = AgentContext(db, "owner", message.chat_id)
        task = task_ops.create(svc, db.get(ChatMessage, message.message_id), "订会议室", "goal:" + message.message_id)
        task.status, task.owner, task.lease_until, task.run_id = status, "w", time.time() + 60, "run1"
        task.checkpoint = {**task.checkpoint, **checkpoint}
        db.commit()
        return task.id


def settle_run(db, task_id, fallback=""):
    """The Worker's run ended: the task is settled from its last report (worker/settle.finished)."""
    from personal_agent.models import PersonalAssignment
    from personal_agent.worker import settle
    task = db.get(PersonalWorkItem, task_id)
    asg = PersonalAssignment(id=task.run_id, work_item_id=task.id, agent_id=task.agent_id,
                             chat_id=task.chat_id, status="running", owner="w")
    db.add(asg)
    db.flush()
    settle.finished(db, asg, task, "owner", "w", fallback)
    db.commit()


# ---------------------------------------------------------------- the placeholder

async def test_reply_edits_the_placeholder_and_remembers_its_message_id(im):
    user = await receive(im, "帮我查天气", "m1")
    assert im.adapter.calls == [("text", im_actions.PLACEHOLDER)]
    with im.sessions() as db:
        svc = AgentContext(db, "owner")
        reply = svc.conversation.append(svc.identity.agent(), "assistant", "晴天", None, reply_to=user.message_id)
        db.commit()
        ident = reply.message_id
    await channel_bridge.deliver_pending()
    assert im.adapter.calls[1:] == [("edit", "om_1", "晴天")]
    with im.sessions() as db:
        delivery = db.get(ChatMessage, ident).extra_data["channel_delivery"]
        assert delivery["status"] == "sent" and delivery["message_id"] == "om_1"


async def test_markdown_reply_recalls_the_placeholder_instead_of_flattening_it(im, monkeypatch):
    im.adapter.caps.supports_markdown = True
    im.adapter.send_markdown = im.adapter._markdown
    user = await receive(im, "写个表格", "m1")
    with im.sessions() as db:
        svc = AgentContext(db, "owner")
        svc.conversation.append(svc.identity.agent(), "assistant", "| a |", None, reply_to=user.message_id)
        db.commit()
    await channel_bridge.deliver_pending()
    assert im.adapter.calls[1:] == [("recall", "om_1"), ("markdown", "| a |")]


async def test_unanswered_message_has_its_placeholder_recalled(im):
    user = await receive(im, "嗯", "m1")
    with im.sessions() as db:
        im_actions.mark_placeholder_recall(db, user.message_id)
        db.commit()
    await channel_bridge.deliver_pending()
    assert ("recall", "om_1") in im.adapter.calls
    with im.sessions() as db:
        assert db.get(ChatMessage, user.message_id).extra_data["channel_placeholder"]["status"] == "done"


async def test_failed_send_backs_off(im, monkeypatch):
    user = await receive(im, "你好", "m1")
    tries = []
    async def fail(conn, msg, text):
        tries.append(text)
        return SendResult.fail("transient")
    # A channel that can neither edit nor recall, and is down.
    down = SimpleNamespace(caps=im.adapter.caps, send_text=fail)
    monkeypatch.setattr("integrations.channels.registry.get_adapter", lambda kind: down)
    with im.sessions() as db:
        svc = AgentContext(db, "owner")
        reply = svc.conversation.append(svc.identity.agent(), "assistant", "你好呀", None, reply_to=user.message_id)
        db.commit()
        ident = reply.message_id
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    assert tries == ["你好呀"]  # The second pass is before the retry is due.
    with im.sessions() as db:
        delivery = db.get(ChatMessage, ident).extra_data["channel_delivery"]
        assert delivery["status"] == "pending" and delivery["due"] > time.time()


# ---------------------------------------------------------------- outcomes and questions

async def test_blocked_task_asks_in_im_and_a_quoted_reply_answers_it(im):
    user = await receive(im, "帮我订会议室", "m1")
    question = im_actions.new_question("订 3 楼还是 5 楼？")
    task_id = im_task(im, user, report={"assignment": "run1", "status": "blocked", "summary": "需要你选楼层"},
                      pending_question=question, task_wait_reason=question["text"])
    with im.sessions() as db:
        settle_run(db, task_id)
        task = db.get(PersonalWorkItem, task_id)
        assert task.status == "waiting" and task.waiting_reason == "订 3 楼还是 5 楼？"
        notice = db.scalar(select(ChatMessage).join(PersonalMessageTask, PersonalMessageTask.message_id == ChatMessage.message_id)
                           .where(PersonalMessageTask.relation == "question"))
        assert "订 3 楼还是 5 楼？" in notice.content and notice.chat_id == user.chat_id
        notice_id = notice.message_id
        # Waiting again (for example after a restart) does not ask twice.
        from personal_agent.tasks.worker import wait_for_user
        wait_for_user(db, task, task.waiting_reason)
        db.commit()
        assert db.scalar(select(PersonalMessageTask).where(PersonalMessageTask.relation == "question").offset(1)) is None
    await channel_bridge.deliver_pending()
    with im.sessions() as db:
        sent_as = db.get(ChatMessage, notice_id).extra_data["channel_delivery"]["message_id"]
    answer = await receive(im, "5 楼", "m2", reply_to=sent_as)
    with im.sessions() as db:
        task = db.get(PersonalWorkItem, task_id)
        assert task.status == "queued" and "pending_question" not in task.checkpoint
        reply = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant").order_by(ChatMessage.chat_seq.desc()).limit(1))
        assert reply.content.startswith("已转给任务「")
        # The turn needs no model: the quoted message named the task.
        assert db.get(PersonalMessageInbox, answer.message_id).decision["direct"] is True


async def test_dropped_task_ends_without_messaging_the_user(im):
    user = await receive(im, "看看这个 bug", "m1")
    task_id = im_task(im, user, report={"assignment": "run1", "status": "dropped", "summary": "无法复现，已经修好了"})
    with im.sessions() as db:
        settle_run(db, task_id)
        task = db.get(PersonalWorkItem, task_id)
        assert task.status == "completed" and task.checkpoint["outcome"] == "dropped"
        assert AgentContext(db, "owner").tasks.list()[0]["outcome"] == "dropped"
        kinds = [(row.payload or {}).get("kind") for row in db.scalars(select(PersonalMessageInbox))]
        assert "task_event" not in kinds


# ---------------------------------------------------------------- approvals in IM

def waiting_for_approval(im, user, kind="email.send"):
    action = {"kind": kind, "to": ["boss@example.com"], "subject": "周报"} if kind == "email.send" else {"kind": kind, "target": "cloud", "action": {"type": "click"}}
    task_id = im_task(im, user, pending_approval={"id": "a1", "action": action})
    with im.sessions() as db:
        settle_run(db, task_id, "等待批准")
    return task_id


async def test_approval_notice_and_reply_in_im(im):
    user = await receive(im, "把周报发给老板", "m1")
    task_id = waiting_for_approval(im, user)
    with im.sessions() as db:
        notice = db.scalar(select(ChatMessage).join(PersonalMessageTask, PersonalMessageTask.message_id == ChatMessage.message_id)
                           .where(PersonalMessageTask.relation == "approval"))
        assert "发送邮件给 boss@example.com" in notice.content and "回复「同意」" in notice.content
        # Email is checked every time, so there is no "allow for the whole task" answer.
        assert "全部同意" not in notice.content
    await receive(im, "同意！", "m2")
    with im.sessions() as db:
        task = db.get(PersonalWorkItem, task_id)
        assert task.status == "queued" and task.checkpoint["approved_actions"] == ["a1"]
        reply = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant").order_by(ChatMessage.chat_seq.desc()).limit(1))
        assert reply.content.startswith("已批准")


async def test_reject_in_im_and_group_messages_never_approve(im):
    user = await receive(im, "点一下按钮", "m1")
    task_id = waiting_for_approval(im, user, kind="computer.action")
    await receive(im, "同意", "g1", chat_type="group")
    with im.sessions() as db:
        assert db.get(PersonalWorkItem, task_id).checkpoint.get("pending_approval")
    await receive(im, "拒绝", "m2")
    with im.sessions() as db:
        task = db.get(PersonalWorkItem, task_id)
        assert task.checkpoint["action_receipts"]["a1"]["status"] == "rejected"


# ---------------------------------------------------------------- retry, search

async def test_failed_turn_and_delivery_are_listed_and_can_run_again(im):
    from personal_agent.conversation.attention import attention, reprocess, retry_delivery
    user = await receive(im, "总结一下", "m1")
    with im.sessions() as db:
        row = db.get(PersonalMessageInbox, user.message_id)
        row.status, row.decision = "failed", {}
        svc = AgentContext(db, "owner")
        reply = svc.conversation.append(svc.identity.agent(), "assistant", "总结如下", None, reply_to=user.message_id)
        reply.extra_data = {**reply.extra_data, "channel_delivery": {**reply.extra_data["channel_delivery"], "status": "failed", "attempts": 6}}
        db.commit()
        listed = attention(svc)
        assert [item["message_id"] for item in listed["turns"]] == [user.message_id]
        assert [item["message_id"] for item in listed["deliveries"]] == [reply.message_id]
        assert reprocess(svc, user.message_id)["status"] == "pending"
        row = db.get(PersonalMessageInbox, user.message_id)
        assert row.turn_id is None and row.decision["reprocessed"] == 1
        with pytest.raises(ValueError): reprocess(svc, user.message_id)
        retry_delivery(svc, reply.message_id)
        assert db.get(ChatMessage, reply.message_id).extra_data["channel_delivery"]["attempts"] == 0
        assert attention(svc) == {"turns": [], "deliveries": []}
        # Another owner cannot touch them.
        with pytest.raises(LookupError): reprocess(AgentContext(db, "other"), user.message_id)


async def test_search_finds_the_task_an_im_message_started(im):
    from personal_agent.conversation.attention import candidates, search
    user = await receive(im, "帮我订会议室", "om_abc123")
    task_id = im_task(im, user)
    with im.sessions() as db:
        found = search(AgentContext(db, "owner"), "om_abc123")
        assert [item["message_id"] for item in found["messages"]] == [user.message_id]
        assert [task["id"] for task in found["tasks"]] == [task_id]
        # Plain text still finds the message.
        assert search(AgentContext(db, "owner"), "订会议室")["tasks"][0]["id"] == task_id
        assert search(AgentContext(db, "other"), "om_abc123") == {"messages": [], "tasks": []}
        # The second IM message of a long reply finds the task the reply reports on.
        from personal_agent.models import PersonalAgent
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == "owner"))
        db.add(ChatMessage(message_id="pa_msg_long", chat_id=agent.chat_id, role="assistant", content="很长的结果",
            extra_data={"channel_delivery": {"status": "sent", "message_id": "om_part1", "message_ids": ["om_part1", "om_part2"]}}))
        db.flush()
        db.add(PersonalMessageTask(message_id="pa_msg_long", work_item_id=task_id, relation="final"))
        db.commit()
        assert [task["id"] for task in search(AgentContext(db, "owner"), "om_part2")["tasks"]] == [task_id]
    assert "C01:1700000000.123456" in candidates("https://team.slack.com/archives/C01/p1700000000123456")
