from core.auth.public_identity import resolve_public_user
"""Public links cannot inherit cookies or cross visitor/resource boundaries."""

import asyncio
from types import SimpleNamespace

import pytest
from core.chat.schemas import ChatRequest
from core.auth.backend import get_current_user, require_auth
from core.auth.public_chat import authenticate_public_chat, public_session_matches
from core.db.engine import Base
from core.db.models import (
    AgentApiCallLog,
    Artifact,
    ChatRun,
    ChatSession,
    ContentBlock,
    LocalUser,
    Project,
    Team,
    TeamMember,
    UserAgent,
    UserApiKey,
    UserFolder,
    UserShadow,
)
from core.chat import public as service
from core.auth.agent_api import prepare_agent_api_request
from core.projects.api import prepare_project_api_request
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from tests.db_support import share_session


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'public.db'}")
    models = (
        UserShadow,
        LocalUser,
        UserAgent,
        UserApiKey,
        ContentBlock,
        ChatSession,
        ChatRun,
        AgentApiCallLog,
        Project,
        Team,
        TeamMember,
        Artifact,
        UserFolder,
    )
    Base.metadata.create_all(engine, tables=[m.__table__ for m in models])
    session = sessionmaker(bind=engine)()
    session.add(UserShadow(user_id="owner", username="Owner"))
    session.add(UserShadow(user_id="other", username="Other"))
    session.add(
        UserAgent(
            agent_id="agent",
            user_id="owner",
            owner_type="user",
            name="Assistant",
            description="Hello",
        )
    )
    session.add(
        Project(
            project_id="project",
            name="Project",
            kind="personal",
            owner_user_id="owner",
            description="Hello",
        )
    )
    session.commit()
    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *_: {"can_use_api_key": True}
    )
    for module in (service, __import__("core.infra.crypto", fromlist=["encrypt_secret"])):
        monkeypatch.setattr(module, "encrypt_secret", lambda value: "encrypted:" + value)
        monkeypatch.setattr(
            module, "decrypt_secret", lambda value: value.removeprefix("encrypted:")
        )
    monkeypatch.setattr("core.auth.public_identity.decrypt_secret", lambda value: value.removeprefix("encrypted:"))
    share_session(monkeypatch, session)
    yield session
    session.close()
    engine.dispose()


def request(token, visitor, path, method="GET"):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"x-public-chat", token.encode()),
                (b"x-public-visitor", visitor.encode()),
                (b"cookie", b"jx_session=owner-cookie"),
            ],
            "query_string": b"",
        }
    )


@pytest.mark.parametrize("kind,resource", [("agents", "agent"), ("projects", "project")])
def test_link_lifecycle_and_visitor_isolation(db, kind, resource):
    link = service.create_link(db, "owner", kind, resource)
    assert service.create_link(db, "owner", kind, resource) == link
    assert db.query(UserApiKey).count() == 1
    first = resolve_public_user(db, link["token"], "a" * 48)
    second = resolve_public_user(db, link["token"], "b" * 48)
    assert first.public_chat_visitor != second.public_chat_visitor
    assert not hasattr(first, "api_key")
    body = ChatRequest(
        chat_id="visitor-chat",
        message="Hi",
        stream=True,
        **({"agent_id": resource} if kind == "agents" else {"project_id": resource}),
    )
    if kind == "agents":
        prepare_agent_api_request(db, first, body)
    else:
        prepare_project_api_request(db, first, body)
    chat = db.get(ChatSession, body.chat_id)
    assert public_session_matches(chat, first)
    assert not public_session_matches(chat, second)
    allowed = request(link["token"], "a" * 48, "/v1/chats/visitor-chat/messages")
    assert authenticate_public_chat(allowed, db).public_chat_visitor == first.public_chat_visitor
    for path in (
        "/v1/chats/visitor-chat/messages",
        "/v1/me",
        "/v1/chats",
        "/v1/projects/project/files",
        "/v1/agents/agent/api-keys",
    ):
        with pytest.raises(HTTPException):
            authenticate_public_chat(request(link["token"], "b" * 48, path), db)
    # Supplying the public credential must beat even an existing owner cookie.
    resolved = asyncio.run(get_current_user(allowed, None))
    assert resolved.public_chat_visitor == first.public_chat_visitor
    optional = asyncio.run(require_auth(False)(allowed))
    assert optional.public_chat_visitor == first.public_chat_visitor
    service.set_enabled(db, "owner", kind, resource, False)
    with pytest.raises(HTTPException):
        asyncio.run(get_current_user(allowed, None))
    assert service.get_link(db, "owner", kind, resource)["enabled"] is False
    service.set_enabled(db, "owner", kind, resource, True)
    assert resolve_public_user(db, link["token"], "a" * 48)


def test_cannot_publish_someone_elses_resource(db):
    with pytest.raises(HTTPException):
        service.create_link(db, "other", "agents", "agent")
    with pytest.raises(HTTPException):
        service.create_link(db, "other", "projects", "project")


