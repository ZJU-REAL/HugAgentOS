"""Executable release regressions for CE edition boundaries."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as inspect_database
from sqlalchemy import text
from sqlalchemy.orm import Session


@pytest.fixture(scope="module", autouse=True)
def initialized_ce_database():
    from core.db.engine import SessionLocal, init_db
    from core.services.local_user_service import ensure_ce_default_admin

    init_db()
    db = SessionLocal()
    try:
        user_id, _ = ensure_ce_default_admin(db)
    finally:
        db.close()
    return user_id


def test_ce_login_ticket_exchange_and_session_check(initialized_ce_database):
    from api.app import app

    with TestClient(app) as client:
        login = client.post(
            "/login",
            data={"username": "admin", "password": "admin", "redirect": "/"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        ticket = parse_qs(urlparse(login.headers["location"]).query)["ticket"][0]

        exchange = client.post("/v1/auth/ticket/exchange", json={"code": ticket})
        assert exchange.status_code == 200, exchange.text
        assert exchange.json()["data"]["user_id"] == initialized_ce_database

        session = client.get("/v1/auth/session/check")
        assert session.status_code == 200, session.text
        assert session.json()["data"]["username"] == "admin"


def test_ce_registers_all_local_auth_routes():
    from api.app import app

    operations = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    actual = {
        (method.upper(), path)
        for path, path_item in app.openapi()["paths"].items()
        for method in path_item
        if method in operations
    }

    assert {
        ("POST", "/v1/auth/ticket/exchange"),
        ("GET", "/v1/auth/session/check"),
        ("POST", "/v1/auth/desktop/handoff"),
        ("POST", "/v1/auth/desktop/redeem"),
        ("POST", "/v1/auth/logout"),
    } <= actual


def test_ce_self_service_skill_and_mcp_do_not_import_admin_routes(
    initialized_ce_database,
    monkeypatch,
):
    from api.routes.v1 import me_capabilities
    from core.db.engine import SessionLocal
    from core.db.models import AdminMcpServer, AdminSkill

    assert "api.routes.v1.admin_" not in inspect.getsource(me_capabilities)

    async def probe_ok(row, db):
        row.tools_json = [{"name": "example_tool", "description": "", "inputSchema": {}}]
        return True, ""

    monkeypatch.setattr(me_capabilities, "probe_mcp_connectivity", probe_ok)
    user = SimpleNamespace(user_id=initialized_ce_database)
    db: Session = SessionLocal()
    try:
        skill_body = me_capabilities.CreateUserSkillRequest(
            name="ce-release-skill",
            display_name="CE release skill",
            description="Checks the physical edition boundary",
            instructions="Return a concise result.",
        )
        asyncio.run(me_capabilities.create_my_skill(skill_body, user, db))
        skill = db.query(AdminSkill).filter_by(skill_id="ce-release-skill").one()
        assert skill.owner_user_id == initialized_ce_database

        mcp_body = me_capabilities.CreateUserMcpRequest(
            display_name="CE release MCP",
            url="https://example.invalid/mcp",
        )
        asyncio.run(me_capabilities.create_my_mcp_server(mcp_body, user, db))
        mcp = (
            db.query(AdminMcpServer)
            .filter(AdminMcpServer.owner_user_id == initialized_ce_database)
            .one()
        )
        assert mcp.tools_json[0]["name"] == "example_tool"
    finally:
        db.close()


def test_ce_schema_reconcile_repairs_legacy_sqlite_columns(tmp_path):
    from core.db.edition_tables import ce_create_all, ce_reconcile_schema
    from core.db.models import AdminSkill, ChatSession, ModelProvider
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database = tmp_path / "legacy-ce.db"
    engine = create_engine(f"sqlite:///{database}")
    ce_create_all(engine)

    missing = {
        "admin_mcp_servers": ["source_plugin"],
        "admin_skills": ["dep_status", "source_plugin"],
        "chat_sessions": ["channel_id", "external_conversation_id"],
        "model_providers": ["provider", "gateway_group", "weight", "priority"],
        "tool_call_logs": ["sandbox_id"],
        "user_agents": ["plugin_ids", "source_market_slug"],
        "user_api_keys": ["key_enc"],
    }
    with engine.begin() as connection:
        for table_name, columns in missing.items():
            for index in inspect_database(connection).get_indexes(table_name):
                connection.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
            for column in columns:
                connection.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column}"'))
        connection.execute(
            text(
                "INSERT INTO admin_skills "
                "(skill_id, skill_content, display_name, description, version, is_enabled) "
                "VALUES ('legacy-skill', '---\\nname: legacy-skill\\n---\\n', "
                "'Legacy', 'Legacy row', '1.0.0', 1)"
            )
        )

    report = ce_reconcile_schema(engine)
    assert set(report["columns"]) == {
        f"{table}.{column}" for table, columns in missing.items() for column in columns
    }

    session = sessionmaker(bind=engine)()
    try:
        assert (
            session.query(AdminSkill).filter_by(skill_id="legacy-skill").one().dep_status == "ready"
        )
        assert session.query(ModelProvider).first() is None
        assert session.query(ChatSession).first() is None
    finally:
        session.close()
        engine.dispose()


def test_ce_startup_seams_and_compose_defaults_are_ce_safe():
    from core.services.edition_startup import (
        create_distillation_scheduler,
        recover_datasource_sidecars,
        recover_persona_distill_jobs,
    )

    assert asyncio.run(recover_datasource_sidecars()) == {}
    assert create_distillation_scheduler() is None
    assert recover_persona_distill_jobs() == 0

    root = Path(__file__).resolve().parents[4]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    for expected in (
        "${JX_EDITION:-ce}",
        "${AUTH_MODE:-session}",
        "${SSO_LOGIN_MODE:-local}",
        "${SSO_EXCHANGE_MODE:-local}",
        "${VITE_EDITION:-ce}",
    ):
        assert expected in compose
