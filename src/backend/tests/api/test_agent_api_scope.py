"""Agent API credentials never inherit the owner's unrelated API sessions."""

from datetime import datetime, timedelta, timezone

import pytest
from api.schemas import ChatRequest
from core.auth.agent_api_scope import enforce_agent_api_route
from core.auth.backend import UserContext
from core.db.engine import Base
from core.db.models import (
    AgentApiCallLog,
    Artifact,
    ChatRun,
    ChatSession,
    LocalUser,
    Team,
    TeamMember,
    UserAgent,
    UserApiKey,
    UserShadow,
)
from core.services.agent_api_service import prepare_agent_api_request
from core.services.api_key_service import ApiKeyService, resolve_api_key_identity
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-api.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                UserShadow,
                LocalUser,
                Team,
                TeamMember,
                UserAgent,
                UserApiKey,
                ChatSession,
                ChatRun,
                AgentApiCallLog,
                Artifact,
            )
        ],
    )
    session = sessionmaker(bind=engine)()
    session.add(UserShadow(user_id="owner", username="Owner"))
    session.add(UserAgent(agent_id="ua_one", user_id="owner", owner_type="user", name="One"))
    session.add(UserAgent(agent_id="ua_two", user_id="owner", owner_type="user", name="Two"))
    session.commit()
    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *_: {"can_use_api_key": True}
    )
    monkeypatch.setattr("core.infra.crypto.encrypt_secret", lambda value: "encrypted")
    yield session
    session.close()


def _key(db, agent_id="ua_one"):
    return ApiKeyService(db).create_key("owner", "Client", agent_id=agent_id)


def _user(key):
    return UserContext(
        user_id="owner",
        user_center_id="owner",
        username="Owner",
        api_key_id=key.id,
        api_key_agent_id=key.agent_id,
    )


def _request(method, path):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "scheme": "http",
        }
    )


def test_scoped_key_is_separate_from_personal_keys_and_resolves_identity(db):
    key, token = _key(db)
    personal, _ = ApiKeyService(db).create_key("owner", "Personal")
    assert [row.id for row in ApiKeyService(db).list_keys("owner")] == [personal.id]
    owner, resolved = resolve_api_key_identity(db, token)
    assert owner.user_id == "owner" and resolved.id == key.id
    assert key.key_hash != token and key.key_enc != token


@pytest.mark.parametrize(
    "change",
    ["disabled_key", "revoked", "expired", "deleted_agent", "disabled_user"],
)
def test_key_validation_fails_closed(db, change):
    key, token = _key(db)
    if change == "disabled_key":
        key.enabled = False
    elif change == "revoked":
        key.revoked_at = datetime.now(timezone.utc)
    elif change == "expired":
        key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "deleted_agent":
        db.delete(db.get(UserAgent, "ua_one"))
    else:
        db.add(LocalUser(user_id="owner", password_hash="x", status="disabled"))
    db.commit()
    assert resolve_api_key_identity(db, token) is None


def test_disabled_agent_can_create_and_use_scoped_key(db):
    db.get(UserAgent, "ua_one").is_enabled = False
    db.commit()
    key, token = _key(db)
    assert resolve_api_key_identity(db, token)[1].id == key.id
    request, scope = prepare_agent_api_request(
        db, _user(key), ChatRequest(chat_id="disabled-chat", message="Hi", agent_id="ua_one")
    )
    assert request.agent_id == scope["agent_id"] == "ua_one"


def test_personal_key_can_target_disabled_owned_agent(db):
    from api.routes.v1.chats.agent_targets import _resolve_chat_agent_targets

    db.get(UserAgent, "ua_one").is_enabled = False
    db.commit()
    key, token = _key(db, agent_id=None)
    assert resolve_api_key_identity(db, token)[1].id == key.id
    request = ChatRequest(chat_id="personal-chat", message="Hi", agent_id="ua_one")
    prepared, scope = prepare_agent_api_request(db, _user(key), request)
    assert prepared is request and scope is None
    resolved, name, message, command = _resolve_chat_agent_targets(db, request, "owner")
    assert resolved.agent_id == "ua_one" and name == "One" and message == "Hi"
    assert command is None


