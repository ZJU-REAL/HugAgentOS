"""A linked computer reports its own chat changes with the heartbeat, so open pages need not poll it."""

import asyncio
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from core.db.models import Base, ChatMessage, ChatSession
from core.db.models.chat_runs import ChatRun
from personal_agent.device_link import chat_changes
from tests.personal_agent.test_device_link import Cloud, mailbox, make_link, home  # noqa: F401 - home is autouse


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    chat_changes.install()
    chat_changes.take()
    with sessionmaker(bind=engine)() as session:
        yield session
    chat_changes.take()
    engine.dispose()


def chat(db, ident, title="t"):
    db.add(ChatSession(chat_id=ident, user_id="me", title=title))
    db.commit()


def test_a_new_message_marks_its_chat_and_a_new_chat_the_list(db):
    chat(db, "c1")
    assert chat_changes.take() == (["c1"], True)
    db.add(ChatMessage(message_id="m1", chat_id="c1", chat_seq=1, role="user", content="hi"))
    db.commit()
    assert chat_changes.take() == (["c1"], False)


def test_the_personal_agents_cloud_chats_and_rolled_back_writes_are_not_reported(db):
    chat(db, "pa_chat_x")
    chat(db, "c2")
    chat_changes.take()
    db.add(ChatMessage(message_id="m2", chat_id="c2", chat_seq=1, role="user", content="hi"))
    db.flush()
    db.rollback()
    assert chat_changes.take() == ([], False)


def test_a_run_counts_when_its_status_changes_not_when_its_lease_is_renewed(db):
    chat(db, "c3")
    now = datetime.now(timezone.utc)
    db.add(ChatRun(run_id="r1", chat_id="c3", user_id="me", message_id="m", status="running", created_at=now, updated_at=now))
    db.commit()
    chat_changes.take()
    db.execute(update(ChatRun).where(ChatRun.run_id == "r1").values(lease_expires_at=now, updated_at=now))
    db.commit()
    assert chat_changes.take() == ([], False)
    db.query(ChatRun).filter(ChatRun.run_id == "r1").update({ChatRun.status: "completed", ChatRun.updated_at: now},
                                                             synchronize_session=False)
    db.commit()
    assert chat_changes.take() == (["c3"], False)


def test_renaming_a_chat_changes_the_list(db):
    chat(db, "c4")
    chat_changes.take()
    db.execute(update(ChatSession).where(ChatSession.chat_id == "c4").values(title="new"))
    db.commit()
    assert chat_changes.take() == (["c4"], True)


def test_nothing_is_reported_or_kept_without_session_consent(db):
    chat(db, "c5")
    assert chat_changes.report_fields(False) == ({}, None)
    assert chat_changes.take() == ([], False)
    chat(db, "c6")
    assert chat_changes.report_fields(True) == ({"changed_chats": ["c6"], "chat_list_changed": True}, (["c6"], True))


async def test_the_heartbeat_carries_changes_and_keeps_them_when_it_fails(db):
    cloud = Cloud()
    box = mailbox(make_link(sessions=True), cloud)
    chat(db, "c7")
    await box.heartbeat()
    assert cloud.bodies[-1][1]["changed_chats"] == ["c7"] and cloud.bodies[-1][1]["chat_list_changed"]
    await box.heartbeat()
    assert "changed_chats" not in cloud.bodies[-1][1]
    db.add(ChatMessage(message_id="m8", chat_id="c7", chat_seq=1, role="user", content="hi"))
    db.commit()
    import httpx
    box.http = httpx.AsyncClient(base_url="https://cloud.example", transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(httpx.HTTPError):
        await box.heartbeat()
    await box.close()
    assert chat_changes.take() == (["c7"], False)


async def test_a_change_wakes_the_heartbeat_early_but_not_a_kept_one():
    chat_changes.take()
    seen = chat_changes.version()
    start = time.monotonic()
    waiting = asyncio.ensure_future(chat_changes.wait(5, time.time() - 10, seen))
    await asyncio.sleep(0.05)
    chat_changes._note(["c9"], False)
    await asyncio.wait_for(waiting, 2)
    assert time.monotonic() - start < 1
    # Changes put back by a failed report wait for the regular heartbeat.
    seen = chat_changes.version()
    chat_changes.restore(["c9"], False)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(chat_changes.wait(5, time.time() - 10, seen), 0.3)
    chat_changes.take()
