from tests.test_personal_agent_routing import sessions, AgentContext, admit
from tests.test_personal_agent import personal_agent_on


def test_the_turn_prefix_stays_cacheable(sessions, monkeypatch):
    """The system prompt is identical across turns; the per-turn context follows the current message."""
    from core.db import engine as db_engine
    from personal_agent.conversation import front
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        first = admit(svc, "你好", "hi")
        svc.conversation.append(svc.identity.agent(), "assistant", "你好！", None, reply_to=first["message_id"]); db.commit()
        second = admit(svc, "再见", "bye")
        assert front.system_prompt(first["message_id"]) == front.system_prompt(second["message_id"])
        assert "Current time" not in front.system_prompt(second["message_id"])
        assert front.turn_context(second["message_id"]).startswith(front.CONTEXT_OPEN)
        rows = front.with_context(front.history(db, second["message_id"]), "<ctx>")
        assert rows[-1]["content"] == "再见\n\n<ctx>"
        # Earlier messages are a prefix of what the previous turn sent.
        assert front.history(db, second["message_id"])[0] == front.history(db, first["message_id"])[0]
