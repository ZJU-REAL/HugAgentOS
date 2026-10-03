from types import SimpleNamespace

from sqlalchemy.orm import sessionmaker

from core.db.models import ChatMessage, ChatSession
from core.evolution import settlement as evolution_settlement
from core.memory.extractors import writers as W
from core.memory import outbox as O
from core.memory.outbox import common as outbox_common

from core.memory import pipeline as P
from core.memory import service as S
from core.memory import backend as memory_backend
from core.memory import effect_lane as E
from core.memory import profile_store as PS


def _configure_db(db_session, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind())
    with factory() as db:
        if db.get(ChatSession, "c1") is None:
            db.add(ChatSession(chat_id="c1", user_id="u1", title="memory outbox test"))
            db.commit()
    monkeypatch.setattr(outbox_common, "SessionLocal", factory)
    monkeypatch.setattr(E, "SessionLocal", factory)
    monkeypatch.setattr(PS, "SessionLocal", factory)
    return factory


def _memory_settings(**overrides):
    values = {
        "layered_enabled": True,
        "enabled": True,
        "bg_max_concurrency": 2,
        "outbox_lease_s": 30,
        "outbox_max_attempts": 3,
        "outbox_retry_base_s": 0,
        "llm_gate_enabled": True,
        "gate_timeout_s": 1,
        "extract_timeout_s": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _add_assistant_message(factory, message_id: str) -> None:
    with factory() as db:
        db.add(
            ChatMessage(
                message_id=message_id,
                chat_id="c1",
                role="assistant",
                content="ok",
            )
        )
        db.commit()
