"""The card stays true after the user corrects a memory.

The card is a durable record of what a turn wrote, and the user can act on it
directly. So the two have to move together: a memory the user deleted must
disappear from the card, and one they edited must show the new text. Otherwise
the transcript keeps offering actions on something the store no longer has.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.chat import ChatMessage
from core.evolution import settlement_store as SS
from core.evolution.settlement import LAYER_PROCEDURE, settle_turn


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[ChatMessage.__table__])
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(SS, "SessionLocal", Session)
    return Session


def _message(Session, message_id="m1"):
    with Session() as session:
        session.add(ChatMessage(message_id=message_id, chat_id="c1", role="assistant", content="hi"))
        session.commit()


def _card(Session, message_id="m1"):
    with Session() as session:
        row = session.query(ChatMessage).filter_by(message_id=message_id).first()
        return (row.extra_data or {}).get(SS.METADATA_KEY) or {}


def _entry(handle, text="先核验主体"):
    return {"layer": LAYER_PROCEDURE, "handle": handle, "text": text}


def test_an_empty_settlement_is_persisted_so_polling_terminates(db):
    _message(db)
    SS.persist_summary("m1", settle_turn(message_id="m1", memory_entries=[]))
    assert _card(db).get("state") == "empty"


def test_deleting_a_memory_removes_it_from_the_card(db):
    _message(db)
    SS.persist_summary(
        "m1",
        settle_turn(message_id="m1", memory_entries=[_entry("a"), _entry("b")]),
    )

    assert SS.drop_entry("m1", "a") is True
    card = _card(db)
    assert [e["handle"] for e in card["entries"]] == ["b"]
    assert card["gain"] == 1


def test_deleting_the_last_memory_makes_the_card_disappear(db):
    _message(db)
    SS.persist_summary("m1", settle_turn(message_id="m1", memory_entries=[_entry("a")]))

    SS.drop_entry("m1", "a")
    # An entry-less card would render as an evolution that left nothing behind.
    assert _card(db)["state"] == "empty"


def test_editing_a_memory_updates_the_text_shown_on_the_card(db):
    _message(db)
    SS.persist_summary("m1", settle_turn(message_id="m1", memory_entries=[_entry("a")]))

    assert SS.update_entry_text("m1", "a", "先核验主体，再取数") is True
    entry = _card(db)["entries"][0]
    assert entry["text"] == "先核验主体，再取数"
    assert entry["action"] == "edited"


def test_acting_on_an_unknown_handle_changes_nothing(db):
    _message(db)
    SS.persist_summary("m1", settle_turn(message_id="m1", memory_entries=[_entry("a")]))

    assert SS.drop_entry("m1", "nope") is False
    assert SS.update_entry_text("m1", "nope", "x") is False
    assert _card(db)["entries"][0]["text"] == "先核验主体"
