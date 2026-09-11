"""PostgreSQL row contention must not freeze site HTTP or health requests.

Opt in with TEST_POSTGRES_URL pointing to a disposable/local PostgreSQL database.
Every test run creates and removes its own schema; no existing tables are touched.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from api.health import router as health_router
from api.routes import sites_serve
from api.routes.v1 import internal_sites, sites
from core.auth.backend import get_current_user
from core.db.engine import Base, get_db
from core.db.models import Site, SiteKV, UserShadow
from core.services.site_service import SiteService
from core.storage.local import LocalStorageBackend
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.postgres


@pytest.fixture
def site_http(tmp_path, monkeypatch):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL site contention tests")

    schema = "test_site_contention_" + uuid4().hex[:12]
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    # Bound the broken version's wait so a regression fails instead of hanging pytest.
    engine = create_engine(
        url,
        connect_args={
            "application_name": schema,
            "options": f"-c search_path={schema} -c lock_timeout=2000 -c statement_timeout=5000",
        },
    )
    factory = sessionmaker(bind=engine)
    try:
        Base.metadata.create_all(engine)
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
        storage = LocalStorageBackend()
        monkeypatch.setattr("core.storage.get_storage", lambda: storage)
        monkeypatch.setattr("core.services.site_service.get_storage", lambda: storage)
        monkeypatch.setattr("core.db.engine.SessionLocal", factory)
        monkeypatch.setattr("core.services.desktop_cloud_bridge.bridge_enabled", lambda: False)
        monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "site-test-only")

        async def pack_directory(*args, **kwargs):
            return [("index.html", b"<h1>updated</h1>")], None

        monkeypatch.setattr(internal_sites, "pack_and_fetch_dir", pack_directory)
        with factory() as db:
            db.add(UserShadow(user_id="site-owner", username="site-owner"))
            db.commit()
            service = SiteService(db)
            site = service.publish(
                user_id="site-owner",
                title="Contention fixture",
                slug="contention-fixture",
                files=[("index.html", b"<h1>site</h1>")],
            )
            site_id = site.site_id
            service.kv_set(site, "existing", "before")

        def database():
            with factory() as db:
                yield db

        app = FastAPI()
        app.include_router(health_router)
        app.include_router(sites.router)
        app.include_router(sites_serve.router)
        app.include_router(internal_sites.router)
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="site-owner")
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, factory, engine, schema, site_id
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def test_site_panel_reads_do_not_wait_for_site_write_lock(site_http):
    client, factory, _, _, site_id = site_http
    with factory() as holder:
        holder.query(Site).filter_by(site_id=site_id).with_for_update().one()
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(client.get, f"/v1/sites/{site_id}{suffix}")
                    for suffix in ("", "/submissions", "/kv")
                ]
                responses = [future.result(timeout=6) for future in futures]
            assert [r.status_code for r in responses] == [200, 200, 200]
            assert client.get("/health").status_code == 200
        finally:
            holder.rollback()


@pytest.mark.parametrize("operation", ["page", "kv-set", "kv-delete", "form", "publish"])
def test_site_lock_wait_does_not_block_health(site_http, operation):
    client, factory, engine, schema, site_id = site_http
    root = "/site/contention-fixture"
    requests = {
        "page": ("GET", root + "/", {}),
        "kv-set": ("PUT", root + "/__api/kv/new", {"json": {"value": "new"}}),
        "kv-delete": ("DELETE", root + "/__api/kv/existing", {}),
        "form": ("POST", root + "/__api/forms/contact", {"json": {"message": "hi"}}),
        "publish": (
            "POST",
            "/v1/internal/sites/publish",
            {
                "headers": {"X-Internal-Token": "site-test-only"},
                "json": {"user_id": "site-owner", "title": "Updated", "site_id": site_id},
            },
        ),
    }
    with factory() as holder:
        if operation == "kv-delete":
            holder.query(SiteKV).filter_by(site_id=site_id, k="existing").with_for_update().one()
        else:
            holder.query(Site).filter_by(site_id=site_id).with_for_update().one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            method, path, kwargs = requests[operation]
            pending = pool.submit(client.request, method, path, **kwargs)
            try:
                # Verify that we actually exercised a PostgreSQL lock wait before probing.
                deadline = monotonic() + 4
                waiting = False
                while monotonic() < deadline and not pending.done():
                    with engine.connect() as db:
                        waiting = db.execute(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE application_name=:name AND wait_event_type='Lock')"
                            ),
                            {"name": schema},
                        ).scalar()
                    if waiting:
                        break
                    sleep(0.01)
                assert waiting, "request did not reach the intended row-lock contention"
                started = monotonic()
                health = client.get("/health")
                elapsed = monotonic() - started
            finally:
                holder.rollback()
            response = pending.result(timeout=6)
        assert health.status_code == 200
        assert elapsed < 0.75, f"{operation} blocked unrelated health requests for {elapsed:.2f}s"
        assert response.status_code == (201 if operation == "form" else 200), response.text
        if operation == "publish":
            assert response.json()["data"].get("ok"), response.text


def test_view_count_timeout_does_not_break_the_page(site_http):
    client, factory, _, _, site_id = site_http
    with factory() as holder:
        holder.query(Site).filter_by(site_id=site_id).with_for_update().one()
        try:
            # Let the counting UPDATE really reach lock_timeout, not an early unlock.
            response = client.get("/site/contention-fixture/")
            assert response.status_code == 200
            assert response.content == b"<h1>site</h1>"
            assert "sandbox" in response.headers["content-security-policy"]
        finally:
            holder.rollback()
    assert client.get("/site/contention-fixture/").status_code == 200
    assert client.get("/health").status_code == 200
