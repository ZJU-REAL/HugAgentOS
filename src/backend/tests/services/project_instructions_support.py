"""Isolated project-instruction fixtures."""

from __future__ import annotations

import pytest
from core.db.engine import Base
from core.db.models import UserShadow
from core.services.project_service import ProjectService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def env(tmp_path, monkeypatch):
    # No application DB, object store, model, or sandbox process is used.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'acceptance.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr("core.storage.factory._storage_instance", None)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    with factory() as db:
        db.add_all(
            [UserShadow(user_id=u, username=u, email=f"{u}@example.com") for u in ("alice", "bob")]
        )
        db.commit()
        yield db, tmp_path
    engine.dispose()


def local(env):
    db, root = env
    folder = root / "project"
    folder.mkdir()
    p = ProjectService(db).create_local("alice", "Local", str(folder))
    return p, folder / "AGENTS.md"


def update(db, p, text, revision=None):
    return ProjectService(db).update(
        p.project_id,
        "alice",
        {
            "instructions": text,
            "instructions_revision": revision,
        },
        level="admin",
    )
