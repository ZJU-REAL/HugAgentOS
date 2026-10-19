"""Isolated ledger state for process-tool integration tests."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db.engine import Base
from core.db.models import ChatSession, UserShadow
from core.services.commands import store
from orchestration.commands import runtime as command_runtime

@pytest.fixture
async def command_tool_state(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "tool-commands.db"),
        connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        for user in ("user-1", "pin-user"):
            db.add(UserShadow(user_id=user, username=user))
        db.flush()
        db.add_all([ChatSession(chat_id="chat-1", user_id="user-1", title="test"),
            ChatSession(chat_id="pin-chat", user_id="pin-user", title="test")])
        db.commit()
    monkeypatch.setattr(store, "SessionLocal", factory)
    runtime = command_runtime.CommandRuntime()
    monkeypatch.setattr(command_runtime, "runtime", runtime)
    yield factory
    await runtime.shutdown()
    engine.dispose()
