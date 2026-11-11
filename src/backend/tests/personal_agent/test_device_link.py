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
    # Never start a real Cua Driver from a test, whatever computer runs it.
    monkeypatch.setattr(link_runner.ui_host, "_system", lambda: "Linux")
    return tmp_path


def make_link(**consent):
    link = store.Link(device_id="pa_device_1", token="t" * 32, name="Laptop", cloud_base=CLOUD,
                      control_enabled=consent.get("control", False), sessions_enabled=consent.get("sessions", False))
    store.save(ACCOUNT, link)
    return link


class Cloud:
    """The device mailbox as the cloud serves it."""

    def __init__(self, command=None, operation=None, protocol=2, clock=0.0):
        self.command, self.operation = command, operation
        self.protocol, self.clock = protocol, clock
        self.calls, self.receipts, self.bodies = [], [], []

    def __call__(self, request: httpx.Request):
        path = request.url.path.split("/pa_device_1", 1)[1]
        self.calls.append((request.method, path))
        if request.content:
            self.bodies.append((path, json.loads(request.content)))
        if path in ("/heartbeat", "/claim") and self.protocol < 2:
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/heartbeat":
            return httpx.Response(200, json={"data": {"protocol": 2, "server_time": time.time() + self.clock}})
        if path == "/claim":
            command, self.command = self.command, None
            operation, self.operation = self.operation, None
            return httpx.Response(200, json={"data": {"command": command, "operation": operation}})
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


async def settle(box):
    """Let the lanes finish their actions and deliver every receipt."""
    import asyncio

    await asyncio.gather(*[task for task in box.lanes.values() if task], return_exceptions=True)
    await box.flush()


def mailbox(link, cloud):
    box = link_runner.Mailbox(ACCOUNT, link)
    box.http = httpx.AsyncClient(base_url=CLOUD + "/api/v1/personal-agent/device-link/pa_device_1",
                                 transport=httpx.MockTransport(cloud))
    return box


async def test_consent_is_reported_before_claiming_work():
    cloud = Cloud()
    box = mailbox(make_link(control=True), cloud)
    await box.step()
    await box.close()
    assert cloud.calls == [("POST", "/heartbeat"), ("POST", "/claim")]
    path, body = cloud.bodies[0]
    assert path == "/heartbeat" and body["control_enabled"] and not body["sessions_enabled"]
    # Contract A.5: the batch commands are reported; element support only with the accessibility host.
    assert {"read", "steps", "hugagent.sessions", "click"} <= set(body["capabilities"])
    assert "ui.elements" not in body["capabilities"]
    assert box.protocol == 2


async def test_an_older_cloud_is_served_with_the_legacy_routes():
    cloud = Cloud(protocol=1)
    box = mailbox(make_link(control=True), cloud)
    await box.step()
    await box.step()
    await box.close()
    assert box.protocol == 1
    assert cloud.calls[:3] == [("POST", "/heartbeat"), ("POST", "/consent"), ("POST", "/sessions/consent")]
    # Session consent goes once: an older cloud starts a new epoch on every "off".
    assert cloud.calls.count(("POST", "/sessions/consent")) == 1
    assert ("GET", "/commands") in cloud.calls and ("GET", "/sessions/operations") in cloud.calls
    assert ("POST", "/claim") not in cloud.calls


async def test_a_revoked_device_is_unavailable_on_either_protocol():
    def revoked(request):
        return httpx.Response(404, json={"detail": "Device unavailable"})

    box = mailbox(make_link(), revoked)
    with pytest.raises(link_runner.DeviceUnavailable):
        await box.heartbeat()
    await box.close()


async def test_deadlines_are_judged_on_the_cloud_clock(monkeypatch):
    """This computer's clock runs 10 minutes fast; a live operation must still run."""
    ran = []

    async def execute(*args):
        ran.append(args)
        return {"items": []}

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    cloud_time = time.time()
    monkeypatch.setattr(link_runner.time, "time", lambda: cloud_time + 600 + (time.monotonic() % 1))
    operation = {"id": "o7", "operation": "chat.list", "args": {}, "deadline": cloud_time + 30}
    cloud = Cloud(operation=operation, clock=-600)
    box = mailbox(make_link(sessions=True), cloud)
    assert await box.step()
    await settle(box)
    assert ran and cloud.receipts == [{"items": []}]
    await box.close()


