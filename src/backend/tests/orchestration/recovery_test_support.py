"""Shared isolated fixtures and provider fakes."""

from __future__ import annotations
import pytest
from core.db.engine import Base
from core.db.models import ChatSession
from orchestration import chat_run_executor as executor
from orchestration import run_event_stream
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
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    monkeypatch.setattr(run_event_stream, "redis_configured", lambda: True)
    monkeypatch.setattr("core.services.model_config.SessionLocal", sessions)
    executor._active_runs.clear()
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    yield sessions
    executor._active_runs.clear()
    engine.dispose()


async def _async_none():
    return None
