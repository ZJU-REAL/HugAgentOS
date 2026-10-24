"""Shared isolated fixtures and provider fakes."""

from __future__ import annotations

import core.db.engine as db_engine
import core.runs.events.store as store
import core.runs.events.store as run_event_stream
import core.runs.tasks as tasks
import pytest
from core.db.engine import Base
from core.db.models import ChatSession
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def recovery_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'executor-recovery.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr(store, "redis_configured", lambda: True)
    monkeypatch.setattr('core.models.config.SessionLocal', sessions)
    tasks.process_state().active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    tasks.process_state().active_runs.clear()
    engine.dispose()


async def _async_none():
    return None