def test_scoped_key_requires_explicit_agent_id(db):
    key, _ = _key(db)
    with pytest.raises(HTTPException) as exc:
        prepare_agent_api_request(db, _user(key), ChatRequest(chat_id="missing", message="Hi"))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "agent_id_required"


def test_prepare_binds_agent_and_an_isolated_api_session(db):
    key, _ = _key(db)
    prepared, scope = prepare_agent_api_request(
        db, _user(key), ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    assert prepared.agent_id == "ua_one"
    assert scope["version"] == 1
    assert scope["sandbox_user_id"] == f"api_{key.id}"
    assert scope["owner_user_id"] == "owner"
    assert db.get(ChatSession, "api-chat").extra_data["agent_api_scope"] == scope
    assert prepare_agent_api_request(db, _user(key), prepared)[1] == scope


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_id": "ua_two"},
        {"project_id": "p1"},
        {"referenced_chats": [{"chat_id": "private"}]},
        {"enabled_mcps": ["anything"]},
        {"workflow_chat": True},
        {"mention_agent_id": "ua_two"},
    ],
)
def test_prepare_rejects_privilege_expansion_and_records_no_body(db, payload):
    key, _ = _key(db)
    with pytest.raises(HTTPException):
        prepare_agent_api_request(
            db,
            _user(key),
            ChatRequest(chat_id="new", message="secret body", **({"agent_id": "ua_one"} | payload)),
        )
    record = db.query(AgentApiCallLog).one()
    assert record.status == "failed"
    assert "secret body" not in str(record.__dict__)
    assert db.get(ChatSession, "new") is None