def test_bootstrap_returns_only_current_visitor(db):
    from api.routes.v1.public_chats import bootstrap

    link = service.create_link(db, "owner", "agents", "agent")
    first = resolve_public_user(db, link["token"], "a" * 48)
    second = resolve_public_user(db, link["token"], "b" * 48)
    for user, chat_id in ((first, "first"), (second, "second")):
        prepare_agent_api_request(
            db, user, ChatRequest(chat_id=chat_id, message="Hi", agent_id="agent")
        )
    result = bootstrap(Response(), first, db)
    assert result["data"]["name"] == "Assistant"
    assert [c["chat_id"] for c in result["data"]["chats"]] == ["first"]
    assert "system_prompt" not in result["data"]


def test_public_agent_artifact_roundtrip(db, monkeypatch):
    from core.llm.api_runtime.agent import artifact_in_scope, parse_api_scope
    from core.auth.agent_api import make_agent_api_scope

    link = service.create_link(db, "owner", "agents", "agent")
    user = resolve_public_user(db, link["token"], "a" * 48)
    prepare_agent_api_request(
        db, user, ChatRequest(chat_id="files-chat", message="Hi", agent_id="agent")
    )
    db.add(
        Artifact(
            artifact_id="file-one",
            user_id="owner",
            chat_id="files-chat",
            filename="one.txt",
            mime_type="text/plain",
            storage_key="one.txt",
            type="document",
            title="One",
            size_bytes=5,
        )
    )
    db.commit()
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=db.bind))
    scope = parse_api_scope(
        make_agent_api_scope(user, "files-chat"),
        owner_user_id="owner",
        chat_id="files-chat",
        agent_id="agent",
    )
    assert artifact_in_scope("file-one", scope)


@pytest.mark.parametrize("kind,resource", [("agents", "agent"), ("projects", "project")])
def test_public_composer_capabilities_and_uploads(db, kind, resource):
    link = service.create_link(db, "owner", kind, resource)
    user = resolve_public_user(db, link["token"], "a" * 48)
    other = resolve_public_user(db, link["token"], "b" * 48)
    for path, method in (("/v1/models/capabilities", "GET"),
                         ("/v1/tool-approval", "GET"), ("/v1/tool-approval", "PUT"),
                         ("/v1/file/upload", "POST")):
        assert authenticate_public_chat(request(link["token"], "a" * 48, path, method), db)
    metadata = service.prepare_visitor_upload(db, user, "upload-chat")
    assert metadata["public_chat_visitor"] == user.public_chat_visitor
    assert public_session_matches(db.get(ChatSession, "upload-chat"), user)
    with pytest.raises(HTTPException):
        service.prepare_visitor_upload(db, other, "upload-chat")
    with pytest.raises(HTTPException):
        service.prepare_visitor_upload(db, user, "another", folder_id="folder")
    file = Artifact(artifact_id="uploaded", user_id="owner", chat_id="upload-chat",
                    filename="hello.txt", mime_type="text/plain", storage_key="hello",
                    type="document", title="Hello", size_bytes=5, extra_data=metadata)
    db.add(file)
    db.commit()
    body = ChatRequest(chat_id="upload-chat", message="Read it", attachments=[
        {"file_id": "uploaded", "name": "hello.txt", "mime_type": "text/plain"}],
        model_provider_id="selected-model",
        **({"agent_id": resource} if kind == "agents" else {"project_id": resource}))
    if kind == "agents":
        prepare_agent_api_request(db, user, body)
    else:
        prepare_project_api_request(db, user, body)
    service.validate_visitor_attachments(db, user, body.attachments)
    with pytest.raises(HTTPException):
        service.validate_visitor_attachments(db, other, body.attachments)
    for path in ("/files/uploaded", "/files/uploaded/text", "/files/uploaded/preview",
                 "/v1/chats/upload-chat/pending-confirm"):
        assert authenticate_public_chat(request(link["token"], "a" * 48, path), db)
        with pytest.raises(HTTPException):
            authenticate_public_chat(request(link["token"], "b" * 48, path), db)
    assert authenticate_public_chat(
        request(link["token"], "a" * 48, "/v1/chats/upload-chat/file-confirm", "POST"), db)


def test_public_approval_preferences_are_independent(db, monkeypatch):
    from api.routes.v1.tool_approval import ApprovalModeBody, get_tool_approval_mode, set_tool_approval_mode
    from core.auth.users import UserService

    link = service.create_link(db, "owner", "projects", "project")
    first = resolve_public_user(db, link["token"], "a" * 48)
    second = resolve_public_user(db, link["token"], "b" * 48)
    monkeypatch.setattr(UserService, "get_user_settings", lambda *_: {"tool_approval_mode": "auto"})
    def must_not_change_owner(*args, **kwargs):
        raise AssertionError("visitor must not change the owner's global setting")
    monkeypatch.setattr(UserService, "update_user_metadata", must_not_change_owner)
    assert get_tool_approval_mode(first, db)["data"]["mode"] == "auto"
    set_tool_approval_mode(ApprovalModeBody(mode="full"), first, db)
    assert get_tool_approval_mode(first, db)["data"]["mode"] == "full"
    assert get_tool_approval_mode(second, db)["data"]["mode"] == "auto"
    set_tool_approval_mode(ApprovalModeBody(mode="ask"), second, db)
    assert get_tool_approval_mode(first, db)["data"]["mode"] == "full"
    assert get_tool_approval_mode(second, db)["data"]["mode"] == "ask"
