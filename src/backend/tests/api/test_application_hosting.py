"""Three approved application hosting seams; no shared database is modified."""

import hashlib
from types import SimpleNamespace

import pytest
from core.auth.backend import get_current_user
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, text
from tests.api.application_hosting_support import hosted


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def create(client):
    return data(client.post("/v1/applications", json={"title": "Form application"}))["id"]


def table(client, app_id, public=False):
    return data(
        client.post(
            f"/v1/applications/{app_id}/tables",
            json={
                "name": "entries",
                "public_insert": public,
                "columns": [
                    {"name": "name", "required": True, "max_length": 80},
                    {"name": "email", "required": True, "unique": True},
                    {"name": "age", "type": "integer"},
                ],
            },
        )
    )


def test_form_real_columns_and_owner_only_reads(hosted):
    client, engine = hosted
    app_id = create(client)
    table(client, app_id)
    columns = inspect(engine).get_columns(
        "entries" if engine.dialect.name == "postgresql" else f"app_{app_id}_entries",
        schema="app_" + app_id if engine.dialect.name == "postgresql" else None,
    )
    assert {c["name"] for c in columns} >= {"name", "email", "age", "version"}
    body = {
        "rows": [{"name": "Aaron", "email": "aaron@example.test", "age": 30}],
        "request_key": "submission-1",
    }
    path = f"/v1/applications/{app_id}/tables/entries/records"
    result = data(client.post(path, json=body))
    assert data(client.post(path, json=body)) == result
    assert data(client.get(path))["total"] == 1
    assert (
        client.post(
            path,
            json={
                "rows": [{"name": "wrong", "email": "other@example.test", "extra": "x"}],
            },
        ).status_code
        == 422
    )
    record_path = path + "/" + result["items"][0]["id"]
    assert client.patch(record_path, json={"version": 1, "values": {"age": 31}}).status_code == 200
    assert client.patch(record_path, json={"version": 1, "values": {"age": 32}}).status_code == 409
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="other")
    assert client.get(path).status_code == 404


def test_batches_atomic_and_sql_identifiers_rejected(hosted):
    client, _ = hosted
    app_id = create(client)
    table(client, app_id)
    path = f"/v1/applications/{app_id}/tables/entries/records"
    assert (
        client.post(
            path,
            json={
                "rows": [{"name": "A", "email": "duplicate"}, {"name": "B", "email": "duplicate"}],
            },
        ).status_code
        == 409
    )
    assert data(client.get(path))["total"] == 0
    assert (
        client.post(
            f"/v1/applications/{app_id}/tables",
            json={
                "name": "entries; DROP TABLE users",
                "columns": [{"name": "name"}],
            },
        ).status_code
        == 422
    )


def test_mcp_publish_projection_and_revoke(hosted):
    client, engine = hosted
    app_id = create(client)
    table(client, app_id)
    tool = {
        "name": "query_entries",
        "description": "Query approved fields",
        "table": "entries",
        "fields": ["name"],
        "filters": ["email"],
    }
    published = data(client.post(f"/v1/applications/{app_id}/mcp", json={"tools": [tool]}))
    assert published["url"] == f"/applications-mcp/{app_id}"
    from core.services.application_store import applications
    from sqlalchemy import select

    with engine.connect() as db:
        stored = db.execute(select(applications)).mappings().one()
        assert stored["token_hash"] == hashlib.sha256(published["token"].encode()).hexdigest()
    assert "token" not in str(data(client.get(f"/v1/applications/{app_id}")))
    assert client.delete(f"/v1/applications/{app_id}/mcp").status_code == 200


