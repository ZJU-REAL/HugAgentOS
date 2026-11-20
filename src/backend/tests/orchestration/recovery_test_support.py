"""Shared isolated fixtures and provider fakes."""

from __future__ import annotations

import core.db.engine as db_engine
import core.runs.events.store as store
import core.runs.events.store as run_event_stream
import core.runs.tasks as tasks
import pytest
from core.db.models import ChatSession
from core.db.engine import Base  # noqa: F401 (re-exported to tests)
from sqlalchemy import create_engine  # noqa: F401 (re-exported to tests)
from sqlalchemy.orm import sessionmaker  # noqa: F401 (re-exported to tests)
from tests.db_support import file_engine, session_factory, share_factory


@pytest.fixture()
def recovery_env(tmp_path, monkeypatch):
    engine = file_engine(tmp_path / 'executor-recovery.sqlite')
    sessions = session_factory(engine)
    share_factory(monkeypatch, sessions)
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
