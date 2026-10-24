"""WB19 authenticated diagnostic HTTP seams, using isolated persistence."""

import pytest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import UserShadow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.db_support import share_session


@pytest.fixture()
def client(db_session, monkeypatch):
    from features.workbench.api.workbench import router

    monkeypatch.setenv("CONFIG_TOKEN", "wb19-config")
    db_session.add(UserShadow(user_id="wb19-reader", username="Reader", extra_data={}))
    db_session.commit()
    app = FastAPI()
    app.include_router(router)

    def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    share_session(monkeypatch, db_session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="wb19-reader", user_center_id="wb19-reader", username="Reader"
    )
    return TestClient(app)


def test_failed_launch_has_private_diagnostic_and_admin_query(client):
    path = "/v1/workbench"
    response = client.post(path + "/applications/external:absent/launch", json={})
    assert response.status_code == 404
    diagnostic = response.json()["detail"]["diagnosticId"]
    own = client.get(path + "/me/diagnostics/" + diagnostic)
    assert own.status_code == 200
    assert own.json()["data"]["code"] == "app_not_found"
    assert "url" not in own.text
    assert client.get(path + "/admin/diagnostics").status_code in (401, 403)
    admin = client.get(path + "/admin/diagnostics", headers={"Authorization": "Bearer wb19-config"})
    assert admin.status_code == 200
    assert diagnostic in admin.text


def test_user_cannot_read_another_diagnostic(client):
    assert client.get("/v1/workbench/me/diagnostics/" + "a" * 32).status_code == 404


def test_admin_preview_reuses_validation_without_echoing_secrets(client):
    response = client.post(
        "/v1/workbench/admin/diagnostics/preview",
        headers={"Authorization": "Bearer wb19-config"},
        json={
            "declaration": {
                "schemaVersion": "1.0.0",
                "id": "reports",
                "title": "Reports",
                "target": {
                    "kind": "external",
                    "mode": "browser",
                    "url": "https://user:secret@example.org",
                },
            }
        },
    )
    assert response.status_code == 422
    assert "secret" not in response.text


def test_probe_rejects_private_ip_without_connecting(client):
    admin = {"Authorization": "Bearer wb19-config"}
    declaration = {
        "schemaVersion": "1.0.0",
        "id": "wb19_loopback",
        "title": "Loopback",
        "target": {"kind": "external", "mode": "browser", "url": "http://127.0.0.1/"},
    }
    assert (
        client.post(
            "/v1/workbench/admin/external-applications",
            headers=admin,
            json={"declaration": declaration, "enabled": True},
        ).status_code
        == 200
    )
    response = client.post(
        "/v1/workbench/admin/diagnostics/probe/external:wb19_loopback", headers=admin, json={}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "probe_target_denied"


def test_other_account_cannot_fetch_receipt_even_with_owner_query(client):
    from core.auth.backend import get_current_user, UserContext

    result = client.post("/v1/workbench/applications/external:missing/launch", json={})
    identity = result.json()["detail"]["diagnosticId"]
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other", user_center_id="other", username="Other"
    )
    response = client.get("/v1/workbench/me/diagnostics/" + identity + "?user_id=wb19-reader")
    assert response.status_code == 404
    assert client.get("/v1/workbench/me/diagnostics").json()["data"]["items"] == []


def test_admin_checks_real_user_and_missing_user_is_not_invented(client):
    headers = {"Authorization": "Bearer wb19-config"}
    response = client.get("/v1/workbench/admin/diagnostics?user_id=wb19-reader", headers=headers)
    assert response.status_code == 200
    chat = next(
        item
        for item in response.json()["data"]["applications"]
        if item["applicationKey"] == "builtin:chat"
    )
    assert chat["permissionStatus"] == "authorized"
    assert (
        client.get("/v1/workbench/admin/diagnostics?user_id=missing", headers=headers).status_code
        == 404
    )


def test_declared_private_https_is_never_probed(client):
    headers = {"Authorization": "Bearer wb19-config"}
    app = {
        "schemaVersion": "1.0.0",
        "id": "wb19_private",
        "title": "Private",
        "target": {"kind": "external", "mode": "browser", "url": "https://127.0.0.1/"},
    }
    assert (
        client.post(
            "/v1/workbench/admin/external-applications",
            headers=headers,
            json={"declaration": app, "enabled": True},
        ).status_code
        == 200
    )
    result = client.post(
        "/v1/workbench/admin/diagnostics/probe/external:wb19_private", headers=headers, json={}
    )
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "probe_target_denied"


