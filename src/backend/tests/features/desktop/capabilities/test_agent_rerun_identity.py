"""Actual rerun routes keep dedicated-agent identity and reject revoked sources."""
from features.desktop.bridge import identity as bridge_identity
from features.desktop.capabilities import skill_account

from types import SimpleNamespace

import api.routes.v1.chats.reruns as chat_reruns
import api.transports.run_sse as run_sse
import core.chat.context as chat_context
import core.chat.runtime.admission as admission
import core.db.engine as db_engine
import core.infra.responses as responses
import core.auth.users as chat_services_UserService_module
import core.chat.operations.agent_targets as chat_agent_targets
import core.chat.operations.request_context as chat_request_context
import core.chat.operations.schemas as chat_models
import core.chat.operations.session_context as chat_session_context
import pytest
from core.auth.backend import UserContext
from features.desktop.capabilities import agents, registry, runtime, skills, store
from features.desktop.capabilities.paths import revision_for_hash
from core.capabilities.reference import cloud_ref, profile_id
from core.chat.schemas import ChatRequest
from core.db.models import ChatMessage, ChatRun, ChatSession
from features.desktop.bridge import client as bridge
from core.chat.service import ChatService
from fastapi import HTTPException
from tests.features.desktop.capabilities.test_runtime_recovery import state


@pytest.fixture
def rerun(index_db, caps_root, monkeypatch):
    st = state("cloud-a")
    current = [st]
    profile = profile_id(st["cloud_base"], "cloud-a")
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: current[0])
    monkeypatch.setattr(bridge_identity, 'ensure_current_authorization', lambda: None)
    monkeypatch.setattr(skill_account, 'current_local_user_id', lambda: "owner")
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    monkeypatch.setattr(db_engine, "SessionLocal", index_db)

    def publish(profile, cloud_base=st["cloud_base"]):
        definition = agents.AgentDefinition(
            agent_id="saved-agent",
            name="Saved agent",
            system_prompt="OLD_DEFINITION_CANARY",
            profile=profile,
            origin="cloud",
        )
        digest = definition.content_hash()
        inst = registry.upsert(
            profile_id=profile,
            ref=cloud_ref(cloud_base, "agent", "saved-agent", scope="private"),
            content_hash=digest,
            payload={"owner_user_id": "owner"},
        )
        comp = store.write_from_files(
            "agent", profile, inst.key, revision_for_hash(digest), definition.to_files()
        )
        registry.set_state(inst.install_id, "ready", resolved_revision=comp.revision)
        return inst, definition

    inst, definition = publish(profile)
    with index_db() as db:
        for model in (ChatSession, ChatMessage, ChatRun):
            model.__table__.create(db.get_bind(), checkfirst=True)
        db.add(
            ChatSession(
                chat_id="rerun",
                user_id="owner",
                title="Synthetic",
                extra_data={"agent_id": "saved-agent"},
            )
        )
        db.commit()
        svc = ChatService(db)
        question = svc.add_message(chat_id="rerun", role="user", content="original task")
        answer = svc.add_message(chat_id="rerun", role="assistant", content="original answer")
        question_id, answer_id = question.message_id, answer.message_id

    class UserService:
        def __init__(self, db):
            pass

        def get_disabled_builtin_subagent_ids(self, user_id):
            return set()

        def get_user_settings(self, user_id):
            return {}

    class AgentService:
        def __init__(self, db):
            pass

        def list_for_user(self, user_id):
            return agents.merge_visible(
                user_id, [{"agent_id": "local-agent", "name": "Local agent", "is_enabled": True}]
            )

        def get_by_id(self, agent_id, user_id=None):
            return next(row for row in self.list_for_user(user_id) if row["agent_id"] == agent_id)

    monkeypatch.setattr('core.auth.users.UserService', UserService)
    monkeypatch.setattr(chat_services_UserService_module, "UserService", UserService)
    monkeypatch.setattr('core.capabilities.agents.edition.UserAgentService', AgentService)
    monkeypatch.setattr(chat_session_context, 'ensure_main_model_configured', lambda: None)
    monkeypatch.setattr(
        chat_session_context, 'resolve_selected_model_provider_id', lambda *a: None
    )
    monkeypatch.setattr(
        chat_session_context, 'resolve_actual_chat_model_name', lambda *a: "synthetic-model"
    )
    monkeypatch.setattr(chat_context, "resolve_enabled_capabilities", lambda *a: ([], [], []))
    monkeypatch.setattr(chat_session_context, 'load_session_messages', lambda *a: [])
    monkeypatch.setattr(responses, "sse_response", lambda value: value)
    pass

    monkeypatch.setattr(run_sse, "follow_run_as_sse", lambda *a, **kw: None)
    captured = {}

    def context(request, *a, **kw):
        captured["request"] = request
        return {"agent_id": request.agent_id, "mention_agent_id": request.mention_agent_id}

    monkeypatch.setattr(chat_request_context, 'build_ctx', context)

    async def start(**kw):
        captured.update(kw)
        return kw["accepted_run"]

    monkeypatch.setattr(admission, "start_run", start)

    def metadata(session=None, message=None):
        with index_db() as db:
            if session is not None:
                db.get(ChatSession, "rerun").extra_data = session
            if message is not None:
                db.get(ChatMessage, question_id).extra_data = message
            db.commit()

    def source_snapshot():
        runtime.pin_agent_definition("source-run", "owner", definition)
        with index_db() as db:
            db.add(
                ChatRun(
                    run_id="source-run",
                    chat_id="rerun",
                    user_id="owner",
                    message_id=answer_id,
                    user_message_id=question_id,
                    status="completed",
                )
            )
            db.commit()

    async def call(operation):
        user = UserContext(
            user_id="owner", username="synthetic", user_center_id="center-cloud-a"
        )
        if operation == "regenerate":
            return await chat_reruns.regenerate_message(
                "rerun", chat_models.RegenerateRequest(message_index=1), user=user
            )
        return await chat_reruns.edit_and_resend(
            "rerun",
            chat_models.EditAndResendRequest(message_index=0, new_content="edited task"),
            user=user,
        )

    return SimpleNamespace(
        call=call,
        metadata=metadata,
        captured=captured,
        Session=index_db,
        profile=profile,
        current=current,
        inst=inst,
        source_snapshot=source_snapshot,
        publish=publish,
    )


