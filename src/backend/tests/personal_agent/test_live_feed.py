"""Open Personal Agent pages hear about committed changes through the run event log, not by polling."""

import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.db.engine as engine_module
from core.db.models import Base, ChatMessage, ChatSession
from personal_agent.conversation import live
from personal_agent.models import PersonalAgent, PersonalDevice, PersonalWorkItem


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    made = sessionmaker(bind=engine, expire_on_commit=False)
    # The publisher looks owners up with its own sessions.
    monkeypatch.setattr(engine_module, "SessionLocal", made)
    import personal_agent.computer.devices as devices
    monkeypatch.setattr(devices, "SessionLocal", made)
    monkeypatch.setattr(live, "redis_configured", lambda: False)
    yield made
    engine.dispose()


@pytest.fixture
def heard(monkeypatch):
    """(person, topic) pairs handed to the publisher, instead of publishing them."""
    got = []

    class Recorder:
        def submit(self, refs):
            got.append(set(refs))

    monkeypatch.setattr(live, "_publisher", lambda: Recorder())
    return got


def owner_with_agent(db, user):
    chat = "pa_chat_" + user
    db.add(ChatSession(chat_id=chat, user_id=user, title="home"))
    db.add(PersonalAgent(id="pa_" + user, user_id=user, chat_id=chat, sandbox_session_id="sb_" + user))
    db.commit()
    return chat


def message(chat, seq, **extra):
    return ChatMessage(message_id=f"{chat}_{seq}", chat_id=chat, chat_seq=seq, role="assistant", content="", extra_data=extra)


def test_new_message_in_a_personal_chat_is_announced(factory, heard):
    with factory() as db:
        chat = owner_with_agent(db, "u1")
        heard.clear()
        db.add(message(chat, 1))
        db.commit()
    assert heard == [{("u1", chat)}]


def test_other_chats_and_rolled_back_writes_are_not(factory, heard):
    with factory() as db:
        db.add(ChatSession(chat_id="chat_plain", user_id="u2", title="plain"))
        db.add(ChatMessage(message_id="m1", chat_id="chat_plain", chat_seq=1, role="user", content="hi"))
        db.commit()
        chat = owner_with_agent(db, "u3")
        heard.clear()
        db.add(message(chat, 1))
        db.flush()
        db.rollback()
    assert heard == []


def test_streaming_text_is_quiet_until_the_reply_is_complete(factory, heard):
    with factory() as db:
        chat = owner_with_agent(db, "u4")
        row = message(chat, 1, streaming=True)
        db.add(row)
        db.commit()
        heard.clear()
        row.content = "partial"
        row.extra_data = {"streaming": True, "activity": "web_search"}
        db.commit()
        assert heard == []
        row.content = "done"
        row.extra_data = {"streaming": False}
        db.commit()
    assert heard == [{("u4", chat)}]


def test_task_changes_are_announced_to_the_agents_owner(factory, heard):
    with factory() as db:
        owner_with_agent(db, "u5")
        heard.clear()
        db.add(PersonalWorkItem(id="pa_task_1", agent_id="pa_u5", goal="report", status="queued", request_key="k"))
        db.commit()
    assert heard == [{("u5", None)}]


def test_a_heartbeat_is_announced_only_when_the_computer_comes_online(factory, heard):
    with factory() as db:
        device = PersonalDevice(id="pa_device_1", user_id="u6", name="Mac", platform="macos", secret_hash="x",
                                capabilities=[], last_seen=time.time())
        db.add(device)
        db.commit()
        heard.clear()
        device.last_seen = time.time()
        db.commit()
        assert heard == []
        device.last_seen = time.time() - 3600
        db.commit()
        heard.clear()
        device.last_seen = time.time()
        db.commit()
    assert heard == [{("u6", None)}]


