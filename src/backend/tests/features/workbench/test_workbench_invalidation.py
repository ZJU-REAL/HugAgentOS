"""Authenticated HTTP directory validators and revocation, isolated SQLite/PG fixture."""

import asyncio

import pytest
from core.auth import backend, session
from core.config.settings import settings
from core.db.engine import get_db
from core.db.models import UserShadow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def client(db_session, monkeypatch):
    from api.deps import require_config
    from features.workbench.api import workbench_events
    from features.workbench.api.workbench import router

    monkeypatch.setattr(session, "_use_memory_store", lambda: True)
    monkeypatch.setattr(backend, "_auth_mode", lambda: "session")
    monkeypatch.setattr(workbench_events, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    session.process_state().MEMORY_SESSIONS.clear()
    for user in ["revision-alice", "revision-bob"]:
        db_session.add(UserShadow(user_id=user, user_center_id=user, username=user, extra_data={}))
    db_session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_config] = lambda: None
    tokens = {
        user: asyncio.run(session.create_session({"user_id": user}))
        for user in ["revision-alice", "revision-bob"]
    }
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=db_session.get_bind()))
    with TestClient(app) as http:
        http.cookies.set(settings.session.cookie_name, tokens["revision-alice"])
        yield http, tokens
    session.process_state().MEMORY_SESSIONS.clear()


def test_directory_validator_is_private_and_identity_bound(client):
    http, tokens = client
    first = http.get("/v1/workbench/catalog")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-cache"
    etag = first.headers["etag"]
    assert http.get("/v1/workbench/catalog", headers={"If-None-Match": etag}).status_code == 304
    assert (
        http.get("/v1/workbench/catalog", headers={"If-None-Match": "W/" + etag}).status_code == 304
    )
    http.cookies.set(settings.session.cookie_name, tokens["revision-bob"])
    bob = http.get("/v1/workbench/catalog", headers={"If-None-Match": etag})
    assert bob.status_code == 200
    assert bob.headers["etag"] != etag


def test_events_only_publish_revision_and_recheck_projection(client):
    http, _ = client
    first = http.get("/v1/workbench/catalog").json()["data"]
    notice = http.get("/v1/workbench/catalog/events")
    assert notice.status_code == 200
    assert set(notice.json()["data"]) == {"revision", "scope"}
    assert notice.json()["data"]["revision"] == first["revision"]
    created = http.post(
        "/v1/workbench/admin/external-applications",
        json={
            "declaration": {
                "schemaVersion": "1.0.0",
                "id": "revision_report",
                "title": "Reports",
                "target": {
                    "kind": "external",
                    "mode": "browser",
                    "url": "https://reports.example.org",
                },
            },
            "enabled": True,
        },
    )
    assert created.status_code == 200
    changed = http.get("/v1/workbench/catalog/events?revision=" + first["revision"])
    assert changed.status_code == 200
    assert changed.json()["data"]["revision"] != first["revision"]
    assert "Reports" not in changed.text
    assert (
        http.get(
            "/v1/workbench/catalog", headers={"If-None-Match": '"' + first["revision"] + '"'}
        ).status_code
        == 200
    )


def test_revoked_cookie_cannot_resume_notifications(client):
    http, tokens = client
    assert http.get("/v1/workbench/catalog/events").status_code == 200
    assert asyncio.run(session.revoke_session(tokens["revision-alice"])) is True
    assert http.get("/v1/workbench/catalog/events").status_code == 401
    assert http.get("/v1/workbench/catalog").status_code == 401


def test_notifications_timeout_and_reject_unbounded_revision(client, monkeypatch):
    from features.workbench.api import workbench_events

    http, _ = client
    revision = http.get("/v1/workbench/catalog").json()["data"]["revision"]
    monkeypatch.setattr(workbench_events, "WAIT_SECONDS", 0)
    assert http.get("/v1/workbench/catalog/events?revision=" + revision).status_code == 204
    assert http.get("/v1/workbench/catalog/events?revision=" + "a" * 65).status_code == 422
    assert http.get("/v1/workbench/catalog/events?revision=secret-token").status_code == 422


def test_passive_notifications_do_not_keep_an_idle_session_alive(client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    http, _ = client
    now = [datetime.now(timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0] if tz else now[0].replace(tzinfo=None)

    monkeypatch.setattr(session, "datetime", Clock)
    token = asyncio.run(session.create_session({"user_id": "revision-alice"}, ttl_seconds=60))
    http.cookies.set(settings.session.cookie_name, token)
    now[0] += timedelta(seconds=50)
    assert http.get("/v1/workbench/catalog/events").status_code == 200
    now[0] += timedelta(seconds=20)
    assert http.get("/v1/workbench/catalog").status_code == 401


def test_directory_notifications_never_write_or_reconcile(client, db_session):
    from sqlalchemy import event

    http, _ = client
    # Initialize the authenticated directory/origin before observing the idle read.
    http.get("/v1/workbench/catalog")
    writes = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert http.get("/v1/workbench/catalog/events").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert writes == []


def test_revocation_sweep_discovers_owners_without_live_workers(client):
    from features.workbench import activity
    from features.workbench.revocation_sweep import owners
    from core.db.engine import SessionLocal

    http, _ = client
    with SessionLocal() as db:
        activity.save(
            db,
            "revision-alice",
            {
                "instances": {},
                "grants": {},
                "badges": {},
                "notifications": [],
                "background": {},
            },
        )
    assert owners("", 1) == ["revision-alice"]
    assert owners("revision-alice", 1) == []
