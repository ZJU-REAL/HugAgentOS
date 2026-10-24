"""Shared fixtures: an isolated capability root and an isolated SQLite index."""

from __future__ import annotations
from features.desktop.bridge import identity as bridge_identity
from features.desktop.capabilities import skill_account

import pytest
from api.routes.v1.catalog import router as catalog_router
from core.auth.backend import UserContext, get_current_user
from features.desktop.capabilities import device_catalog
from core.db.engine import get_db
from features.desktop.bridge import client as bridge
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests._capability_index import bind_capability_index
from tests.features.desktop.capabilities._cloud_identity import CLOUD_BASE, CLOUD_USER, cloud_token
from tests.db_support import share_session


@pytest.fixture
def caps_root(tmp_path, monkeypatch):
    root = tmp_path / "caps"
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(root))
    return root


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    engine, factory = bind_capability_index(tmp_path, monkeypatch)
    yield factory
    engine.dispose()


@pytest.fixture
def hybrid_catalog_client(tmp_path, index_db, caps_root, monkeypatch):
    from core.capabilities.catalog.runtime import invalidate_runtime_catalog_cache
    from core.db.engine import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        bridge_identity,
        'get_state',
        lambda: {"cloud_base": CLOUD_BASE, "token": cloud_token(CLOUD_USER)},
    )
    monkeypatch.setattr('features.desktop.auth.bridge.bridge_enabled', lambda: True)
    monkeypatch.setattr('features.desktop.capabilities.skill_account.current_local_user_id', lambda: CLOUD_USER)
    # 云端下发的连接器与本次断言无关，固定成空，避免依赖清单缓存。
    monkeypatch.setattr(device_catalog, "_managed_connectors", lambda: [])

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    invalidate_runtime_catalog_cache()

    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[get_db] = lambda: session
    share_session(monkeypatch, session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=CLOUD_USER, user_center_id=CLOUD_USER, username=CLOUD_USER
    )
    yield TestClient(app)
    session.close()
    engine.dispose()
    invalidate_runtime_catalog_cache()


@pytest.fixture
def client(tmp_path, index_db, caps_root, monkeypatch):
    """Isolated local desktop API, including the catalog tables refreshed by installs."""
    from core.db.engine import Base

    Base.metadata.create_all(index_db.kw["bind"])
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    monkeypatch.setattr('core.capabilities.mcp.service.SessionLocal', index_db)
    from features.desktop.api.desktop_capabilities import router as desktop_router
    from features.desktop.capabilities import skills
    from features.desktop.bridge import client as bridge

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "workspace" / "skills"))
    monkeypatch.setattr('features.desktop.auth.bridge.bridge_enabled', lambda: True)
    monkeypatch.setattr(skill_account, 'current_local_user_id', lambda: "local-u1")
    bridge_identity.reset_for_tests()
    app = FastAPI()
    app.include_router(desktop_router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="local-u1", user_center_id="local-u1", username="local-u1"
    )
    yield TestClient(app)
    bridge_identity.reset_for_tests()
