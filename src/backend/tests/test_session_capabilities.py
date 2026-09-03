"""Chat-sticky direct skill and connector activation tests."""

from __future__ import annotations

from types import SimpleNamespace

import core.db.engine as dbe
import pytest
from core.db.models import ChatSession, UserShadow
from core.llm import session_capabilities
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

USER = "sticky_user"
CHAT = "sticky_chat"


@pytest.fixture()
def sticky_env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/sticky.db")
    dbe.Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbe, "SessionLocal", test_session)
    with test_session() as db:
        db.add(UserShadow(user_id=USER, username=USER))
        db.add(ChatSession(chat_id=CHAT, user_id=USER, title="sticky"))
        db.commit()
    return SimpleNamespace(Session=test_session)


def test_record_and_restore_direct_capabilities(sticky_env, monkeypatch):
    session_capabilities.record_session_capability_activation(
        CHAT,
        skill_ids=["skill-a", "skill-a"],
        mcp_ids=["mcp-a"],
    )
    session_capabilities.record_session_capability_activation(
        CHAT,
        skill_ids=["skill-b"],
        mcp_ids=["mcp-a", "mcp-b"],
    )
    with sticky_env.Session() as db:
        row = db.query(ChatSession).filter(ChatSession.chat_id == CHAT).first()
        assert row.extra_data["activated_skill_ids"] == ["skill-a", "skill-b"]
        assert row.extra_data["activated_mcp_ids"] == ["mcp-a", "mcp-b"]

    import core.config.catalog_resolver as resolver

    def hard_gate(db, user_id, *, skill_ids=None, mcp_ids=None):
        assert user_id == USER
        return ["skill-a"], ["mcp-a"], ["skill-b"], ["mcp-b"]

    monkeypatch.setattr(resolver, "resolve_explicit_runtime_capabilities", hard_gate)
    restored = session_capabilities.resolve_session_activated_capabilities(
        user_id=USER,
        chat_id=CHAT,
    )
    assert restored.skill_ids == ["skill-a"]
    assert restored.mcp_ids == ["mcp-a"]


def test_missing_chat_is_empty(sticky_env):
    restored = session_capabilities.resolve_session_activated_capabilities(
        user_id=USER,
        chat_id="missing",
    )
    assert restored.skill_ids == []
    assert restored.mcp_ids == []
