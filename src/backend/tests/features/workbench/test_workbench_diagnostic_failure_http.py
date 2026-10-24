"""Real router/session/database outcomes survive optional diagnostic writer failure."""

import asyncio

import pytest
from core.auth.session import create_session
from core.config.settings import settings
from core.db.engine import get_db
from core.db.models import UserShadow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.db_support import share_session

BASE = "/v1/workbench"
KEY = "external:telemetry_failure"
DECLARATION = {
    "schemaVersion": "1.0.0",
    "id": "telemetry_failure",
    "title": "Telemetry failure",
    "target": {"kind": "external", "mode": "browser", "url": "https://reports.example.org/"},
}


@pytest.fixture()
def client(db_session, monkeypatch):
    from features.workbench.api.workbench import router
    from features.workbench import diagnostics

    assert settings.auth.mode == "session"
    assert settings.session.store_type == "memory"
    monkeypatch.setenv("CONFIG_TOKEN", "diagnostic-failure-test-config")
    db_session.add(UserShadow(user_id="diagnostic-failure-user", username="Reader", extra_data={}))
    db_session.commit()
    app = FastAPI()
    app.include_router(router)

    def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    share_session(monkeypatch, db_session)
    token = asyncio.run(create_session({"user_id": "diagnostic-failure-user", "username": "Reader"}))
    with TestClient(app) as browser:
        browser.cookies.set(settings.session.cookie_name, token)
        response = browser.post(
            BASE + "/admin/external-applications",
            headers={"Authorization": "Bearer diagnostic-failure-test-config"},
            json={"declaration": DECLARATION, "enabled": True},
        )
        assert response.status_code == 200, response.text

        def unavailable(*args, **kwargs):
            raise RuntimeError("diagnostic-storage-fault-with-private-detail")

        monkeypatch.setattr(diagnostics, "record", unavailable)
        yield browser


def test_success_and_conditional_catalog_keep_original_outcomes(client):
    result = client.get(BASE + "/catalog")
    assert result.status_code == 200, result.text
    assert KEY in [item["applicationKey"] for item in result.json()["data"]["items"]]
    etag = result.headers["etag"]
    unchanged = client.get(BASE + "/catalog", headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag
    launch = client.post(BASE + "/applications/" + KEY + "/launch", json={})
    assert launch.status_code == 200, launch.text
    assert launch.json()["data"]["target"] == DECLARATION["target"]
    assert launch.headers["cache-control"] == "no-store"
    assert "diagnosticId" not in launch.text
    assert "private-detail" not in launch.text


def test_denied_origin_retains_403_without_invented_receipt(client):
    result = client.post(
        BASE + "/applications/" + KEY + "/launch",
        headers={"Origin": "https://untrusted.example.org", "Sec-Fetch-Site": "cross-site"},
        json={},
    )
    assert result.status_code == 403
    assert result.json()["detail"]["code"] == "invalid_origin"
    assert result.headers["cache-control"] == "no-store"
    assert "diagnosticId" not in result.text
    assert "private-detail" not in result.text


def test_disabled_application_retains_409_without_invented_receipt(client):
    updated = client.put(
        BASE + "/admin/external-applications/" + KEY,
        headers={"Authorization": "Bearer diagnostic-failure-test-config"},
        json={"declaration": DECLARATION, "enabled": False, "revision": 1},
    )
    assert updated.status_code == 200, updated.text
    result = client.post(BASE + "/applications/" + KEY + "/launch", json={})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "app_disabled"
    assert result.headers["cache-control"] == "no-store"
    assert "diagnosticId" not in result.text
    assert "private-detail" not in result.text
