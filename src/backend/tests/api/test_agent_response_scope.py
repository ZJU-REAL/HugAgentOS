"""Scoped admission must not interpret account-wide delegation commands."""

import api.routes.v1.chats.agent_targets as chat_agent_targets
import api.routes.v1.chats.request_context as chat_request_context
import api.routes.v1.chats.session_context as chat_session_context
import core.db.engine as db_engine
import pytest
from api.routes.v1 import agent_responses, chats
from api.schemas import ChatRequest
from core.db.models import ChatMessage, ChatRun, UserAgent
from core.services import agent_api_service
from sqlalchemy.orm import sessionmaker
from tests.api.test_agent_api_scope import _key, _user, db
from tests.api.test_agent_response_admission import _patch_common_chat_route


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("agent_enabled", [True, False])
async def test_scope_preserves_message_and_never_enumerates_other_agents(
    db, monkeypatch, stream, agent_enabled
):
    db.get(UserAgent, "ua_one").is_enabled = agent_enabled
    db.commit()
    key, _ = _key(db)
    ChatMessage.__table__.create(db.bind, checkfirst=True)
    _patch_common_chat_route(monkeypatch, chats)
    monkeypatch.setattr(db_engine, "SessionLocal", sessionmaker(bind=db.bind))
    monkeypatch.setattr(chat_session_context, "_load_session_messages", lambda *_: [])

    def forbidden_resolution(*args, **kwargs):
        raise AssertionError("Scoped requests must not resolve owner-wide agent commands")

    monkeypatch.setattr(chat_agent_targets, "_resolve_chat_agent_targets", forbidden_resolution)

    async def preferences(*args):
        return (["owner-skill"], ["other-agent"], ["owner-mcp"]), {
            "memory_enabled": True,
            "memory_write_enabled": True,
            "ontology_enabled": True,
        }

    monkeypatch.setattr(agent_responses, "_read_preferences", preferences)
    context_args = {}

    def context(*args, **kwargs):
        context_args.update(kwargs)
        return {"user_id": "owner", "chat_id": "api-chat", "agent_id": key.agent_id}

    monkeypatch.setattr(chat_request_context, "_build_ctx", context)

    captured = {}

    async def start(**kwargs):
        captured.update(kwargs)
        captured["payload"] = dict(kwargs["accepted_run"].request_payload)
        return kwargs["accepted_run"]

    monkeypatch.setattr(agent_responses.chat_run_executor, "start_run", start)

    message = "调用「Two」子智能体：原样保留的内容"
    result = await agent_responses._start_response_run(
        ChatRequest(chat_id="api-chat", message=message, stream=stream, agent_id="ua_one"),
        _user(key),
        db,
    )
    assert captured["effective_user_message"] == message
    assert captured["raw_user_message"] == message
    assert "explicit_subagent_command" not in captured["context"]
    assert "explicit_subagent_command" not in captured["payload"]
    scope = captured["context"]["agent_api_scope"]
    assert scope["agent_id"] == "ua_one"
    assert captured["payload"]["agent_api_scope"] == scope
    assert context_args["memory_enabled"] is False
    assert context_args["memory_write_enabled"] is False
    assert context_args["ontology_enabled"] is False
    run = db.get(ChatRun, result.run_id)
    assert run.request_payload["agent_api_scope"] == scope
    logs, total = agent_api_service.list_agent_api_calls(db, "owner", "ua_one")
    assert total == 1 and logs[0]["run_id"] == result.run_id
    assert logs[0]["http_status"] == (200 if stream else None)
