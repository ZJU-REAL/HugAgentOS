"""HTTP lifecycle, global gate and durable execution outcome for agent keys."""

from datetime import datetime, timezone
import importlib.util
import io
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import (
    Boolean,
    Column,
    MetaData,
    String,
    Table,
    TIMESTAMP,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.orm import sessionmaker

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import AgentApiCallLog, ChatRun, UserApiKey
from core.services.agent_api_service import (
    begin_agent_api_call,
    bind_agent_api_run,
    list_agent_api_calls,
    prepare_agent_api_request,
)
from tests.api.test_agent_api_scope import db, _key, _user
from api.schemas import ChatRequest


def _app(db, monkeypatch, *, user_id="owner", middleware=False):
    from api.routes.v1 import agent_api_keys, api_keys

    monkeypatch.setattr(
        agent_api_keys, "resolve_user_capabilities", lambda *_: {"can_use_api_key": True}
    )
    monkeypatch.setattr(api_keys, "resolve_user_capabilities", lambda *_: {"can_use_api_key": True})
    app = FastAPI()
    app.include_router(agent_api_keys.router)
    app.include_router(api_keys.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        user_center_id=user_id,
        username=user_id,
    )
    app.dependency_overrides[get_db] = lambda: db
    if middleware:
        from api.middleware import agent_api_scope

        monkeypatch.setattr(agent_api_scope, "SessionLocal", sessionmaker(bind=db.bind))
        app.add_middleware(agent_api_scope.AgentApiScopeMiddleware)

    @app.get("/v1/config/private")
    async def private():
        # Deliberately bypasses get_current_user, as several admin gates do.
        return {"secret": "must not reach the scoped caller"}

    @app.post("/v1/agents/responses")
    async def responses(body: ChatRequest):
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_key_lifecycle_dto_and_cross_agent_key_isolation(db, monkeypatch):
    monkeypatch.setattr("core.infra.crypto.decrypt_secret", lambda _: "test-only-plaintext")
    app = _app(db, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/agents/ua_one/api-keys", json={"name": "App", "expires_in_days": 30}
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        key = created.json()["data"]
        assert key["agent_id"] == "ua_one" and key["api_key"].startswith("sk-jx-")
        key_id = key["id"]
        listed = (await client.get("/v1/agents/ua_one/api-keys")).json()["data"]["items"]
        assert listed[0]["api_key"] is None and listed[0]["revealable"]
        assert (await client.get(f"/v1/agents/ua_two/api-keys/{key_id}/reveal")).status_code == 404
        revealed = await client.get(f"/v1/agents/ua_one/api-keys/{key_id}/reveal")
        assert revealed.json()["data"]["api_key"] == "test-only-plaintext"
        assert revealed.headers["cache-control"] == "no-store"
        disabled = await client.patch(
            f"/v1/agents/ua_one/api-keys/{key_id}", json={"enabled": False}
        )
        assert not disabled.json()["data"]["enabled"]
        assert (await client.delete(f"/v1/agents/ua_one/api-keys/{key_id}")).json()["data"][
            "revoked"
        ]
        assert (await client.get("/v1/agents/ua_one/api-keys")).json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_personal_key_routes_exist_without_gateway_entitlement(db, monkeypatch):
    from api.routes.v1 import CE_ROUTERS

    assert ("api_keys", "router") in CE_ROUTERS
    app = _app(db, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/me/api-keys")).status_code == 200
        created = await client.post("/v1/me/api-keys", json={"name": "Global"})
        assert created.status_code == 201
        key_id = created.json()["data"]["id"]
        assert created.json()["data"]["api_key"].startswith("sk-jx-")
        assert (await client.get("/v1/me/api-keys")).json()["data"]["items"][0]["id"] == key_id


@pytest.mark.asyncio
async def test_disabled_agent_keys_remain_creatable(db, monkeypatch):
    from core.db.models import UserAgent

    db.get(UserAgent, "ua_one").is_enabled = False
    db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(_app(db, monkeypatch)), base_url="http://test"
    ) as client:
        created = await client.post("/v1/agents/ua_one/api-keys", json={"name": "Disabled"})
        assert created.status_code == 201
        assert (await client.get("/v1/agents/ua_one/api-keys")).status_code == 200


@pytest.mark.asyncio
async def test_non_owner_cannot_create_or_list_keys(db, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(_app(db, monkeypatch, user_id="other")),
        base_url="http://test",
    ) as client:
        assert (await client.get("/v1/agents/ua_one/api-keys")).status_code == 403
        assert (
            await client.post("/v1/agents/ua_one/api-keys", json={"name": "App"})
        ).status_code == 403
    assert db.query(UserApiKey).count() == 0


@pytest.mark.asyncio
async def test_global_gate_cannot_be_bypassed_with_cookie_or_nonstandard_admin_dependency(
    db, monkeypatch
):
    _, token = _key(db)
    app = _app(db, monkeypatch, middleware=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {"Authorization": f"Bearer {token}", "Cookie": "jx_session=admin"}
        forbidden = await client.get("/v1/config/private", headers=headers)
        assert forbidden.status_code == 403 and "secret" not in forbidden.text
        assert (await client.get("/v1/agents/ua_one/api-keys", headers=headers)).status_code == 403
        assert (
            await client.post(
                "/v1/agents/responses",
                headers=headers,
                json={"chat_id": "api-chat", "message": "Hi"},
            )
        ).status_code == 200
        assert (
            await client.get(
                "/v1/config/private", headers={"Authorization": "Bearer sk-jx-invalid"}
            )
        ).status_code == 401


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "needs_attention"])
def test_call_records_follow_durable_run_outcome_and_preserve_revoked_key_snapshot(db, outcome):
    key, _ = _key(db)
    _, scope = prepare_agent_api_request(
        db, _user(key), ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    call_id = begin_agent_api_call(db, scope, True)
    run = ChatRun(
        run_id="run",
        chat_id="api-chat",
        user_id="owner",
        message_id="reply",
        request_payload={"agent_api_scope": scope},
        status=outcome,
        completed_at=datetime.now(timezone.utc),
        usage={"prompt_tokens": 11, "completion_tokens": 7},
    )
    db.add(run)
    db.commit()
    bind_agent_api_run(db, call_id, "run")
    key.revoked_at = datetime.now(timezone.utc)
    db.commit()
    items, total = list_agent_api_calls(db, "owner", "ua_one", status=outcome)
    assert total == 1 and items[0]["status"] == outcome
    assert items[0]["total_tokens"] == 18 and items[0]["http_status"] == 200
    assert items[0]["key_name"] == "Client"
    assert list_agent_api_calls(db, "other", "ua_one")[1] == 0
    assert list_agent_api_calls(db, "owner", "ua_two")[1] == 0
    assert list_agent_api_calls(db, "owner", "ua_one", key_id="another")[1] == 0


def _migration():
    path = Path(__file__).parents[2] / "alembic/versions/agentapi01_scoped_keys_and_call_logs.py"
    spec = importlib.util.spec_from_file_location("agentapi_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_upgrade_and_downgrade_revoke_scoped_keys_without_changing_personal(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    old = Table(
        "user_api_keys",
        MetaData(),
        Column("id", String, primary_key=True),
        Column("user_id", String),
        Column("enabled", Boolean, nullable=False),
        Column("revoked_at", TIMESTAMP),
    )
    old.create(engine)
    migration = _migration()
    with engine.begin() as connection:
        connection.execute(old.insert(), [{"id": "personal", "enabled": True}])
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        current = Table("user_api_keys", MetaData(), autoload_with=connection)
        connection.execute(current.insert().values(id="agent", agent_id="ua_one", enabled=True))
        assert "agent_api_call_logs" in inspect(connection).get_table_names()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        result = {row.id: row for row in connection.execute(select(old))}
        assert result["personal"].enabled and result["personal"].revoked_at is None
        assert not result["agent"].enabled and result["agent"].revoked_at is not None
        assert "agent_id" not in {
            c["name"] for c in inspect(connection).get_columns("user_api_keys")
        }


def test_postgresql_upgrade_and_downgrade_compile_without_database():
    sql = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql}
    )
    with Operations.context(context):
        _migration().upgrade()
        _migration().downgrade()
    rendered = sql.getvalue()
    assert "ADD COLUMN agent_id" in rendered
    assert "CREATE TABLE agent_api_call_logs" in rendered
    assert rendered.index("UPDATE user_api_keys") < rendered.index("DROP COLUMN agent_id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "chat_id": "api-chat",
            "message": "secret",
            "agent_api_scope": {"owner_user_id": "another"},
        },
        {"chat_id": "api-chat", "message": "secret", "stream": "invalid"},
    ],
)
async def test_schema_rejections_are_logged_without_reading_or_saving_body(
    db, monkeypatch, payload
):
    _, token = _key(db)
    app = _app(db, monkeypatch, middleware=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agents/responses", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 422
    row = db.query(AgentApiCallLog).one()
    assert row.http_status == 422 and row.error_code == "request_validation_failed"
    assert row.chat_id is None and row.run_id is None
    assert "secret" not in str(row.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["  ", "\t ", " "])
async def test_bearer_whitespace_never_falls_back_to_privileged_cookie(db, monkeypatch, separator):
    _, token = _key(db)
    app = _app(db, monkeypatch, middleware=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/config/private",
            headers={
                "Authorization": f"Bearer{separator}{token} ",
                "Cookie": "jx_session=admin",
            },
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_cancel_rejection_is_not_a_new_agent_invocation(db, monkeypatch):
    from fastapi import HTTPException

    key, token = _key(db)
    _, scope = prepare_agent_api_request(
        db, _user(key), ChatRequest(chat_id="api-chat", message="Hi", agent_id="ua_one")
    )
    db.add(
        ChatRun(
            run_id="run-cancel",
            chat_id="api-chat",
            user_id="owner",
            message_id="msg-cancel",
            status="completed",
            request_payload={"agent_api_scope": scope},
        )
    )
    db.commit()
    app = _app(db, monkeypatch, middleware=True)

    @app.post("/v1/chat-runs/{run_id}/cancel")
    async def cancel(run_id: str):
        raise HTTPException(status_code=409, detail="Already completed")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat-runs/run-cancel/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert db.query(AgentApiCallLog).count() == 0