@pytest.mark.asyncio
async def test_external_mcp_protocol_field_allowlist_and_revocation(hosted):
    import httpx
    from core.services.application_mcp import ApplicationMCPGateway
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    client, _ = hosted
    app_id = create(client)
    table(client, app_id)
    data(
        client.post(
            f"/v1/applications/{app_id}/tables/entries/records",
            json={
                "rows": [{"name": "Aaron", "email": "private@example.test"}],
            },
        )
    )
    published = data(
        client.post(
            f"/v1/applications/{app_id}/mcp",
            json={
                "tools": [
                    {
                        "name": "query_entries",
                        "description": "Query names",
                        "table": "entries",
                        "fields": ["name"],
                        "filters": ["name"],
                    }
                ]
            },
        )
    )
    gateway = FastAPI()
    gateway.mount("/applications-mcp", ApplicationMCPGateway())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://testserver"
    ) as anonymous:
        assert (await anonymous.post(published["url"], json={})).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway),
        headers={"Authorization": "Bearer " + published["token"]},
    ) as http:
        async with streamable_http_client(
            "http://testserver" + published["url"], http_client=http
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["query_entries"]
                result = await session.call_tool("query_entries", {"name": "Aaron"})
                assert not result.isError
                assert result.structuredContent["items"] == [{"name": "Aaron"}]
                result = await session.call_tool("query_entries", {"email": "private@example.test"})
                assert result.isError
        data(client.delete(f"/v1/applications/{app_id}/mcp"))
        assert (await http.post("http://testserver" + published["url"], json={})).status_code == 401


@pytest.fixture
def hosted_sites(hosted, tmp_path, monkeypatch):
    from api.routes import sites_serve
    from api.routes.v1 import application_transports, applications
    from core.db.engine import Base, get_db
    from core.db.models import UserShadow
    from core.services.site_service import SiteService
    from core.storage.local import LocalStorageBackend
    from sqlalchemy.orm import sessionmaker

    platform = create_engine(
        "sqlite:///" + str(tmp_path / "sites-platform.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(platform)
    factory = sessionmaker(bind=platform)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    storage = LocalStorageBackend()
    monkeypatch.setattr("core.services.site_service.get_storage", lambda: storage)
    monkeypatch.setattr("core.storage.get_storage", lambda: storage)
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="owner"))
        db.commit()
        service = SiteService(db)
        resume = service.publish(
            user_id="owner",
            title="Personal résumé",
            slug="personal-resume",
            files=[("index.html", "<h1>Aaron · 个人简历</h1>".encode())],
        )
        form = service.publish(
            user_id="owner",
            title="Form system",
            slug="application-form",
            files=[("index.html", b"<form>Application form</form>")],
        )
        ids = resume.site_id, form.site_id
    client, _ = hosted

    def database():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    client.app.include_router(application_transports.public_router)
    client.app.include_router(sites_serve.router)
    yield client, ids
    platform.dispose()


def test_static_resume_does_not_create_database(hosted_sites):
    client, _ = hosted_sites
    response = client.get("/site/personal-resume/")
    assert response.status_code == 200
    assert "个人简历" in response.text
    assert data(client.get("/v1/applications"))["items"] == []


def test_visitor_form_persists_without_read_access(hosted_sites):
    client, (_, site_id) = hosted_sites
    app_id = data(client.post("/v1/applications", json={"title": "Form", "site_id": site_id}))["id"]
    table(client, app_id, public=True)
    from fastapi import HTTPException

    def unauthenticated():
        raise HTTPException(401, "Login required")

    client.app.dependency_overrides[get_current_user] = unauthenticated
    response = client.post(
        "/site/application-form/__api/data/entries",
        json={
            "rows": [{"name": "Visitor", "email": "visitor@example.test"}],
            "request_key": "visitor-once",
        },
    )
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"ok", "id"}
    assert client.get(f"/v1/applications/{app_id}/tables/entries/records").status_code == 401
    assert client.get("/site/application-form/__api/data/entries").status_code == 404
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="owner")
    result = data(client.get(f"/v1/applications/{app_id}/tables/entries/records"))
    assert result["items"][0]["email"] == "visitor@example.test"


def test_postgres_roles_cannot_read_other_apps_or_registry(hosted):
    client, engine = hosted
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL permission checks")
    first, second = create(client), create(client)
    table(client, first)
    table(client, second)
    from sqlalchemy.exc import ProgrammingError

    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL ROLE "app_role_{first}"'))
        with pytest.raises(ProgrammingError):
            connection.execute(text(f'SELECT * FROM "app_{second}"."entries"'))
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL ROLE "app_role_{first}"'))
        with pytest.raises(ProgrammingError):
            connection.execute(text("SELECT * FROM hosted_applications"))