@pytest.mark.parametrize("operation", ["regenerate", "edit"])
@pytest.mark.parametrize("source", ["session-local", "message-cloud", "old-run-cloud", "mention"])
async def test_rerun_preserves_proven_agent_identity_and_mention_boundary(rerun, operation, source):
    if source == "session-local":
        rerun.metadata(session={"agent_id": "local-agent"})
        expected = "local-agent"
    elif source == "message-cloud":
        rerun.metadata(message={"agent_id": "saved-agent", "agent_profile": rerun.profile})
        expected = "saved-agent"
    elif source == "old-run-cloud":
        rerun.source_snapshot()
        expected = "saved-agent"
    else:
        rerun.metadata(
            session={},
            message={
                "mention_agent_id": "saved-agent",
                "mention_name": "Saved agent",
                "mention_agent_profile": rerun.profile,
            },
        )
        expected = None
    await rerun.call(operation)
    request = rerun.captured["request"]
    assert request.agent_id == expected
    assert request.mention_agent_id == ("saved-agent" if source == "mention" else None)
    assert "OLD_DEFINITION_CANARY" not in str(request.model_dump())
    if operation == "edit" and expected:
        with rerun.Session() as db:
            row = (
                db.query(ChatMessage)
                .filter_by(role="user")
                .order_by(ChatMessage.chat_seq.desc())
                .first()
            )
            assert row.extra_data["agent_id"] == expected


@pytest.mark.parametrize("operation", ["regenerate", "edit"])
@pytest.mark.parametrize("change", ["revoke", "actor", "account-same-id", "unproven-cloud"])
async def test_rerun_rejects_unproven_or_changed_cloud_identity_before_history_mutation(
    rerun, monkeypatch, operation, change
):
    if change != "unproven-cloud":
        rerun.metadata(message={"agent_id": "saved-agent", "agent_profile": rerun.profile})
    if change == "revoke":
        registry.set_enabled(rerun.inst.install_id, False)
    elif change == "actor":
        monkeypatch.setattr(skill_account, 'current_local_user_id', lambda: "other-user")
    elif change == "account-same-id":
        st = state("cloud-b")
        other_profile = profile_id(st["cloud_base"], "cloud-b")
        rerun.publish(other_profile)
        rerun.current[0] = st
    with pytest.raises(HTTPException) as error:
        await rerun.call(operation)
    assert error.value.status_code in (403, 409)
    assert "accepted_run" not in rerun.captured
    with rerun.Session() as db:
        assert [m.content for m in db.query(ChatMessage).order_by(ChatMessage.chat_seq)] == [
            "original task",
            "original answer",
        ]


def test_new_user_message_saves_server_resolved_agent_profile(rerun):
    with rerun.Session() as db:
        request, _, _, _ = chat_agent_targets._resolve_chat_agent_targets(
            db, ChatRequest(chat_id="rerun", message="task", agent_id="saved-agent"), "owner"
        )
    extra = chat_request_context._build_user_extra_data(request)
    assert extra["agent_id"] == "saved-agent"
    assert extra["agent_profile"] == rerun.profile