def test_key_cannot_reuse_owner_chat_or_another_keys_chat(db):
    key, _ = _key(db)
    db.add(ChatSession(chat_id="private", user_id="owner", title="Private"))
    db.commit()
    with pytest.raises(HTTPException):
        prepare_agent_api_request(
            db, _user(key), ChatRequest(chat_id="private", message="Hi", agent_id="ua_one")
        )
    prepare_agent_api_request(
        db, _user(key), ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    other, _ = _key(db)
    with pytest.raises(HTTPException):
        prepare_agent_api_request(
            db, _user(other), ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
        )


def test_global_route_gate_allows_only_own_api_runs(db):
    key, _ = _key(db)
    user = _user(key)
    _, scope = prepare_agent_api_request(
        db, user, ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    db.add(
        ChatRun(
            run_id="own",
            chat_id="api-chat",
            user_id="owner",
            message_id="answer",
            request_payload={"agent_api_scope": scope},
        )
    )
    db.add(
        ChatRun(
            run_id="private-run",
            chat_id="api-chat",
            user_id="owner",
            message_id="other",
            request_payload={},
        )
    )
    db.commit()
    enforce_agent_api_route(_request("POST", "/v1/agents/responses"), db, user)
    enforce_agent_api_route(_request("GET", "/v1/chats/stream/own"), db, user)
    enforce_agent_api_route(_request("POST", "/v1/chat-runs/own/cancel"), db, user)
    for method, path in [
        ("GET", "/v1/me/api-keys"),
        ("POST", "/v1/agents"),
        ("GET", "/v1/chats/stream/private-run"),
        ("POST", "/v1/chat-runs/own/steer"),
    ]:
        with pytest.raises(HTTPException):
            enforce_agent_api_route(_request(method, path), db, user)


def test_capability_disabled_invalidates_scoped_and_personal_keys(db, monkeypatch):
    _, token = _key(db)
    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *_: {"can_use_api_key": False}
    )
    assert resolve_api_key_identity(db, token) is None


def test_team_manager_can_publish_but_member_cannot_and_lost_role_revokes_access(db):
    db.add(Team(team_id="team", name="Team"))
    member = TeamMember(team_id="team", user_id="owner", role="member")
    db.add(member)
    db.add(UserAgent(agent_id="ua_team", team_id="team", owner_type="team", name="Team"))
    db.commit()
    with pytest.raises(HTTPException):
        _key(db, "ua_team")
    member.role = "admin"
    db.commit()
    _, token = _key(db, "ua_team")
    assert resolve_api_key_identity(db, token) is not None
    member.role = "member"
    db.commit()
    assert resolve_api_key_identity(db, token) is None


def test_only_persisted_own_api_artifact_can_be_downloaded(db):
    key, _ = _key(db)
    user = _user(key)
    prepare_agent_api_request(
        db, user, ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    for artifact_id, chat_id, key_id in [
        ("owned", "api-chat", key.id),
        ("private", None, None),
        ("bad-key", "api-chat", "another"),
        ("unmarked", "api-chat", None),
    ]:
        db.add(
            Artifact(
                artifact_id=artifact_id,
                chat_id=chat_id,
                user_id="owner",
                type="document",
                title="File",
                filename="file.txt",
                size_bytes=1,
                mime_type="text/plain",
                storage_key=artifact_id,
                extra_data={"agent_api_key_id": key_id},
            )
        )
    db.commit()
    enforce_agent_api_route(_request("GET", "/files/owned"), db, user)
    for file_id in ("private", "bad-key", "unmarked", "absent"):
        with pytest.raises(HTTPException):
            enforce_agent_api_route(_request("GET", f"/files/{file_id}"), db, user)
    db.get(Artifact, "owned").deleted_at = datetime.now(timezone.utc)
    db.commit()
    with pytest.raises(HTTPException):
        enforce_agent_api_route(_request("GET", "/files/owned"), db, user)


@pytest.mark.asyncio
async def test_explicit_key_precedes_session_in_required_and_optional_auth(db, monkeypatch):
    import core.auth.backend as auth
    from fastapi.security import HTTPAuthorizationCredentials

    key, token = _key(db)

    async def forbidden_session(_request):
        raise AssertionError("cookie must never widen a scoped Bearer")

    monkeypatch.setattr(auth, "_resolve_session_user", forbidden_session)
    request = _request("POST", "/v1/agents/responses")
    request.scope["headers"] = [
        (b"authorization", f"Bearer {token}".encode()),
        (b"cookie", b"jx_session=admin"),
    ]
    resolved = await auth.get_current_user(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db,
    )
    assert resolved.api_key_id == key.id
    assert (await auth.require_auth(False)(request, db)).api_key_id == key.id


def test_owner_session_cannot_silently_widen_an_api_chat(db):
    key, _ = _key(db)
    prepare_agent_api_request(
        db, _user(key), ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    owner = UserContext(user_id="owner", user_center_id="owner", username="Owner")
    with pytest.raises(HTTPException):
        prepare_agent_api_request(
            db, owner, ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
        )


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_first_use_arbitrates_session_provenance(db, same_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from core.services.agent_api_service import _bind_api_session, make_agent_api_scope
    from sqlalchemy.orm import Session

    first, _ = _key(db)
    second = first if same_key else _key(db)[0]
    scopes = [make_agent_api_scope(_user(key), "race-chat") for key in (first, second)]
    barrier = Barrier(2)

    class RacingSession(Session):
        checked_session = False

        def get(self, entity, ident, **kwargs):
            row = super().get(entity, ident, **kwargs)
            if entity is ChatSession and not self.checked_session:
                self.checked_session = True
                assert row is None
                barrier.wait(timeout=5)
            return row

    def bind(scope):
        with RacingSession(bind=db.bind) as concurrent_db:
            try:
                _bind_api_session(concurrent_db, scope, "Race")
                return 200
            except HTTPException as exc:
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(bind, scopes))
    assert outcomes == ([200, 200] if same_key else [200, 403])
    db.expire_all()
    session = db.get(ChatSession, "race-chat")
    assert session.extra_data["agent_api_scope"] in scopes
    assert db.query(ChatSession).filter_by(chat_id="race-chat").count() == 1


@pytest.mark.asyncio
async def test_tab_bearer_required_dependency_never_uses_cookie(db, monkeypatch):
    import core.auth.backend as auth

    key, token = _key(db)

    async def forbidden_session(_):
        raise AssertionError("must authenticate the explicit scoped key")

    monkeypatch.setattr(auth, "_resolve_session_user", forbidden_session)
    request = _request("POST", "/v1/agents/responses")
    request.scope["headers"] = [(b"authorization", b"Bearer" + bytes([9]) + token.encode())]
    # Starlette's HTTPBearer may not parse a tab separator; use the raw header.
    assert (await auth.get_current_user(request, None, db)).api_key_id == key.id
