"""Project recovery keeps durable key/project provenance and normal capabilities."""

from copy import deepcopy
from types import SimpleNamespace

import core.chat.runtime.recovery as runtime_recovery
import core.chat.runtime.recovery_snapshot as recovery_snapshot
import core.db.engine as db_engine
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import pytest
from core.db.engine import Base
from core.db.models import ChatRun, ChatSession
from core.runs.api_recovery import validate_recovery_context
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'project-recovery.db'}")
    Base.metadata.create_all(engine, tables=[ChatSession.__table__, ChatRun.__table__])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    scope = {
        "version": 1,
        "owner_user_id": "owner",
        "api_key_id": "project-key",
        "project_id": "one",
        "chat_id": "project-chat",
    }
    context = {
        "user_id": "owner",
        "chat_id": "project-chat",
        "project_id": "one",
        "project_api_scope": scope,
        "memory_enabled": True,
        "allow_bash": True,
    }
    run = ChatRun(
        run_id="project-run",
        chat_id="project-chat",
        user_id="owner",
        message_id="reply",
        status="pending",
        request_payload={
            "api_key_id": "project-key",
            "project_id": "one",
            "project_api_scope": scope,
        },
    )
    with sessions() as db:
        db.add(
            ChatSession(
                chat_id="project-chat",
                user_id="owner",
                project_id="one",
                title="Project",
                extra_data={"project_api_scope": scope},
            )
        )
        db.add(run)
        db.commit()
    yield sessions, run, deepcopy(context)
    engine.dispose()


def validate(sessions, run, context):
    with sessions() as db:
        return validate_recovery_context(db, run, context, user_id="owner", chat_id="project-chat")


def test_restore_preserves_normal_project_capabilities(recovery):
    sessions, run, context = recovery
    assert validate(sessions, run, context) == context
    assert context["allow_bash"] and context["memory_enabled"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_context_scope",
        "wrong_context_project",
        "wrong_context_owner",
        "wrong_context_chat",
        "missing_payload_scope",
        "wrong_payload_project",
        "wrong_key",
        "missing_session_scope",
        "wrong_session_project",
        "deleted_session",
        "wrong_version",
        "mixed_agent_scope",
    ],
)
def test_restore_rejects_lost_or_drifted_project_provenance(recovery, damage):
    sessions, run, context = recovery
    run.request_payload = deepcopy(run.request_payload)
    if damage == "missing_context_scope":
        context.pop("project_api_scope")
    elif damage == "wrong_context_project":
        context["project_id"] = "two"
    elif damage == "wrong_context_owner":
        context["user_id"] = "someone-else"
    elif damage == "wrong_context_chat":
        context["chat_id"] = "other-chat"
    elif damage == "missing_payload_scope":
        run.request_payload.pop("project_api_scope")
    elif damage == "wrong_payload_project":
        run.request_payload["project_id"] = "two"
    elif damage == "wrong_key":
        run.request_payload["api_key_id"] = "other-key"
    elif damage == "wrong_version":
        run.request_payload["project_api_scope"]["version"] = 2
    elif damage == "mixed_agent_scope":
        context["agent_api_scope"] = {}
    else:
        with sessions() as db:
            session = db.get(ChatSession, "project-chat")
            if damage == "missing_session_scope":
                session.extra_data = {}
            elif damage == "wrong_session_project":
                session.project_id = "two"
            else:
                from datetime import datetime, timezone

                session.deleted_at = datetime.now(timezone.utc)
            db.commit()
    with pytest.raises(ValueError):
        validate(sessions, run, context)


def decision(context):
    return SimpleNamespace(
        run_id="project-run",
        user_id="owner",
        chat_id="project-chat",
        message_id="reply",
        snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [],
                "effective_user_message": "Hi",
                "raw_user_message": "Hi",
                "context": context,
            },
        },
    )


def test_worker_restore_rejects_drift_before_starting_worker(recovery, monkeypatch):
    _, _, context = recovery
    context["project_id"] = "two"
    monkeypatch.setattr(
        runtime, 'register_run_task', lambda *a, **k: pytest.fail("must not start")
    )
    assert runtime_recovery._register_recovered_chat(decision(context)) is False


@pytest.mark.asyncio
async def test_completed_snapshot_rejects_missing_scope_before_committing(recovery, monkeypatch):
    _, _, context = recovery
    parked = []
    monkeypatch.setattr(
        ownership,
        'journal',
        lambda: SimpleNamespace(
            needs_attention=lambda *a, **k: parked.append(k) or True,
            claim=lambda *a, **k: pytest.fail("must not claim damaged snapshot"),
        ),
    )
    damaged = decision(context)
    damaged.snapshot.update(
        assistant_content="answer",
        context={
            "user_id": "owner",
            "chat_id": "project-chat",
            "project_id": "two",
        },
    )
    assert await recovery_snapshot._commit_recovered_chat_snapshot(damaged) is False
    assert parked
