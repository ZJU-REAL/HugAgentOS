"""Retirement regression through real admission, schema, context and persistence.

Only the model-worker execution/transport boundary is controlled. This is not a
full model or sandbox end-to-end test.
"""

import asyncio
import hashlib
import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import api.transports.run_sse as run_sse
import core.chat.runtime.admission as admission
import core.runs.runtime as runtime
import pytest
from core.auth import session
from core.chat.schemas import ChatRequest
from core.config.settings import settings
from core.db.engine import Base, SessionLocal, engine
from core.db.models import (
    ChatMessage,
    ChatRun,
    ChatSession,
    ModelProvider,
    ModelRoleAssignment,
    Project,
    UserShadow,
)
from core.models.config import ModelConfigService
from fastapi.testclient import TestClient
from sqlalchemy.orm import object_session


@pytest.fixture()
def boundary(monkeypatch):
    from api.app import app
    pass

    assert settings.auth.mode == "session"
    assert session._use_memory_store()
    Base.metadata.create_all(engine)
    session.process_state().MEMORY_SESSIONS.clear()
    with SessionLocal() as db:
        db.add(UserShadow(user_id="retirement-user", username="Reader", extra_data={}))
        db.add(
            ModelProvider(
                provider_id="retirement-model",
                display_name="Controlled admission model",
                provider_type="chat",
                provider="openai_compatible",
                base_url="http://127.0.0.1:1/v1",
                api_key="isolated-controlled-model",
                model_name="controlled-admission-model",
                is_active=True,
            )
        )
        db.add(ModelRoleAssignment(role_key="main_agent", provider_id="retirement-model"))
        db.commit()
    ModelConfigService.get_instance().invalidate_cache()
    captured = []

    async def model_worker(**kwargs):
        # Every real admission/context step must complete before this boundary.
        context = kwargs["context"]
        assert "batch_chat" not in context
        run = kwargs["accepted_run"]
        assert "batch_chat" not in (run.request_payload or {})
        db = object_session(run)
        assert db is not None
        message = db.get(ChatMessage, run.message_id)
        assert message is not None
        message.content = "Controlled model response"
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        captured.append(kwargs)
        return SimpleNamespace(run_id=run.run_id, message_id=run.message_id)

    async def wait(run_id):
        with SessionLocal() as db:
            run = db.get(ChatRun, run_id)
            assert run.status == "completed"
            return SimpleNamespace(status=run.status, error_message=None)

    async def follow(run_id, *, chat_id):
        yield "event: run_started\ndata: " + json.dumps({"run_id": run_id}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(admission, "start_run", model_worker)
    monkeypatch.setattr(runtime, "wait_run", wait)
    monkeypatch.setattr(run_sse, "follow_run_as_sse", follow)
    client = TestClient(app)
    token = asyncio.run(
        session.create_session({"user_id": "retirement-user", "username": "Reader"})
    )
    client.cookies.set(settings.session.cookie_name, token)
    yield client, captured
    client.close()
    session.process_state().MEMORY_SESSIONS.clear()
    ModelConfigService.get_instance().invalidate_cache()
    Base.metadata.drop_all(engine)


@pytest.mark.parametrize("kind", ["json", "stream", "project-init"])
def test_ordinary_public_chat_and_project_init_survive_retirement(boundary, kind):
    client, captured = boundary
    body = {
        "chat_id": "ordinary-" + kind,
        "message": "ordinary question",
        "stream": kind == "stream",
        # A retired legacy flag cannot create or reactivate a batch conversation.
        "batch_chat": True,
    }
    if kind == "project-init":
        response = client.post(
            "/v1/projects", json={"name": "Retirement project", "kind": "personal"}
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["data"]["project_id"]
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            project.instructions = "Preserve the existing project instructions"
            db.commit()
        body.update(message="/init", project_id=project_id)
    response = client.post("/v1/agents/responses", json=body)
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    if kind == "stream":
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.endswith("data: [DONE]\n\n")
    else:
        assert response.json()["response"] == "Controlled model response"
    with SessionLocal() as db:
        chat = db.get(ChatSession, body["chat_id"])
        assert chat.user_id == "retirement-user"
        assert "batch_chat" not in (chat.extra_data or {})
        run = db.query(ChatRun).filter_by(chat_id=chat.chat_id).one()
        assert run.status == "completed"
        assert "batch_chat" not in run.request_payload
        if kind == "project-init":
            assert chat.project_id == project_id
            assert captured[0]["context"]["project_init"] is True
            assert (
                "Preserve the existing project instructions"
                in captured[0]["effective_user_message"]
            )


def test_scoped_runtime_wrappers_do_not_reintroduce_retired_fields():
    from core.llm.api_runtime.agent import apply_api_scope
    from features.evaluation.runtime.runtime import apply_evaluation_scope
    from core.auth.agent_api import _FORBIDDEN_INPUTS
    from core.chat.operations.session_context import _ensure_chat_session

    assert "batch_chat" not in ChatRequest.model_fields
    assert "batch_chat" not in inspect.signature(_ensure_chat_session).parameters
    assert "batch_chat" not in _FORBIDDEN_INPUTS
    sandbox = "api_" + hashlib.sha256(b"key:api-chat").hexdigest()[:40]
    api = apply_api_scope(
        {
            "user_id": "owner",
            "chat_id": "api-chat",
            "agent_id": "agent",
            "agent_api_scope": {
                "version": 1,
                "owner_user_id": "owner",
                "api_key_id": "key",
                "agent_id": "agent",
                "chat_id": "api-chat",
                "sandbox_user_id": "api_key",
                "sandbox_session_id": sandbox,
            },
            "project_id": "private",
            "plan_chat": True,
            "workflow_chat": True,
            "site_chat": True,
        }
    )
    evaluation = apply_evaluation_scope(
        {
            "user_id": "owner",
            "chat_id": "eval_" + "a" * 32,
            "project_id": "private",
            "plan_chat": True,
            "workflow_chat": True,
            "site_chat": True,
        }
    )
    for value in (api, evaluation):
        assert "batch_chat" not in value
        assert value["project_id"] is None
        assert value["memory_enabled"] is False
        assert value["plan_chat"] is False
        assert value["workflow_chat"] is False
        assert value["site_chat"] is False
