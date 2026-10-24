"""Navigation reminders are journal verdicts, isolated by authenticated owner."""

from datetime import datetime, timedelta, timezone

import core.runs.runtime as runtime
import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession
from core.chat.sidebar_status import sidebar_run_statuses
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_chat(db, ident, owner="owner", **kw):
    row = ChatSession(chat_id=ident, user_id=owner, title=ident, **kw)
    db.add(row)
    return row


def add_run(db, ident, chat, status, when, owner="owner", kind="chat"):
    db.add(ChatRun(run_id=ident, chat_id=chat, user_id=owner, message_id="msg-"+ident,
        status=status, created_at=when, updated_at=when, request_payload={"kind":kind}))


def test_latest_real_verdicts_exclude_other_users_reserved_background_and_deleted(db):
    now = datetime.now(timezone.utc)
    for chat in ("done", "failed", "waiting", "cancel", "background", "deleted"):
        add_chat(db, chat)
    add_chat(db, "other", "other")
    add_chat(db, "pa_chat_private")
    db.get(ChatSession,"deleted").deleted_at=now
    add_run(db,"done","done","completed",now)
    add_run(db,"fail","failed","failed",now)
    add_run(db,"wait","waiting","needs_attention",now)
    add_run(db,"old-done","cancel","completed",now-timedelta(seconds=5))
    add_run(db,"cancel","cancel","cancelled",now)
    add_run(db,"bg","background","completed",now,kind="internal_job_agent")
    add_run(db,"deleted","deleted","failed",now)
    add_run(db,"other","other","failed",now,owner="other")
    add_run(db,"reserved","pa_chat_private","completed",now)
    db.commit()
    result={r["chat_id"]:r for r in sidebar_run_statuses(db,"owner")}
    assert {k:v["status"] for k,v in result.items()} == {
        "done":"completed","failed":"failed","waiting":"needs_attention","cancel":"cancelled"}
    assert result["done"]["message_id"] == "msg-done"


def test_new_run_supersedes_old_failure_and_old_live_tasks_are_still_visible(db):
    now=datetime.now(timezone.utc)
    add_chat(db,"retry");add_chat(db,"long")
    add_run(db,"failed","retry","failed",now-timedelta(seconds=3))
    add_run(db,"retry","retry","running",now)
    add_run(db,"long","long","running",now-timedelta(days=40))
    db.commit()
    rows={r["chat_id"]:r for r in sidebar_run_statuses(db,"owner")}
    assert rows["retry"]["run_id"] == "retry"
    assert rows["long"]["status"] == "running"








def test_active_run_route_only_adds_verdicts_when_requested(db,monkeypatch):
    from api.routes.v1.chats import run_views
    from core.auth.backend import UserContext
    pass
    monkeypatch.setattr(runtime,"list_active_runs_for_user",lambda *args:([],[]))
    monkeypatch.setattr(run_views.chat_context,"resolve_db_user_id",lambda db,uid:uid)
    add_chat(db,"mine")
    add_run(db,"final","mine","failed",datetime.now(timezone.utc))
    db.commit()
    user=UserContext(user_id="owner",user_center_id="owner",username="owner")
    normal=run_views.list_active_chat_runs(user=user,db=db,recent_seconds=0)
    assert "statuses" not in normal["data"]
    projected=run_views.list_active_chat_runs(user=user,db=db,recent_seconds=0,include_status=True)
    assert projected["data"]["statuses"][0]["status"]=="failed"
