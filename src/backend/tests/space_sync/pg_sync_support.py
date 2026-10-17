"""Isolated PostgreSQL fixture for real concurrent space-sync transactions."""

import os
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import UserShadow
from core.myspace import mirror
from core.space_sync.content import register


@pytest.fixture
def postgres_space(tmp_path, monkeypatch):
    raw = os.environ.get("SPACE_SYNC_PG_URL")
    if not raw:
        pytest.skip("Set SPACE_SYNC_PG_URL to the isolated space_sync_test database")
    url = make_url(raw)
    if (
        url.get_backend_name() != "postgresql"
        or url.host != "127.0.0.1"
        or url.database != "space_sync_test"
        or url.username != "space_sync_test"
    ):
        raise ValueError("Concurrency tests require the dedicated loopback test database")
    schema = "sync_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=3000 -cstatement_timeout=8000"
        },
    )
    factory = sessionmaker(bind=engine)
    try:
        Base.metadata.create_all(engine)
        monkeypatch.setattr("core.db.engine.SessionLocal", factory)
        from core.config.settings import settings

        configured = replace(
            settings,
            storage=replace(settings.storage, type="local", path=str(tmp_path / "storage")),
        )
        monkeypatch.setattr("core.config.settings.settings", configured)
        monkeypatch.setattr("core.sandbox._common.settings", configured)
        monkeypatch.setenv("STORAGE_TYPE", "local")
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
        monkeypatch.setattr("core.storage.factory._storage_instance", None)
        with factory() as db:
            db.add(UserShadow(user_id="alice", username="alice"))
            db.commit()
        from core.sandbox._common import myspace_cache_dir
        from core.storage import get_storage

        root = myspace_cache_dir("alice")
        root.mkdir(parents=True)
        yield SimpleNamespace(factory=factory, root=root, storage=get_storage(), engine=engine)
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed(space, name="a.txt", data=b"initial"):
    path = space.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    result = register("alice", mirror.mirror_entry("alice", name))
    return result["file_id"]


def await_event(event):
    assert event.wait(5), "Concurrent operation failed to reach its synchronization point"
