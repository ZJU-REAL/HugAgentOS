"""Public device-login API: browser approval never conveys the device secret."""
import asyncio
import dataclasses
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config.settings import settings
from core.auth.session import create_session


@pytest.fixture(params=["memory", "redis"])
def client(request, monkeypatch, tmp_path):
    import subprocess
    import time
    from redis.asyncio import Redis
    from core.auth import desktop_login_store as store, session
    from api.routes.v1.desktop_login import router
    previous = settings.session
    mode = request.param
    object.__setattr__(settings, "session", dataclasses.replace(previous, store_type=mode))
    store._rates.clear()
    store._memory.clear()
    process = None
    redis = None
    if mode == "redis":
        socket = str(tmp_path / "redis.sock")
        process = subprocess.Popen(["redis-server", "--port", "0", "--unixsocket", socket,
                                    "--save", "", "--appendonly", "no"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if __import__("os").path.exists(socket):
                break
            time.sleep(.02)
        redis = Redis(unix_socket_path=socket)
        monkeypatch.setattr(store, "get_redis", lambda: redis)
        monkeypatch.setattr(session, "get_redis", lambda: redis)
    app = FastAPI()
    app.include_router(router)
    try:
        with TestClient(app) as client:
            yield client
            if redis:
                client.portal.call(redis.aclose)
    finally:
        object.__setattr__(settings, "session", previous)
        if process:
            process.terminate()
            process.wait(timeout=5)


def begin(client):
    secret = secrets.token_hex(32)
    response = client.post("/v1/auth/desktop/requests", json={"device_secret": secret})
    assert response.status_code == 200, response.text
    return response.json()["data"], secret


def browser_login(client):
    token = client.portal.call(create_session, {"user_id": "device-test", "username": "测试账号"})
    client.cookies.set(settings.session.cookie_name, token)
    return token


def test_approval_requires_explicit_confirmation_and_device_secret(client):
    grant, secret = begin(client)
    path = "/v1/auth/desktop/requests/" + grant["request_id"]
    token = browser_login(client)
    page = client.get(path).json()["data"]
    assert page["status"] == "pending"
    assert page["username"] == "测试账号"
    assert "token" not in page and "device_secret" not in page
    assert secret not in str(grant)
    assert client.post(path + "/poll", json={"device_secret": secret}).json()["data"]["status"] == "pending"
    assert client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                       headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post(path + "/poll", json={"device_secret": "0" * 64}).status_code == 404
    delivered = client.post(path + "/poll", json={"device_secret": secret}).json()["data"]
    assert delivered["token"] == token
    # A lost response can be recovered by the same device until its acknowledgement.
    assert client.post(path + "/poll", json={"device_secret": secret}).json()["data"]["token"] == token
    assert client.post(path + "/ack", json={"device_secret": secret}).json()["data"]["status"] == "completed"
    assert "token" not in client.post(path + "/poll", json={"device_secret": secret}).json()["data"]
    assert client.get(path).json()["data"]["status"] == "completed"


@pytest.mark.parametrize("terminal", ["cancel", "deny"])
def test_cancel_or_deny_prevents_delivery(client, terminal):
    grant, secret = begin(client)
    path = "/v1/auth/desktop/requests/" + grant["request_id"]
    browser_login(client)
    if terminal == "cancel":
        r = client.post(path + "/cancel", json={"device_secret": secret})
    else:
        r = client.post(path + "/deny", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                        headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                       headers={"Origin": "http://testserver"}).status_code == 409
    assert "token" not in client.post(path + "/poll", json={"device_secret": secret}).json()["data"]


def test_expiry_and_revoked_browser_session(client, monkeypatch):
    from core.auth import desktop_login_store as store
    from core.auth.session import revoke_session
    grant, secret = begin(client)
    path = "/v1/auth/desktop/requests/" + grant["request_id"]
    token = browser_login(client)
    client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                headers={"Origin": "http://testserver"})
    client.portal.call(revoke_session, token)
    assert client.post(path + "/poll", json={"device_secret": secret}).json()["data"]["status"] == "cancelled"
    grant, secret = begin(client)
    now = store.time.time()
    monkeypatch.setattr(store.time, "time", lambda: now + 301)
    assert client.post("/v1/auth/desktop/requests/" + grant["request_id"] + "/poll",
                       json={"device_secret": secret}).status_code == 410


def test_concurrent_approval_and_ack_are_single_grant(client):
    from concurrent.futures import ThreadPoolExecutor
    grant, secret = begin(client)
    path = "/v1/auth/desktop/requests/" + grant["request_id"]
    token = browser_login(client)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.post(path + "/approve",
            json={"confirm_code": grant["confirm_code"], "account_id": "device-test"}, headers={"Origin": "http://testserver"}), range(6)))
    assert all(r.status_code == 200 for r in responses)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.post(path + "/poll", json={"device_secret": secret}), range(6)))
    assert all(r.json()["data"]["token"] == token for r in responses)
    client.post(path + "/ack", json={"device_secret": secret})
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.post(path + "/poll", json={"device_secret": secret}), range(6)))
    assert all(r.json()["data"]["status"] == "completed" and "token" not in r.json()["data"] for r in responses)


def test_wrong_code_missing_cookie_and_foreign_account(client):
    grant, secret = begin(client)
    path = "/v1/auth/desktop/requests/" + grant["request_id"]
    assert client.get(path).status_code == 401
    assert client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                       headers={"Origin": "http://testserver"}).status_code == 401
    browser_login(client)
    assert client.post(path + "/approve", json={"confirm_code": "BADCODE!", "account_id": "device-test"},
                       headers={"Origin": "http://testserver"}).status_code == 400
    client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                headers={"Origin": "http://testserver"})
    other = client.portal.call(create_session, {"user_id": "other", "username": "其他账号"})
    client.cookies.set(settings.session.cookie_name, other)
    assert client.get(path).status_code == 403
    assert client.post(path + "/approve", json={"confirm_code": grant["confirm_code"], "account_id": "device-test"},
                       headers={"Origin": "http://testserver"}).status_code == 409


def test_rate_limit_and_no_store(client):
    from core.auth import desktop_login_store as store
    secret = secrets.token_hex(32)
    for _ in range(10):
        r = client.post("/v1/auth/desktop/requests", json={"device_secret": secret})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
    assert client.post("/v1/auth/desktop/requests", json={"device_secret": secret}).status_code == 429

def test_account_switch_between_display_and_confirmation_is_rejected(client):
    grant, secret = begin(client)
    path = "/v1/auth/desktop/requests/" + grant["request_id"]
    browser_login(client)
    displayed = client.get(path).json()["data"]
    other = client.portal.call(create_session, {"user_id": "other", "username": "另一账号"})
    client.cookies.set(settings.session.cookie_name, other)
    response = client.post(path + "/approve", json={"confirm_code": displayed["confirm_code"],
                                                  "account_id": displayed["account_id"]},
                           headers={"Origin": "http://testserver"})
    assert response.status_code == 409
    assert client.post(path + "/poll", json={"device_secret": secret}).json()["data"]["status"] == "pending"

def test_proxy_peers_do_not_share_the_device_quota(client):
    for _ in range(12):
        begin(client)