def test_long_postgres_names_and_reserved_mcp_parameters(hosted):
    client, _ = hosted
    app_id = create(client)
    name = "a" * 48
    data(
        client.post(
            f"/v1/applications/{app_id}/tables",
            json={
                "name": name,
                "columns": [{"name": "model_config"}, {"name": "normal"}],
            },
        )
    )
    data(
        client.post(
            f"/v1/applications/{app_id}/tables/{name}/records",
            json={
                "rows": [{"model_config": "safe SQL data", "normal": "record"}],
            },
        )
    )
    assert data(client.get(f"/v1/applications/{app_id}/tables/{name}/records"))["total"] == 1
    assert (
        client.post(
            f"/v1/applications/{app_id}/mcp",
            json={
                "tools": [
                    {
                        "name": "query",
                        "description": "Invalid parameter test",
                        "table": name,
                        "fields": ["normal"],
                        "filters": ["model_config"],
                    }
                ]
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize("parameter", ["model_dump_one_level", "model_config", "class", "for"])
def test_mcp_sdk_reserved_parameters_rejected(hosted, parameter):
    client, _ = hosted
    app_id = create(client)
    data(
        client.post(
            f"/v1/applications/{app_id}/tables",
            json={
                "name": "records",
                "columns": [{"name": parameter}, {"name": "normal"}],
            },
        )
    )
    assert (
        client.post(
            f"/v1/applications/{app_id}/mcp",
            json={
                "tools": [
                    {
                        "name": "query",
                        "description": "Reserved SDK parameter",
                        "table": "records",
                        "fields": ["normal"],
                        "filters": [parameter],
                    }
                ]
            },
        ).status_code
        == 422
    )


def test_concurrent_retries_write_once(hosted):
    from concurrent.futures import ThreadPoolExecutor

    client, _ = hosted
    app_id = create(client)
    table(client, app_id)
    path = f"/v1/applications/{app_id}/tables/entries/records"
    body = {
        "rows": [{"name": "Concurrent", "email": "same@example.test"}],
        "request_key": "concurrent",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.post(path, json=body), range(2)))
    assert data(responses[0]) == data(responses[1])
    assert data(client.get(path))["total"] == 1


def test_mcp_rollback_preserves_records(hosted):
    client, _ = hosted
    app_id = create(client)
    table(client, app_id)
    data(
        client.post(
            f"/v1/applications/{app_id}/tables/entries/records",
            json={
                "rows": [{"name": "Preserved", "email": "preserved@example.test"}],
            },
        )
    )
    first = {
        "name": "query_entries",
        "description": "Version one",
        "table": "entries",
        "fields": ["name"],
        "filters": ["name"],
    }
    published = data(client.post(f"/v1/applications/{app_id}/mcp", json={"tools": [first]}))
    assert published["version"] == 1
    second = {**first, "fields": ["name", "email"]}
    assert (
        data(client.post(f"/v1/applications/{app_id}/mcp", json={"tools": [second]}))["version"]
        == 2
    )
    rolled = data(client.post(f"/v1/applications/{app_id}/mcp/rollback", json={"version": 1}))
    assert rolled["version"] == 3 and rolled["token"] != published["token"]
    assert data(client.get(f"/v1/applications/{app_id}"))["tools"][0]["fields"] == ["name"]
    assert data(client.get(f"/v1/applications/{app_id}/tables/entries/records"))["total"] == 1


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        [],
        {"header": None, "body": []},
        {"header": {"code": 200}, "body": [None]},
    ],
)
def test_malformed_industry_source_is_controlled(envelope, monkeypatch):
    from core.services.application_industry import fetch_enterprises
    from fastapi import HTTPException

    monkeypatch.setattr(
        "mcp_servers.ai_chain_information_mcp.impl_company._resolve_company_config",
        lambda: ("http://source.example.test", "local-test-only"),
    )
    monkeypatch.setattr(
        "core.services.application_industry.httpx.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: envelope
        ),
    )
    with pytest.raises(HTTPException) as error:
        fetch_enterprises("test", 5)
    assert error.value.status_code == 502
