"""Display projection and keyset pagination preserve the canonical transcript."""

import json
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import Session
from core.db.models import ChatSession, ChatMessage


def test_display_page_bounds_transfer_and_cursor_is_stable(tmp_path):
    from core.db.history_page import history_page

    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    full = [{"id": "tool-1", "result": {"text": "长" * 1_000_000, "items": [1, 2]}}]
    with Session(engine) as db:
        db.add(ChatSession(chat_id="c", user_id="u", title="history"))
        db.flush()
        for seq in range(1, 7):
            db.add(
                ChatMessage(
                    message_id=f"m{seq}",
                    chat_id="c",
                    chat_seq=seq,
                    role="assistant",
                    content=str(seq),
                    tool_calls=full,
                    model_steps=full,
                )
            )
        db.commit()
        statements = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda c, cur, sql, p, ctx, many: statements.append(sql),
        )
        rows, has_more, cursor = history_page(db, "c", limit=2)
        assert [r.chat_seq for r in rows] == [5, 6]
        assert has_more and cursor == 5
        assert len(statements) == 1 and "count(" not in statements[0].lower()
        assert "model_steps" not in statements[0]
        assert len(json.dumps(rows[0].tool_calls, ensure_ascii=False)) < 3000
        assert rows[0].tool_calls[0]["result"]["items"] == [1, 2]
        assert rows[0].tool_calls[0]["result_truncated"]
        db.add(ChatMessage(message_id="m7", chat_id="c", chat_seq=7, role="user", content="new"))
        db.commit()
        rows, has_more, cursor = history_page(db, "c", limit=2, before_seq=cursor)
        assert [r.chat_seq for r in rows] == [3, 4] and cursor == 3
        db.execute(
            update(ChatMessage)
            .where(ChatMessage.message_id == "m3")
            .values(tool_calls=[{"result": "updated"}])
        )
        db.commit()
        rows, _, _ = history_page(db, "c", limit=1, before_seq=4)
        assert rows[0].tool_calls == [{"result": "updated"}]
        assert len(db.get(ChatMessage, "m5").tool_calls[0]["result"]["text"]) == 1_000_000


def test_display_projection_invalidates_on_other_updates_and_backfills(tmp_path):
    from core.db.history_projection import backfill_display

    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    with Session(engine) as db:
        db.add(ChatSession(chat_id="c", user_id="u", title="x"))
        db.flush()
        db.add(
            ChatMessage(
                message_id="m",
                chat_id="c",
                role="assistant",
                content="x",
                tool_calls=[{"result": "x" * 5000}],
            )
        )
        db.commit()
        db.execute(update(ChatMessage).values(content="changed"))
        db.commit()
        assert db.get(ChatMessage, "m").tool_calls_display is None
        assert backfill_display(db, batch_size=1) == 1
        assert db.get(ChatMessage, "m").tool_calls_display[0]["result_truncated"]


def test_backfill_interruption_keeps_committed_batches(tmp_path):
    import pytest
    from core.db.history_projection import backfill_display

    engine = create_engine(f"sqlite:///{tmp_path/'resume.db'}")
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    with Session(engine) as db:
        db.add(ChatSession(chat_id="c", user_id="u", title="x"))
        db.flush()
        for i in range(3):
            db.add(
                ChatMessage(
                    message_id=f"m{i}",
                    chat_id="c",
                    chat_seq=i + 1,
                    role="assistant",
                    content="x",
                    tool_calls=[{"result": "x" * 5000}],
                )
            )
        db.commit()
        db.execute(update(ChatMessage).values(tool_calls_display=None))
        db.commit()
    writes = []

    def interrupt(conn, cur, sql, params, ctx, many):
        if sql.startswith("UPDATE chat_messages"):
            writes.append(sql)
            if len(writes) == 2:
                raise RuntimeError("interrupted")

    event.listen(engine, "before_cursor_execute", interrupt)
    with Session(engine) as db:
        with pytest.raises(RuntimeError, match="interrupted"):
            backfill_display(db, batch_size=1)
    event.remove(engine, "before_cursor_execute", interrupt)
    with Session(engine) as db:
        assert (
            db.query(ChatMessage).filter(ChatMessage.tool_calls_display.is_not(None)).count() == 1
        )
        assert backfill_display(db, batch_size=1) == 2
