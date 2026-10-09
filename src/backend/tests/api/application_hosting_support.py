"""Disposable application and platform stores shared by hosting API contracts."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from core.auth.backend import get_current_user
from core.services.application_store import initialize_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(params=["sqlite", "postgres"])
def hosted(tmp_path, monkeypatch, request):
    from api.routes.v1 import application_transports, applications

    from unittest.mock import AsyncMock
    monkeypatch.setattr("core.services.site_rate_limit.redis_configured", lambda: True)
    monkeypatch.setattr("core.services.site_rate_limit.get_redis", lambda: SimpleNamespace(eval=AsyncMock(return_value=1)))
    admin = None
    database = "test_applications_" + uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("TEST_APPLICATION_POSTGRES_URL")
        if not url:
            pytest.skip("Disposable TEST_APPLICATION_POSTGRES_URL required")
        admin = create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        engine = create_engine(make_url(url).set(database=database))
    else:
        engine = create_engine(
            "sqlite:///" + str(tmp_path / "data.db"), connect_args={"check_same_thread": False}
        )
    initialize_store(engine)
    monkeypatch.setattr(
        "core.config.application_hosting.application_hosting_settings",
        SimpleNamespace(database_url=str(engine.url)),
    )
    monkeypatch.setattr("core.services.application_data.application_engine", lambda: engine)
    monkeypatch.setattr("core.services.application_deployments.application_engine", lambda: engine)
    from core.db.engine import get_db
    from core.db.models import AdminMcpServer
    from sqlalchemy.orm import sessionmaker

    platform_engine = create_engine(
        "sqlite:///" + str(tmp_path / "platform.db"), connect_args={"check_same_thread": False}
    )
    from core.db.engine import Base
    from core.db.models import UserShadow

    Base.metadata.create_all(platform_engine)
    platform_sessions = sessionmaker(platform_engine)
    with platform_sessions() as db:
        db.add(UserShadow(user_id="owner", username="owner", email="owner@example.com"))
        db.commit()
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr("core.storage.factory._storage_instance", None)

    def platform_dependency():
        with platform_sessions() as db:
            yield db

    app = FastAPI()
    app.dependency_overrides[get_db] = platform_dependency
    app.include_router(applications.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="owner")
    with TestClient(app) as client:
        yield client, engine
    platform_engine.dispose()
    if admin is not None:
        from core.services.application_store import applications
        from sqlalchemy import select

        with engine.connect() as connection:
            ids = list(connection.scalars(select(applications.c.id)))
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
            for app_id in ids:
                connection.execute(text(f'DROP ROLE IF EXISTS "app_role_{app_id}"'))
        admin.dispose()
    else:
        engine.dispose()
