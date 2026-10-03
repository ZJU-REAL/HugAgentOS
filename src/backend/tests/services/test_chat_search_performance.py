"""Search contracts: visible text, tenant boundaries and bounded query count."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from core.db.models import ChatSession, ChatMessage
from core.db.repository.chat import ChatSessionRepository


def test_search_pages_in_database_without_n_plus_one(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'search.db'}")
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    with Session(engine) as db:
        for i in range(80):
            db.add(ChatSession(chat_id=f"c{i:03}", user_id="owner", title="conversation"))
            db.add(
                ChatMessage(
                    message_id=f"m{i}",
                    chat_id=f"c{i:03}",
                    role="assistant",
                    chat_seq=1,
                    content="visible needle answer",
                )
            )
        db.add(ChatSession(chat_id="private", user_id="other", title="needle private"))
        db.add(ChatSession(chat_id="hidden", user_id="owner", title="reasoning only"))
        db.add(
            ChatMessage(
                message_id="h",
                chat_id="hidden",
                role="assistant",
                chat_seq=1,
                content="needle</think>public answer",
            )
        )
        db.commit()
        statements = []
        event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        results, total = ChatSessionRepository(db).search(
            "owner", "needle", page=2, page_size=10, scope="all"
        )
        assert total == 80
        assert len(results) == 10
        assert all(
            r["match_type"] == "content" and "needle" in r["matched_snippet"] for r in results
        )
        assert len(statements) <= 3, f"query count grows with matches: {len(statements)}"
    engine.dispose()


def test_search_finds_visible_match_after_many_reasoning_matches(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    with Session(engine) as db:
        db.add(ChatSession(chat_id="c", user_id="owner", title="Legacy"))
        for i in range(22):
            db.add(
                ChatMessage(
                    message_id=str(i),
                    chat_id="c",
                    role="assistant",
                    chat_seq=i + 1,
                    content="needle</think>nothing visible" if i < 21 else "visible needle",
                )
            )
        db.commit()
        rows, total = ChatSessionRepository(db).search("owner", "needle", scope="all")
        assert total == 1
        assert rows[0]["matched_snippet"] == "visible needle"
    engine.dispose()
