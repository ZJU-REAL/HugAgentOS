"""Shared fixtures: an isolated capability root and an isolated SQLite index."""

from __future__ import annotations

import pytest
from api.routes.v1.catalog import router as catalog_router
from core.auth.backend import UserContext, get_current_user
from core.capabilities import device_catalog
from core.db.engine import get_db
from core.services import desktop_cloud_bridge as bridge
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests._capability_index import bind_capability_index
from tests.capabilities._cloud_identity import CLOUD_BASE, CLOUD_USER, cloud_token


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
    from core.config.catalog_runtime import invalidate_runtime_catalog_cache
    from core.db.engine import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        bridge,
        "get_state",
        lambda: {"cloud_base": CLOUD_BASE, "token": cloud_token(CLOUD_USER)},
    )
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: True)
    # 云端下发的连接器与本次断言无关，固定成空，避免依赖清单缓存。
    monkeypatch.setattr(device_catalog, "_managed_connectors", lambda: [])

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    invalidate_runtime_catalog_cache()

    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=CLOUD_USER, user_center_id=CLOUD_USER, username=CLOUD_USER
    )
    yield TestClient(app)
    session.close()
    engine.dispose()
    invalidate_runtime_catalog_cache()
