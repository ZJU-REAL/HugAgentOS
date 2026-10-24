"""Shared maintenance discovers other workers and snapshot deletion is conditional."""
import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db.models import Base, ChatSandboxSnapshot
from core.infra.time import utc_now
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from core.sandbox.providers.opensandbox import snapshots, tracking
from core.sandbox.sessions import registry


async def test_missing_binding_does_not_block_other_worker_session(monkeypatch):
    monkeypatch.setattr(registry, "bindings", AsyncMock(return_value=[
        ("missing", "gone", 0), ("other-worker", "live", 0)]))
    monkeypatch.setattr(registry, "lookup", AsyncMock(side_effect=["gone", "live"]))
    forget = AsyncMock()
    monkeypatch.setattr(registry, "forget", forget)
    response = SimpleNamespace(status_code=200, json=lambda: {"metadata": {}},
                               raise_for_status=lambda: None)
    missing = SimpleNamespace(status_code=404, json=lambda: {"code": "DOCKER::SANDBOX_NOT_FOUND"})
    client = SimpleNamespace(get=AsyncMock(side_effect=[missing, response]))
    provider = object.__new__(OpenSandboxProvider)
    provider._sessions = {}
    provider._domain = "http://controller"
    provider._get_http_client = lambda: client
    provider._get_session_lock = AsyncMock(side_effect=lambda _: asyncio.Lock())
    session = SimpleNamespace(last_active_ts=time.monotonic())
    provider._attach_session = AsyncMock(return_value=session)
    await provider._discover_idle_sessions()
    forget.assert_awaited_once_with("missing", "gone")
    assert provider._sessions["other-worker"] is session
    assert session.last_active_ts < time.monotonic() - 100


async def test_snapshot_gc_cannot_remove_concurrent_replacement(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'snap.sqlite'}")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["chat_sandbox_snapshots"]])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(snapshots, "_get_db_session_local", lambda: factory)
    with factory() as db:
        db.add(ChatSandboxSnapshot(chat_id="chat", snapshot_id="old", sandbox_id="sandbox",
                                  expires_at=utc_now()-timedelta(hours=1)))
        db.commit()
    provider = object.__new__(OpenSandboxProvider)
    async def delete_remote(snapshot_id):
        with factory() as db:
            row = db.get(ChatSandboxSnapshot, "chat")
            row.snapshot_id = "new"
            row.expires_at = utc_now() + timedelta(days=1)
            db.commit()
    provider._delete_snapshot = delete_remote
    await provider._snapshot_gc_once()
    with factory() as db:
        assert db.get(ChatSandboxSnapshot, "chat").snapshot_id == "new"
    engine.dispose()