def test_departed_computers_are_announced(factory, heard):
    from personal_agent.computer.devices import ONLINE_WINDOW, announce_departures
    with factory() as db:
        now = time.time()
        db.add_all([
            PersonalDevice(id="gone", user_id="u7", name="a", platform="macos", secret_hash="x", capabilities=[], last_seen=now - ONLINE_WINDOW - 3),
            PersonalDevice(id="here", user_id="u8", name="b", platform="macos", secret_hash="x", capabilities=[], last_seen=now),
            PersonalDevice(id="long_gone", user_id="u9", name="c", platform="macos", secret_hash="x", capabilities=[], last_seen=now - 3600),
        ])
        db.commit()
        heard.clear()
    announce_departures(10)
    assert heard == [{("u7", None)}]


def test_a_commit_reaches_the_owners_open_page(factory):
    user = "u_" + uuid.uuid4().hex[:8]
    with factory() as db:
        chat = owner_with_agent(db, user)

    async def scenario():
        frames = live.follow(user)
        assert json.loads((await anext(frames))[6:]) == {"type": "ready"}
        pending = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0.05)
        with factory() as db:
            db.add(message(chat, 1))
            db.add(PersonalWorkItem(id="pa_task_" + user, agent_id="pa_" + user, goal="g", status="queued", request_key="k"))
            db.commit()
        frame = await asyncio.wait_for(pending, 5)
        await frames.aclose()
        return frame

    frame = asyncio.run(scenario())
    # The message and the task, committed together, arrive as one signal naming the conversation.
    assert json.loads(frame[6:]) == {"type": "changed", "topics": [chat]}


def test_rewriting_equal_values_or_renewing_a_lease_is_quiet(factory, heard):
    with factory() as db:
        owner_with_agent(db, "u10")
        task = PersonalWorkItem(id="pa_task_u10", agent_id="pa_u10", goal="g", status="running", request_key="k", lease_until=1.0)
        db.add(task)
        db.commit()
        heard.clear()
        task.goal = "g"
        task.lease_until = 2.0
        db.commit()
        assert heard == []
        task.status = "completed"
        db.commit()
    assert heard == [{("u10", None)}]


def test_bulk_statements_are_announced_to_the_rows_owners(factory, heard):
    from sqlalchemy import update
    from personal_agent.models import PersonalSessionOperation
    with factory() as db:
        chat = owner_with_agent(db, "u11")
        db.add(PersonalWorkItem(id="pa_task_u11", agent_id="pa_u11", goal="g", status="running", request_key="k"))
        db.add(PersonalSessionOperation(id="op1", user_id="u11", target="device:d", request_key="r", operation="chat.list", args={}, deadline=time.time() + 60))
        db.add(message(chat, 1))
        db.commit()
        heard.clear()
        db.execute(update(PersonalWorkItem).where(PersonalWorkItem.id == "pa_task_u11").values(status="completed"))
        db.execute(update(PersonalSessionOperation).where(PersonalSessionOperation.user_id == "u11").values(status="completed"))
        db.execute(update(ChatMessage).where(ChatMessage.chat_id == chat).values(content="x"))
        db.commit()
    assert heard == [{("u11", None), ("u11", "operation:op1"), ("u11", chat)}]


def test_a_heartbeat_with_chat_changes_tells_the_owners_pages(factory, heard):
    from personal_agent.computer import devices
    with factory() as db:
        paired = devices.pair(db, "u12", "Mac", "macos", ["hugagent.sessions"])
        device = db.get(PersonalDevice, paired["id"])
        device.sessions_enabled = 1
        db.commit()
        heard.clear()
        devices.heartbeat(db, paired["id"], paired["token"], False, True, changed_chats=["c1"], chat_list_changed=True)
        topic = "device:" + paired["id"]
        assert {("u12", topic), ("u12", topic + ":c1")} <= set().union(*heard)
        # Without session consent the computer's chats are not announced.
        heard.clear()
        devices.heartbeat(db, paired["id"], paired["token"], False, False, changed_chats=["c2"])
    assert not any(topic and topic.endswith(":c2") for pairs in heard for _user, topic in pairs)
