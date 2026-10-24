"""Call sites that used to query on the loop, or across network waits, now do neither."""

import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db import engine as db_engine


@pytest.fixture
def isolated_db(monkeypatch):
    database = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    factory = sessionmaker(bind=database)
    monkeypatch.setattr(db_engine, "SessionLocal", factory)
    return database, factory


async def test_project_memory_authorization_queries_off_the_loop(monkeypatch):
    from core.memory import access, backend

    loop_thread = threading.get_ident()
    seen = []
    memory = SimpleNamespace(
        get=lambda memory_id: {"user_id": "owner", "metadata": {"workspace_id": "project:p1"}}
    )
    monkeypatch.setattr(backend, "settings", SimpleNamespace(memory=SimpleNamespace(enabled=True)))
    monkeypatch.setattr(backend, "_get_memory", lambda: memory)

    def owner(db, project_id, actor):
        seen.append((threading.get_ident(), project_id, actor))
        return "owner"

    monkeypatch.setattr(access, "_project_memory_owner", owner)
    context = await access.authorized_memory_context("m1", "editor")
    assert context.scope_user_id == "owner" and context.workspace_id == "project:p1"
    assert seen and seen[0][0] != loop_thread and seen[0][1:] == ("p1", "editor")

    monkeypatch.setattr(access, "_project_memory_owner", lambda db, project_id, actor: None)
    assert await access.authorized_memory_context("m1", "editor") is None


async def test_resource_checkpoint_saves_after_the_worker_answers(monkeypatch, isolated_db):
    from core.capabilities.plugins.resources import service, store

    database, factory = isolated_db
    store.PluginResourceCheckpoint.__table__.create(database)
    monkeypatch.setenv("PLUGIN_RESOURCE_SECRET_KEY", "test-key")
    order = []

    async def worker(row, path, body=None, *, transport=None):
        order.append("network")
        return {"state": {"cookies": []}}

    monkeypatch.setattr(service, "request", worker)
    row = SimpleNamespace(application_key=None, user_id="owner", install_id="install")
    saved = await service.checkpoint(row, "account", "connection")
    with factory() as db:
        record = db.get(store.PluginResourceCheckpoint, saved["checkpoint_id"])
        assert (record.user_id, record.install_id, record.name) == ("owner", "install", "account")
    assert order == ["network"]


async def test_workbench_workers_close_without_a_shared_session(monkeypatch):
    from features.workbench import revocation

    loop_thread = threading.get_ident()
    events = []

    async def close(row):
        events.append(("close", row.resource_id))

    def release(db, row):
        events.append(("release", row.resource_id, threading.get_ident() != loop_thread))

    from core.db import threads

    monkeypatch.setattr(revocation.service, "close", close)
    monkeypatch.setattr(revocation, "_release_background_reservation", release)
    # No database needed: the release step gets no session, only its thread matters.
    monkeypatch.setattr(threads, "_in_session", lambda fn, args, kwargs: fn(None, *args, **kwargs))
    rows = [SimpleNamespace(resource_id="a"), SimpleNamespace(resource_id="b")]
    result = await revocation._close_workers(rows)
    assert result == {"closed": 2, "deferred": 0}
    assert events == [
        ("close", "a"),
        ("release", "a", True),
        ("close", "b"),
        ("release", "b", True),
    ]


async def test_stopping_prewarmed_workers_closes_rows_after_shutdowns(monkeypatch, isolated_db):
    from core.capabilities.plugins.resources import prewarm, store

    database, factory = isolated_db
    store.PluginResource.__table__.create(database)
    with factory() as db:
        for resource_id, status in (("w1", "warm"), ("w2", "warming"), ("a1", "active")):
            db.add(store.PluginResource(
                resource_id=resource_id, user_id="owner", chat_id="chat", install_id="i",
                revision="r", slug="s", module_id="m", scope="local", descriptor="",
                status=status, created_at=0, expires_at=0,
            ))
        db.commit()
    monkeypatch.setattr(store, "descriptor", lambda row: {})
    assert await prewarm._stop("chat") is True
    with factory() as db:
        statuses = {row.resource_id: row.status for row in db.query(store.PluginResource)}
    assert statuses == {"w1": "closed", "w2": "closed", "a1": "active"}
