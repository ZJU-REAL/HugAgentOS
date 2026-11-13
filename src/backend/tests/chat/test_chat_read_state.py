"""Server-side chat read positions: monotonic, owner-scoped, surfaced where the sidebar reads."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import core.runs.runtime as runtime
from core.auth.backend import UserContext
from core.chat.read_state import advance, read_seqs, seed_read_states
from core.chat.sidebar_status import sidebar_run_statuses
from core.db.engine import Base
from core.db.models import ChatMessage, ChatReadState, ChatRun, ChatSession, UserShadow
from core.infra.exceptions import ResourceNotFoundError


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for ident in ("owner", "other"):
            session.add(UserShadow(user_id=ident, user_center_id=ident, username=ident))
        session.commit()
        yield session
    engine.dispose()


def user(ident="owner"):
    return UserContext(user_id=ident, user_center_id=ident, username=ident)


def add_chat(db, chat_id, owner="owner", messages=(), **kw):
    db.add(ChatSession(chat_id=chat_id, user_id=owner, title=chat_id, **kw))
    db.flush()
    for seq, (message_id, role) in enumerate(messages, start=1):
        db.add(ChatMessage(message_id=message_id, chat_id=chat_id, chat_seq=seq, role=role, content="text"))
    db.flush()


def add_run(db, chat_id, run_id, message_id, status, seq=None, owner="owner"):
    now = datetime.now(timezone.utc)
    db.add(ChatRun(run_id=run_id, chat_id=chat_id, user_id=owner, message_id=message_id,
                   assistant_chat_seq=seq, status=status, created_at=now, updated_at=now,
                   request_payload={"kind": "chat"}))
    db.flush()


def mark(db, chat_id, message_id, who="owner"):
    from api.routes.v1.chats import sessions as routes
    import core.chat.operations.schemas as schemas

    return routes.mark_chat_read(chat_id=chat_id, request=schemas.MarkChatReadRequest(message_id=message_id),
                                 user=user(who), db=db)["data"]


def test_mark_read_only_moves_forward(db):
    add_chat(db, "c1", messages=[("u1", "user"), ("a1", "assistant"), ("u2", "user"), ("a2", "assistant")])
    db.commit()
    assert mark(db, "c1", "a2")["read_seq"] == 4
    # An old request arriving late (another client, a retried request) cannot un-read.
    late = mark(db, "c1", "a1")
    assert late["read_seq"] == 4 and late["read_message_id"] == "a2"
    assert advance(db, "owner", "c1", 0)["read_seq"] == 4
    assert read_seqs(db, "owner", ["c1", "missing"]) == {"c1": 4, "missing": 0}


def test_mark_read_accepts_a_runs_reserved_reply_before_it_is_written(db):
    add_chat(db, "c1", messages=[("u1", "user")])
    add_run(db, "c1", "run", "reply", "running", seq=2)
    db.commit()
    assert mark(db, "c1", "reply")["read_seq"] == 2


def test_cannot_read_or_mark_another_users_chat(db):
    add_chat(db, "theirs", owner="other", messages=[("u1", "user"), ("a1", "assistant")])
    add_chat(db, "mine", messages=[("m1", "user")])
    db.commit()
    with pytest.raises(ResourceNotFoundError):
        mark(db, "theirs", "a1")
    # A message of a different chat cannot be used to advance this one.
    with pytest.raises(ResourceNotFoundError):
        mark(db, "mine", "a1")
    assert db.query(ChatReadState).count() == 0
    from api.routes.v1.chats import sessions as routes
    from core.infra.exceptions import AppException

    with pytest.raises((ResourceNotFoundError, AppException)):
        routes.get_chat(chat_id="theirs", user=user(), db=db)


def test_sidebar_statuses_report_server_read_state(db, monkeypatch):
    add_chat(db, "done", messages=[("u1", "user"), ("a1", "assistant")])
    add_chat(db, "legacy", messages=[("u2", "user"), ("a2", "assistant")])
    add_chat(db, "other", owner="other", messages=[("u3", "user"), ("a3", "assistant")])
    add_run(db, "done", "r1", "a1", "completed", seq=2)
    add_run(db, "legacy", "r2", "a2", "failed")  # no reserved seq: falls back to the message
    add_run(db, "other", "r3", "a3", "completed", seq=2, owner="other")
    db.commit()
    rows = {row["chat_id"]: row for row in sidebar_run_statuses(db, "owner")}
    assert set(rows) == {"done", "legacy"}
    assert rows["done"]["read"] is False and rows["done"]["message_seq"] == 2
    assert rows["legacy"]["message_seq"] == 2 and rows["legacy"]["read"] is False
    # Reading on one client is what every other client sees on its next poll.
    mark(db, "done", "a1")
    advance(db, "other", "legacy", 9)  # someone else's position never counts for the owner
    db.commit()
    rows = {row["chat_id"]: row for row in sidebar_run_statuses(db, "owner")}
    assert rows["done"]["read"] is True and rows["done"]["read_seq"] == 2
    assert rows["legacy"]["read"] is False
    # Through the route the sidebar polls.
    from api.routes.v1.chats import run_views

    monkeypatch.setattr(runtime, "list_active_runs_for_user", lambda *args: ([], []))
    data = run_views.list_active_chat_runs(user=user(), db=db, include_status=True, recent_seconds=0)["data"]
    assert {row["chat_id"]: row["read"] for row in data["statuses"]} == {"done": True, "legacy": False}


def test_seed_marks_existing_chats_read_once(db):
    add_chat(db, "old", messages=[("u1", "user"), ("a1", "assistant")])
    add_chat(db, "empty")
    add_chat(db, "gone", messages=[("u2", "user")], deleted_at=datetime.now(timezone.utc))
    add_chat(db, "kept", messages=[("u3", "user"), ("a3", "assistant"), ("u4", "user")])
    advance(db, "owner", "kept", 1)
    db.commit()
    assert seed_read_states(db.connection()) == 2
    db.commit()
    assert read_seqs(db, "owner", ["old", "empty", "gone", "kept"]) == {"old": 2, "empty": 0, "gone": 0, "kept": 1}
    assert seed_read_states(db.connection()) == 0


def test_sqlite_bootstrap_seeds_when_it_creates_the_table(tmp_path, monkeypatch):
    import core.db.engine as engine_module

    engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    Base.metadata.create_all(engine)
    ChatReadState.__table__.drop(engine)
    with Session(engine) as db:
        db.add(UserShadow(user_id="owner", user_center_id="owner", username="owner"))
        add_chat(db, "old", messages=[("u1", "user"), ("a1", "assistant")])
        db.commit()
    monkeypatch.setattr(engine_module, "engine", engine)
    monkeypatch.setattr(engine_module, "_init_schema", lambda: Base.metadata.create_all(engine))
    engine_module.init_db()
    with Session(engine) as db:
        assert read_seqs(db, "owner", ["old"]) == {"old": 2}
    engine.dispose()


@pytest.mark.parametrize("filename", [
    "src/backend/alembic/versions/chatread01_chat_read_states.py",
    "ce/overlay/src/backend/alembic/versions/ce_0044_chat_read_states.py",
])
def test_migration_creates_and_seeds_read_positions(filename):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    spec = importlib.util.spec_from_file_location("read_migration", Path(__file__).resolve().parents[4] / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == ("padevice01" if "chatread01" in filename else "ce_0043")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ChatReadState.__table__.drop(engine)
    with Session(engine) as db:
        db.add(UserShadow(user_id="owner", user_center_id="owner", username="owner"))
        add_chat(db, "old", messages=[("u1", "user"), ("a1", "assistant")])
        add_chat(db, "empty")
        db.commit()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()  # idempotent: create_all may already have made the table
            assert dict(conn.execute(text("SELECT chat_id, read_seq FROM chat_read_states")).all()) == {"old": 2, "empty": 0}
            module.downgrade()
            assert "chat_read_states" not in inspect(conn).get_table_names()
    engine.dispose()
