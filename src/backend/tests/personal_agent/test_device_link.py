"""Desktop device link: local consent, at-most-once execution and account isolation."""

import json
import time

import httpx
import pytest

from personal_agent.device_link import runner as link_runner
from personal_agent.device_link import store

ACCOUNT = "cloud:cloud.example:443:owner"
CLOUD = "https://cloud.example"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path))
    monkeypatch.setattr(link_runner, "current_account", lambda: (ACCOUNT, CLOUD))
    monkeypatch.setattr(link_runner, "_bridge_headers", lambda account: {"x-desktop-bridge": "s"})
    return tmp_path


def make_link(**consent):
    link = store.Link(device_id="pa_device_1", token="t" * 32, name="Laptop", cloud_base=CLOUD,
                      control_enabled=consent.get("control", False), sessions_enabled=consent.get("sessions", False))
    store.save(ACCOUNT, link)
    return link


class Cloud:
    """The device mailbox as the cloud serves it."""

    def __init__(self, command=None, operation=None):
        self.command, self.operation = command, operation
        self.calls, self.receipts = [], []

    def __call__(self, request: httpx.Request):
        path = request.url.path.split("/pa_device_1", 1)[1]
        self.calls.append((request.method, path))
        if request.method == "POST" and path.startswith(("/commands/", "/sessions/operations/")):
            self.receipts.append(json.loads(request.content)["result"])
            return httpx.Response(200, json={"data": {"accepted": True}})
        if path == "/commands":
            command, self.command = self.command, None
            return httpx.Response(200, json={"data": command})
        if path == "/sessions/operations":
            operation, self.operation = self.operation, None
            return httpx.Response(200, json={"data": operation})
        if path.endswith("/check"):
            return httpx.Response(200, json={"data": {"permitted": True}})
        return httpx.Response(200, json={"data": {}})


def mailbox(link, cloud):
    box = link_runner.Mailbox(ACCOUNT, link)
    box.http = httpx.AsyncClient(base_url=CLOUD + "/api/v1/personal-agent/device-link/pa_device_1",
                                 transport=httpx.MockTransport(cloud))
    return box


async def test_consent_is_reported_before_polling():
    cloud = Cloud()
    box = mailbox(make_link(control=True), cloud)
    await box.step()
    await box.close()
    assert cloud.calls[:2] == [("POST", "/consent"), ("POST", "/sessions/consent")]
    assert ("GET", "/commands") in cloud.calls


async def test_command_runs_once_and_never_while_control_is_off(monkeypatch):
    ran = []

    async def driver(action):
        ran.append(action)
        return {"status": "input_executed"}

    monkeypatch.setattr(link_runner, "run_driver", driver)
    command = {"id": "c1", "action": {"type": "click", "x": 1, "y": 1}, "deadline": time.time() + 30}
    cloud = Cloud(command=command)
    box = mailbox(make_link(control=False), cloud)
    assert await box.step()
    assert not ran and cloud.receipts[0]["waiting"]
    cloud.command = command  # redelivered: the journal refuses it
    box.link.control_enabled = True
    assert not await box.step()
    assert not ran
    await box.close()


async def test_unknown_operation_is_refused_without_executing(monkeypatch):
    async def execute(*args):
        raise AssertionError("must not execute")

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    operation = {"id": "o1", "operation": "shell.exec", "args": {}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    box = mailbox(make_link(sessions=True), cloud)
    assert await box.step()
    assert cloud.receipts[0]["not_executed"]
    cloud.operation = operation
    assert not await box.step()
    await box.close()


async def test_consent_withdrawn_during_cloud_check_does_not_write(monkeypatch):
    async def execute(*args):
        raise AssertionError("must not execute")

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    link = make_link(sessions=True)
    operation = {"id": "o2", "operation": "chat.create", "args": {"title": "A"}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    original = cloud.__call__

    def withdraw(request):
        if request.url.path.endswith("/check"):
            store.save(ACCOUNT, store.Link(**{**link.__dict__, "sessions_enabled": False}))
        return original(request)

    box = mailbox(link, withdraw)
    assert await box.step()
    assert cloud.receipts[0]["error"] == "permission_revoked"
    await box.close()


async def test_operation_for_another_account_is_not_executed(monkeypatch):
    async def execute(*args):
        raise AssertionError("must not execute")

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    operation = {"id": "o3", "operation": "chat.list", "args": {}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    box = mailbox(make_link(sessions=True), cloud)
    monkeypatch.setattr(link_runner, "current_account", lambda: ("cloud:cloud.example:443:other", CLOUD))
    assert await box.step()
    assert cloud.receipts[0]["error"] == "account_changed"
    await box.close()


def test_an_action_started_before_a_crash_is_reported_unknown_not_replayed():
    journal = store.Journal(ACCOUNT)
    journal.start("command", "c1")
    journal.close()
    journal = store.Journal(ACCOUNT)
    assert journal.seen("command", "c1")
    assert journal.pending() == [("command", "c1", {"outcome_unknown": True, "error": "interrupted; check the result before retry"})]
    journal.close()


def test_links_are_private_and_per_account(home):
    make_link()
    assert store.load(ACCOUNT).device_id == "pa_device_1"
    assert store.load("cloud:cloud.example:443:other") is None
    assert link_runner.current_link((ACCOUNT, "https://elsewhere.example")) is None
    path = store.account_dir(ACCOUNT) / "link.json"
    assert path.stat().st_mode & 0o077 == 0
    store.delete(ACCOUNT)
    assert store.load(ACCOUNT) is None


def test_probe_never_reaches_the_cloud_vocabulary():
    from personal_agent.device_link import CAPABILITIES

    assert "probe" not in CAPABILITIES and "request_permissions" not in CAPABILITIES


@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from features.desktop.api import device_link
    from core.auth.backend import UserContext, get_current_user

    async def probe(action):
        return {"control": True, "permissions": None}

    monkeypatch.setattr(device_link, "current_account", lambda: (ACCOUNT, CLOUD))
    monkeypatch.setattr(device_link, "run_driver", probe)
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "secret")
    app = FastAPI()
    app.include_router(device_link.router)
    user = {"user_center_id": ACCOUNT}
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u1", user_center_id=user["user_center_id"], username="owner")
    return TestClient(app), user


def test_routes_exist_only_in_the_desktop_execution_plane(api, monkeypatch):
    client, _ = api
    monkeypatch.delenv("HUGAGENT_DESKTOP_BRIDGE_SECRET")
    assert client.get("/v1/device-link").status_code == 404


def test_only_the_signed_in_desktop_account_can_link(api):
    client, user = api
    user["user_center_id"] = "cloud:cloud.example:443:other"
    assert client.get("/v1/device-link").status_code == 403


def test_link_consent_and_unlink(api):
    client, _ = api
    status = client.get("/v1/device-link").json()["data"]
    assert status["link"] is None and "hugagent.sessions" in status["capabilities"]
    body = {"device_id": "pa_device_1", "token": "t" * 32, "name": "Laptop"}
    linked = client.put("/v1/device-link", json=body).json()["data"]["link"]
    assert linked == {"device_id": "pa_device_1", "name": "Laptop", "control_enabled": False, "sessions_enabled": False}
    assert store.load(ACCOUNT).cloud_base == CLOUD
    changed = client.patch("/v1/device-link", json={"sessions_enabled": True}).json()["data"]["link"]
    assert changed["sessions_enabled"] and not changed["control_enabled"]
    assert client.delete("/v1/device-link").json()["data"]["link"] is None
    assert client.patch("/v1/device-link", json={"control_enabled": True}).status_code == 404