def test_browser_failure_reports_are_validated_without_secrets(client):
    result = client.post(
        "/v1/workbench/applications/builtin:chat/diagnostics",
        json={"code": "page_failed", "token": "never-log"},
    )
    assert result.status_code == 422
    assert "never-log" not in result.text
    result = client.post(
        "/v1/workbench/applications/builtin:chat/diagnostics", json={"code": "page_failed"}
    )
    assert result.status_code == 200
    assert result.json()["data"]["appKey"] == "builtin:chat"
    assert result.json()["data"]["installId"] is None
    assert result.json()["data"]["instanceId"] is None


@pytest.fixture()
def probe_target(client):
    headers = {"Authorization": "Bearer wb19-config"}
    app = {
        "schemaVersion": "1.0.0",
        "id": "wb19_target",
        "title": "Target",
        "target": {"kind": "external", "mode": "browser", "url": "https://reports.example.org/"},
    }
    assert (
        client.post(
            "/v1/workbench/admin/external-applications",
            headers=headers,
            json={"declaration": app, "enabled": True},
        ).status_code
        == 200
    )
    return headers


def network(monkeypatch, addresses, peer="93.184.216.34", status=200):
    """External socket/TLS test double; not evidence of native network success."""
    import io
    import socket
    from types import SimpleNamespace
    from features.workbench import diagnostic_probe

    connected = []

    class Wire:
        def settimeout(self, timeout):
            assert timeout <= 2

        def connect(self, address):
            connected.append(address)

        def getpeername(self):
            return (peer, 443)

        def sendall(self, value):
            assert value.startswith(b"HEAD ")
            assert b"Authorization" not in value and b"Cookie" not in value

        def makefile(self, *args, **kwargs):
            return io.BytesIO(
                (
                    "HTTP/1.1 "
                    + str(status)
                    + " status\r\nLocation: https://127.0.0.1/\r\nContent-Length: 0\r\n\r\n"
                ).encode()
            )

        def close(self):
            pass

    monkeypatch.setattr(
        diagnostic_probe,
        "socket",
        SimpleNamespace(
            SOCK_STREAM=socket.SOCK_STREAM,
            socket=lambda *_: Wire(),
            getaddrinfo=lambda *_: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses
            ],
        ),
    )
    monkeypatch.setattr(
        diagnostic_probe,
        "ssl",
        SimpleNamespace(
            SSLError=socket.error,
            create_default_context=lambda: SimpleNamespace(
                wrap_socket=lambda sock, server_hostname: sock
            ),
        ),
    )
    return connected


def test_probe_rejects_mixed_dns_without_connection(client, probe_target, monkeypatch):
    connected = network(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    response = client.post(
        "/v1/workbench/admin/diagnostics/probe/external:wb19_target", headers=probe_target, json={}
    )
    assert response.status_code == 422
    assert connected == []


def test_probe_checks_peer_and_does_not_follow_redirect(client, probe_target, monkeypatch):
    connected = network(monkeypatch, ["93.184.216.34"], peer="127.0.0.1")
    response = client.post(
        "/v1/workbench/admin/diagnostics/probe/external:wb19_target", headers=probe_target, json={}
    )
    assert response.status_code == 422
    assert connected == [("93.184.216.34", 443)]
    connected = network(monkeypatch, ["93.184.216.34"], status=302)
    response = client.post(
        "/v1/workbench/admin/diagnostics/probe/external:wb19_target", headers=probe_target, json={}
    )
    assert response.status_code == 422
    assert connected == [("93.184.216.34", 443)]
    assert "127.0.0.1" not in response.text


def test_self_resource_observation_distinguishes_registration_from_process_count(client, monkeypatch):
    # Explicitly model unavailable schema independently of collection order or SQL dialect.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    empty = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    original = client.app.dependency_overrides[get_db]
    try:
        with Session(empty) as db:
            client.app.dependency_overrides[get_db] = lambda: db
            share_session(monkeypatch, db)
            response = client.get("/v1/workbench/me/diagnostics/resource-metrics?user_id=other")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            value = response.json()["data"]
            assert value["registration"]["available"] is False
            assert value["runtime"]["complete"] is False
            assert value["runtime"]["appOwnerRunningWorkers"] is None
    finally:
        client.app.dependency_overrides[get_db] = original
        empty.dispose()
