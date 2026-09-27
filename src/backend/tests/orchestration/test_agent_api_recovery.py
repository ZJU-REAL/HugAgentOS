"""API recovery cannot widen an accepted run into its owner's private context."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.auth.backend import UserContext
from core.db.engine import Base
from core.db.models import ChatRun, ChatSession
from core.services.agent_api_service import make_agent_api_scope
from orchestration.agent_api_recovery import validate_recovery_context
from orchestration import chat_run_executor as executor


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'scope-recovery.db'}")
    Base.metadata.create_all(engine, tables=[ChatSession.__table__, ChatRun.__table__])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(executor, "SessionLocal", sessions)
    scope = make_agent_api_scope(
        UserContext(
            user_id="owner",
            user_center_id="owner",
            username="Owner",
            api_key_id="ak_scope",
            api_key_agent_id="ua_agent",
        ),
        "api-chat",
    )
    context = {
        "agent_api_scope": scope,
        "chat_id": "api-chat",
        "user_id": "owner",
        "direct_agent_id": "ua_agent",
        "agent_id": "ua_agent",
        "memory_enabled": True,
        "memory_write_enabled": True,
        "visible_subagents": ["private"],
    }
    run = ChatRun(
        run_id="run-api",
        chat_id="api-chat",
        user_id="owner",
        message_id="msg-api",
        status="pending",
        request_payload={"agent_id": "ua_agent", "agent_api_scope": scope},
    )
    with sessions() as db:
        db.add(
            ChatSession(
                chat_id="api-chat",
                user_id="owner",
                title="API",
                extra_data={"agent_id": "ua_agent", "agent_api_scope": scope},
            )
        )
        db.add(run)
        db.commit()
    yield sessions, run, deepcopy(context)
    engine.dispose()


def _validate(sessions, run, context):
    with sessions() as db:
        return validate_recovery_context(db, run, context, user_id="owner", chat_id="api-chat")


def test_recovery_reapplies_private_context_restrictions(recovery):
    sessions, run, context = recovery
    restored = _validate(sessions, run, context)
    assert restored["agent_api_scope"] == context["agent_api_scope"]
    assert restored["memory_enabled"] is False
    assert restored["memory_write_enabled"] is False
    assert restored["visible_subagents"] == []
    assert restored["sandbox_session_id"] == context["agent_api_scope"]["sandbox_session_id"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_payload",
        "missing_context",
        "both_missing",
        "wrong_key",
        "wrong_agent",
        "wrong_owner",
        "wrong_chat",
        "owner_sandbox",
        "wrong_session",
        "session_deleted",
        "wrong_version",
        "missing_agent_id",
        "missing_direct_agent_id",
    ],
)
def test_missing_or_changed_provenance_fails_closed(recovery, damage):
    sessions, run, context = recovery
    run.request_payload = deepcopy(run.request_payload)
    if damage in ("missing_payload", "both_missing"):
        run.request_payload.pop("agent_api_scope")
    if damage in ("missing_context", "both_missing"):
        context.pop("agent_api_scope")
    if damage == "wrong_key":
        context["agent_api_scope"]["api_key_id"] = "ak_other"
    if damage == "wrong_agent":
        context["direct_agent_id"] = "ua_other"
    if damage == "missing_agent_id":
        context.pop("agent_id")
    if damage == "missing_direct_agent_id":
        context.pop("direct_agent_id")
    if damage == "wrong_owner":
        context["user_id"] = "other"
    if damage == "wrong_chat":
        context["chat_id"] = "private"
    if damage in ("owner_sandbox", "wrong_version"):
        field, value = ("sandbox_user_id", "owner") if damage == "owner_sandbox" else ("version", 2)
        # Even three mutually consistent markers must satisfy the runtime schema.
        run.request_payload["agent_api_scope"][field] = value
        context["agent_api_scope"][field] = value
        with sessions() as db:
            session = db.get(ChatSession, "api-chat")
            session.extra_data = {
                "agent_id": "ua_agent",
                "agent_api_scope": deepcopy(context["agent_api_scope"]),
            }
            db.commit()
    if damage in ("wrong_session", "session_deleted"):
        with sessions() as db:
            session = db.get(ChatSession, "api-chat")
            if damage == "wrong_session":
                session.extra_data = {}
            else:
                from datetime import datetime, timezone

                session.deleted_at = datetime.now(timezone.utc)
            db.commit()
    with pytest.raises(ValueError):
        _validate(sessions, run, context)


def _decision(context):
    return SimpleNamespace(
        run_id="run-api",
        user_id="owner",
        chat_id="api-chat",
        message_id="msg-api",
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


def test_worker_restore_passes_only_validated_context(recovery, monkeypatch):
    _, _, context = recovery
    observed = {}

    def workflow(**kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(executor, "_run_workflow", workflow)
    monkeypatch.setattr(executor, "_register_run_task", lambda *a, **k: None)
    assert executor._register_recovered_chat(_decision(context)) is True
    assert observed["context"]["memory_enabled"] is False
    assert observed["context"]["agent_api_scope"] == context["agent_api_scope"]


def test_worker_restore_refuses_lost_scope_before_start(recovery, monkeypatch):
    _, _, context = recovery
    context.pop("agent_api_scope")
    monkeypatch.setattr(
        executor,
        "_register_run_task",
        lambda *a, **k: pytest.fail("damaged scope must not create a worker"),
    )
    assert executor._register_recovered_chat(_decision(context)) is False


@pytest.mark.asyncio
async def test_completed_snapshot_lost_scope_parks_without_writing(recovery, monkeypatch):
    _, _, context = recovery
    decisions = []
    journal = SimpleNamespace(
        needs_attention=lambda run_id, **kwargs: decisions.append((run_id, kwargs)) or True,
        claim=lambda *a, **k: pytest.fail("must reject before claiming or committing"),
    )
    monkeypatch.setattr(executor, "_journal", lambda: journal)
    decision = _decision(context)
    decision.snapshot.update(assistant_content="answer", context={})
    assert await executor._commit_recovered_chat_snapshot(decision) is False
    assert decisions[0][0] == "run-api"


@pytest.mark.asyncio
async def test_completed_snapshot_checks_worker_context_too(recovery, monkeypatch):
    _, _, context = recovery
    decisions = []
    monkeypatch.setattr(
        executor,
        "_journal",
        lambda: SimpleNamespace(
            needs_attention=lambda *a, **k: decisions.append(k) or True,
            claim=lambda *a, **k: pytest.fail("must reject a mismatched worker snapshot"),
        ),
    )
    decision = _decision({})
    decision.snapshot.update(assistant_content="answer", context=context)
    assert await executor._commit_recovered_chat_snapshot(decision) is False
    assert decisions


def test_ordinary_legacy_recovery_preserves_context(recovery):
    sessions, run, _ = recovery
    run.request_payload = {"kind": "chat"}
    with sessions() as db:
        db.get(ChatSession, "api-chat").extra_data = {}
        db.commit()
    assert _validate(sessions, run, None) == {}
    assert _validate(sessions, run, {"memory_enabled": True}) == {"memory_enabled": True}