async def test_command_runs_once_and_never_while_control_is_off(monkeypatch):
    ran = []

    async def driver(action, timeout=None):
        ran.append(action)
        return {"status": "input_executed"}

    monkeypatch.setattr(link_runner.dispatch, "run_driver", driver)
    command = {"id": "c1", "action": {"type": "click", "x": 1, "y": 1}, "deadline": time.time() + 30}
    cloud = Cloud(command=command)
    box = mailbox(make_link(control=False), cloud)
    assert await box.step()
    await settle(box)
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
    await settle(box)
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
    await settle(box)
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
    await settle(box)
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


async def test_heartbeat_restates_consent_and_marks_contact():
    cloud = Cloud()
    box = mailbox(make_link(control=True), cloud)
    await box.heartbeat()
    await box.heartbeat()
    await box.close()
    assert cloud.calls == [("POST", "/heartbeat"), ("POST", "/heartbeat")]
    assert box.contact == "connected" and box.last_contact


async def test_heartbeat_keeps_running_while_an_action_is_slow(monkeypatch):
    """A long action must not stop liveness reports: the cloud would call the device offline."""
    import asyncio

    monkeypatch.setattr(link_runner, "HEARTBEAT", 0.01)
    release = asyncio.Event()

    async def execute(*args):
        await release.wait()
        return {"items": []}

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    operation = {"id": "o4", "operation": "chat.list", "args": {}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    box = mailbox(make_link(sessions=True), cloud)
    box.start()
    step = asyncio.create_task(box.step())
    await asyncio.sleep(0.1)
    assert box.activity and box.activity["id"] == "o4"
    beats = cloud.calls.count(("POST", "/heartbeat"))
    assert beats >= 3
    release.set()
    assert await step
    await settle(box)
    assert box.activity is None and cloud.receipts == [{"items": []}]
    await box.close()


async def test_an_action_that_never_ends_is_bounded_and_reported_unknown(monkeypatch):
    import asyncio

    monkeypatch.setattr(link_runner, "OPERATION_TIMEOUT", 0.05)

    async def execute(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    operation = {"id": "o5", "operation": "chat.list", "args": {}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    box = mailbox(make_link(sessions=True), cloud)
    assert await box.step()
    await settle(box)
    assert cloud.receipts[0]["outcome_unknown"] and "timed_out" in cloud.receipts[0]["error"]
    cloud.operation = operation  # never replayed
    assert not await box.step()
    await box.close()


async def test_an_unexpected_error_in_an_action_still_closes_its_receipt(monkeypatch):
    def broken(account):
        # Not one of the errors perform() expects: before, this killed the loop silently.
        raise LookupError("database is locked")

    monkeypatch.setattr(link_runner, "_bridge_headers", broken)
    operation = {"id": "o6", "operation": "chat.list", "args": {}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    box = mailbox(make_link(sessions=True), cloud)
    assert await box.step()
    await settle(box)
    assert cloud.receipts[0]["outcome_unknown"]
    assert box.journal.pending() == []
    await box.close()


async def test_runner_survives_an_unexpected_error_and_recovers(monkeypatch):
    """A defect in one pass is logged and the loop starts over; it never dies silently."""
    import asyncio

    make_link(sessions=True)
    passes = []

    async def step(self):
        passes.append(1)
        if len(passes) == 1:
            raise LookupError("unexpected")
        return False

    async def beat(self):
        pass

    monkeypatch.setattr(link_runner.Mailbox, "step", step)
    monkeypatch.setattr(link_runner.Mailbox, "_beat", beat)
    monkeypatch.setattr(link_runner, "OFFLINE_RETRY", 0.01)
    monkeypatch.setattr(link_runner, "IDLE_POLL", 0.01)
    monkeypatch.setattr(link_runner, "ACTIVE_POLL", 0.01)
    runner = link_runner.Runner()
    runner.start()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(passes) >= 3:
            break
    assert len(passes) >= 3
    assert runner.state == "connected"
    await runner.stop()
    assert runner.mailbox is None


async def test_runner_restarts_a_loop_that_ended(monkeypatch):
    import asyncio

    monkeypatch.setattr(link_runner, "OFFLINE_RETRY", 0.01)
    runs = []

    async def loop(self):
        runs.append(1)
        if len(runs) == 1:
            raise SystemError("escaped")
        await asyncio.Event().wait()

    monkeypatch.setattr(link_runner.Runner, "_loop", loop)
    runner = link_runner.Runner()
    runner.start()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(runs) >= 2:
            break
    assert len(runs) == 2
    await runner.stop()


def test_snapshot_reports_the_worse_view():
    runner = link_runner.Runner()
    runner.state = "connected"
    runner.mailbox = link_runner.Mailbox(ACCOUNT, make_link())
    runner.mailbox.contact = "offline"
    assert runner.snapshot()["state"] == "offline"
    runner.mailbox.contact = "unavailable"
    assert runner.snapshot()["state"] == "unavailable"
    runner.mailbox.journal.close()


async def test_a_slow_session_operation_does_not_hold_back_screen_commands(monkeypatch):
    import asyncio

    release, ran = asyncio.Event(), []

    async def execute(*args):
        await release.wait()
        return {"items": []}

    async def driver(action, timeout=None):
        ran.append(action)
        return {"status": "input_executed"}

    monkeypatch.setattr(link_runner.local_api, "execute", execute)
    monkeypatch.setattr(link_runner.dispatch, "run_driver", driver)
    operation = {"id": "o8", "operation": "chat.list", "args": {}, "deadline": time.time() + 30}
    cloud = Cloud(operation=operation)
    box = mailbox(make_link(control=True, sessions=True), cloud)
    assert await box.step()
    cloud.command = {"id": "c8", "action": {"type": "click", "x": 1, "y": 1}, "deadline": time.time() + 30}
    assert await box.step()  # the session lane is busy; the command lane still claims
    await asyncio.wait_for(box.lanes["command"], 1)
    assert ran and not box.idle("session")
    assert ("/claim", {"kinds": ["command"]}) in cloud.bodies
    release.set()
    await settle(box)
    await box.close()


async def test_control_withdrawn_after_claim_never_reaches_the_screen(monkeypatch):
    async def driver(action, timeout=None):
        raise AssertionError("must not run")

    monkeypatch.setattr(link_runner.dispatch, "run_driver", driver)
    link = make_link(control=True)
    command = {"id": "c9", "action": {"type": "click", "x": 1, "y": 1}, "deadline": time.time() + 30}
    cloud = Cloud(command=command)
    original = cloud.__call__

    def withdraw(request):
        if request.url.path.endswith("/claim"):
            store.save(ACCOUNT, store.Link(**{**link.__dict__, "control_enabled": False}))
        return original(request)

    box = mailbox(link, withdraw)
    assert await box.step()
    await settle(box)
    assert cloud.receipts[0]["not_executed"]
    await box.close()


async def test_work_past_its_deadline_is_answered_not_executed():
    command = {"id": "c10", "action": {"type": "click", "x": 1, "y": 1}, "deadline": time.time() - 1}
    cloud = Cloud(command=command)
    box = mailbox(make_link(control=True), cloud)
    assert await box.step()
    await settle(box)
    assert cloud.receipts == [{"error": "expired before it ran", "not_executed": True}]
    await box.close()


async def test_a_receipt_refused_for_good_does_not_block_the_others():
    calls = []

    def proxy(request):
        calls.append(request.url.path)
        if "/commands/" in request.url.path:
            return httpx.Response(413)
        return Cloud()(request)

    box = mailbox(make_link(), proxy)
    box.journal.finish("command", "big", {"image": "..."})
    box.journal.finish("session", "small", {"items": []})
    await box.flush()
    assert box.journal.pending() == []
    await box.close()


async def test_a_stalled_mailbox_stops_reporting_itself_alive():
    cloud = Cloud()
    box = mailbox(make_link(), cloud)
    box.passed = time.time() - link_runner.STALLED_AFTER - 1
    await box.heartbeat()
    assert cloud.calls == [] and box.contact == "offline"
    await box.close()


async def test_an_older_pairing_without_sessions_still_serves_commands():
    def legacy(request):
        if request.url.path.endswith("/sessions/consent"):
            return httpx.Response(409, json={"detail": "Session capability not paired"})
        return Cloud(protocol=1)(request)

    box = mailbox(make_link(control=True), legacy)
    await box.step()
    await box.step()
    assert box.protocol == 1 and box.contact == "connected"
    await box.close()


def test_a_large_busy_screen_fits_the_receipt_and_actions_map_back(monkeypatch):
    import os as _os
    from PIL import Image
    from personal_agent.device_link import driver

    noise = Image.frombytes("RGB", (3840, 2160), _os.urandom(3840 * 2160 * 3))
    image, pixels, scale = driver.encode(noise)
    assert len(pixels) <= driver.IMAGE_BUDGET and scale > 1
    assert abs(image.width * scale - 3840) < scale + 1

    sent = []
    monkeypatch.setattr(driver.platform, "system", lambda: "Linux")
    monkeypatch.setattr(driver, "observe", lambda image=None: {"width": 100, "height": 100, "scale": 2.0,
                                                               "observation_id": "o", "signature": "0"})
    monkeypatch.setattr(driver, "native_action", sent.append)
    monkeypatch.setattr(driver, "settle", lambda: None)
    driver.execute({"type": "click", "x": 10, "y": 20, "observation_id": "o"})
    assert sent == [{"type": "click", "x": 20, "y": 40, "observation_id": "o"}]


def test_status_says_signed_out_instead_of_failing(api, monkeypatch):
    from features.desktop.api import device_link

    client, _ = api
    monkeypatch.setattr(device_link, "current_account", lambda: None)
    status = client.get("/v1/device-link")
    assert status.status_code == 200 and status.json()["data"]["state"] == "signed_out"
    assert client.put("/v1/device-link", json={"device_id": "d", "token": "t" * 32, "name": "L"}).status_code == 403


def test_status_reports_the_runner_state(api):
    client, _ = api
    data = client.get("/v1/device-link").json()["data"]
    assert data["state"] == "unlinked" and data["activity"] is None


# -- read / steps: where they run, how long they may take, the pixel batch --------------

class FakeHost:
    """The accessibility host as dispatch sees it."""

    def __init__(self, usable, reason=None):
        self.usable, self.reason, self.calls = usable, reason, []

    async def available(self):
        return self.usable

    async def read(self, action):
        self.calls.append(("read", action))
        return {"mode": "elements", "snapshot_id": "s0000002a"}

    async def steps(self, action):
        self.calls.append(("steps", action))
        return {"ok": True, "executed": 1, "failed_step": None, "steps": []}


@pytest.fixture
def routed(monkeypatch):
    from personal_agent.device_link import dispatch

    driven = []

    async def driver(action, timeout=dispatch.DRIVER_TIMEOUT):
        driven.append((action, timeout))
        return {"mode": "pixels", "snapshot_id": "o1"} if action["type"] == "read" else {"status": "input_executed"}

    monkeypatch.setattr(dispatch, "run_driver", driver)

    def use(host):
        monkeypatch.setattr(dispatch.ui_host, "host", host)
        return dispatch, driven

    return use


async def test_read_and_steps_use_the_accessibility_host_when_it_is_usable(routed):
    host = FakeHost(usable=True)
    dispatch, driven = routed(host)
    assert (await dispatch.execute({"type": "read"}))["mode"] == "elements"
    await dispatch.execute({"type": "steps", "window": "1:2", "steps": [{"type": "click", "element": "s0000002a:3"}]})
    # A single action of an older cloud carries screen pixels: always the screenshot driver.
    await dispatch.execute({"type": "click", "x": 1, "y": 1})
    assert [kind for kind, _ in host.calls] == ["read", "steps"]
    assert [action["type"] for action, _ in driven] == ["click"]


async def test_a_mac_without_the_host_reads_pixels_and_says_why(routed):
    dispatch, driven = routed(FakeHost(usable=False, reason="accessibility_permission_missing"))
    result = await dispatch.execute({"type": "read"})
    assert result["mode"] == "pixels" and result["degraded_reason"] == "accessibility_permission_missing"
    await dispatch.execute({"type": "steps", "steps": [{"type": "click", "x": 1, "y": 2}]})
    assert [action["type"] for action, _ in driven] == ["read", "steps"]
    # The child process gets a limit inside the lane's: 10 s for a read, 70 s for a batch.
    assert [timeout for _, timeout in driven] == [10, 70]


async def test_other_systems_read_pixels_without_a_degraded_reason(routed):
    dispatch, _ = routed(FakeHost(usable=False, reason="not_macos"))
    assert (await dispatch.execute({"type": "read"})).get("degraded_reason") is None


async def test_a_failing_host_is_reported_not_retried(routed):
    host = FakeHost(usable=True)

    async def broken(action):
        raise RuntimeError("driver crashed")

    host.steps = host.read = broken
    dispatch, driven = routed(host)
    assert "read_failed" in (await dispatch.execute({"type": "read"}))["error"]
    assert (await dispatch.execute({"type": "steps", "window": "1:2", "steps": []}))["outcome_unknown"]
    assert not driven  # Never re-run on the screenshot driver: some input may have landed.


def test_time_limits_follow_the_command_type():
    from personal_agent.device_link import dispatch

    assert dispatch.time_limit({"type": "read"}) == 15
    assert dispatch.time_limit({"type": "steps"}) == 75
    assert dispatch.time_limit({"type": "click"}) == dispatch.COMMAND_TIMEOUT


async def test_a_steps_command_runs_once_with_its_own_time_limit(monkeypatch, routed):
    host = FakeHost(usable=True)
    dispatch, _ = routed(host)
    limits = []
    launch = link_runner.Mailbox._launch

    def recording(self, kind, ident, action, seconds):
        limits.append(seconds)
        return launch(self, kind, ident, action, seconds)

    monkeypatch.setattr(link_runner.Mailbox, "_launch", recording)
    command = {"id": "c9", "action": {"type": "steps", "window": "1:2", "steps": [{"type": "type", "text": "hi"}]},
               "deadline": time.time() + 60}
    cloud = Cloud(command=command)
    box = mailbox(make_link(control=True), cloud)
    assert await box.step()
    await settle(box)
    cloud.command = command  # Redelivered: the journal refuses it.
    assert not await box.step()
    await box.close()
    assert len(host.calls) == 1 and limits == [75]
    assert cloud.receipts == [{"ok": True, "executed": 1, "failed_step": None, "steps": []}]


async def test_heartbeat_reports_element_support_only_while_the_host_is_usable(routed):
    routed(FakeHost(usable=True))
    cloud = Cloud()
    box = mailbox(make_link(control=True), cloud)
    await box.heartbeat()
    await box.close()
    assert "ui.elements" in cloud.bodies[0][1]["capabilities"]


@pytest.fixture
def screen(monkeypatch):
    """The pixel driver on a fake 200x100 Linux screen that records the input it sends."""
    from personal_agent.device_link import driver

    sent, shots = [], []
    observation = {"width": 200, "height": 100, "scale": 1.0, "observation_id": "o1", "signature": "0" * 640,
                   "image_base64": "", "platform": "Linux"}

    def observe(image=None):
        shots.append(image)
        return dict(observation)

    monkeypatch.setattr(driver.platform, "system", lambda: "Linux")
    monkeypatch.setattr(driver, "observe", observe)
    monkeypatch.setattr(driver, "settle", lambda: "settled")
    monkeypatch.setattr(driver, "native_action", sent.append)
    return driver, sent, observation


def test_pixel_steps_run_in_order_and_return_the_screen_after(screen):
    driver, sent, _ = screen
    result = driver.run({"type": "steps", "snapshot_id": "o1", "steps": [
        {"type": "click", "x": 10, "y": 20}, {"type": "type", "text": "hello"},
        {"type": "scroll", "amount": 3, "direction": "down"}, {"type": "hotkey", "keys": ["ctrl", "s"]}]})
    assert result["ok"] and result["executed"] == 4 and result["failed_step"] is None
    assert [step["effect"] for step in result["steps"]] == ["unverifiable"] * 4
    assert [action["type"] for action in sent] == ["click", "type", "scroll", "hotkey"]
    assert sent[2]["amount"] == -3  # A direction sets the sign, as for older computers.
    after = result["after"]
    assert after["mode"] == "pixels" and after["skill"] == "pa-computer-pixels" and after["window"] == "screen"
    assert after["snapshot_id"] == "o1" and after["platform"] == "linux"


def test_pixel_steps_stop_at_the_first_failure(screen):
    driver, sent, _ = screen
    result = driver.run({"type": "steps", "snapshot_id": "o1", "steps": [
        {"type": "click", "x": 10, "y": 20}, {"type": "click", "x": 500, "y": 20}, {"type": "type", "text": "x"}]})
    assert not result["ok"] and result["executed"] == 1 and result["failed_step"] == 1
    assert result["steps"][1]["effect"] == "refused" and len(result["steps"]) == 2
    assert len(sent) == 1 and "after" in result


def test_pixel_steps_on_a_changed_screen_do_not_run(screen):
    driver, sent, _ = screen
    result = driver.run({"type": "steps", "snapshot_id": "old", "expected_signature": "f" * 640,
                         "steps": [{"type": "click", "x": 1, "y": 1}]})
    assert result["not_executed"] and result["failed_step"] == 0 and not sent
    assert result["after"]["snapshot_id"] == "o1"  # The fresh screen, to plan again on.


@pytest.mark.parametrize("steps, error", [
    ([{"type": "click", "element": "s0000002a:3"}], "needs_elements_mode"),
    ([{"type": "set_value", "text": "x"}], "needs_elements_mode"),
    ([{"type": "menu", "path": ["File"]}], "needs_elements_mode"),
    ([{"type": "click", "x": 1}], "invalid_steps"),
    ([{"type": "click", "x": 1, "y": 1, "button": "middle"}], "invalid_steps"),
    ([{"type": "run_script", "text": "rm -rf ~"}], "invalid_steps"),
    ([{"type": "open_browser", "url": "file:///etc/passwd"}], "invalid_steps"),
    ([{"type": "key", "key": "f13"}], "invalid_steps"),
    ([{"type": "scroll", "amount": 500}], "invalid_steps"),
    ([{"type": "type", "text": "x" * 10001}], "invalid_steps"),
    ([{"type": "click", "x": True, "y": 1}], "invalid_steps"),
    ([], "invalid_steps"),
    ([{"type": "key", "key": "enter"}] * 17, "invalid_steps"),
])
def test_invalid_pixel_batches_are_refused_before_any_input(screen, steps, error):
    driver, sent, _ = screen
    result = driver.run({"type": "steps", "snapshot_id": "o1", "steps": steps})
    assert result["error"].startswith(error) and result["not_executed"] and not sent


def test_the_cloud_and_the_device_accept_the_same_steps():
    """The device must run every batch the cloud validated (contract A.2)."""
    from personal_agent.computer import steps as cloud
    from personal_agent.device_link import driver

    accepted = [
        {"type": "hotkey", "keys": ["cmd", "shift", "t"]}, {"type": "hotkey", "keys": ["enter"]},
        {"type": "scroll", "amount": -100, "direction": "left", "x": 3, "y": 4},
        {"type": "open_browser", "url": "https://example.com/a?b=c"}, {"type": "menu", "path": ["File", "Save As…"]},
        {"type": "set_value", "element": "s0000002a:12", "text": ""}, {"type": "move", "x": 0, "y": 10000},
    ]
    for step in accepted:
        cloud.validate_step(dict(step))
        assert driver.step_problem(step, elements=True) is None, step


def test_a_chord_the_driver_cannot_press_fails_its_step_only(screen):
    driver, sent, _ = screen
    result = driver.run({"type": "steps", "snapshot_id": "o1", "steps": [{"type": "hotkey", "keys": ["cmd", "shift"]}]})
    assert result["failed_step"] == 0 and result["steps"][0]["effect"] == "refused" and not sent


def test_the_screen_is_read_once_it_stops_changing(monkeypatch):
    from personal_agent.device_link import driver

    frames = iter(["a", "b", "c", "c", "d"])
    monkeypatch.setattr(driver, "screenshot", lambda: next(frames))
    monkeypatch.setattr(driver, "signature", lambda image: image)
    monkeypatch.setattr(driver.time, "sleep", lambda seconds: None)
    assert driver.settle() == "c"

    clock = iter(range(100))
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(clock) * 0.2)
    monkeypatch.setattr(driver, "screenshot", lambda: str(next(clock)))  # Never stable.
    driver.settle()  # Returns at the 500 ms limit instead of waiting forever.


def test_a_pixel_read_has_the_read_shape(screen):
    driver, _, _ = screen
    result = driver.run({"type": "read", "window": None, "screenshot": True})
    assert {"mode", "platform", "skill", "window", "snapshot_id", "elements", "windows", "media_type"} <= set(result)
    assert result["mode"] == "pixels" and result["media_type"] == "image/png"
