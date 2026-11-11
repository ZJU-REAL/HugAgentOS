"""pa_computer_read / pa_computer_act: protocol, approvals, replay and the cloud flow against a mock device."""
import asyncio
import base64
import io
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from core.infra.ephemeral import LocalEphemeralState
from personal_agent.computer import computer, devices, receipts
from personal_agent.computer import steps as step_rules
from personal_agent.context import AgentContext
from personal_agent.models import PersonalDevice, PersonalDeviceCommand, PersonalWorkItem
from personal_agent.tasks import approvals
from personal_agent.tasks.execution import current_execution
from tests.test_personal_agent import personal_agent_on, sessions  # noqa: F401

NEW = sorted({"observe", "click", "double_click", "right_click", "move", "type", "key", "scroll",
              "open_browser", "read", "steps", "ui.elements", "hugagent.sessions"})


def image(fmt="PNG", color="blue"):
    data = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(data, format=fmt)
    return base64.b64encode(data.getvalue()).decode()


@pytest.fixture
def world(sessions, monkeypatch):
    from personal_agent.delivery import delivery
    from personal_agent.computer import computer_wait
    for target in ("personal_agent.tasks.guards", "personal_agent.computer.computer_wait",
                   "personal_agent.tasks.approvals", "personal_agent.computer.devices"):
        monkeypatch.setattr(target + ".SessionLocal", sessions)
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    cache = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: cache)
    monkeypatch.setattr(computer_wait, "get_ephemeral_state", lambda: cache)
    monkeypatch.setattr(devices, "WAIT_SLICE", 0.5)
    current_execution.set(None)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Write a note in TextEdit", "note")["id"]
        row = db.get(PersonalWorkItem, task)
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 300
        db.commit()
    kit = []
    computer.register_computer_tools(kit, task, "owner")
    yield SimpleNamespace(task=task, cache=cache, tools={tool.name: tool._func for tool in kit})
    current_execution.set(None)


def text(chunk):
    return json.loads(chunk.content[0].text)


def checkpoint(sessions, task):
    with sessions() as db:
        return db.get(PersonalWorkItem, task).checkpoint or {}


# -- steps and translation ------------------------------------------------------------------

@pytest.mark.parametrize("step", [
    {"type": "tap"}, {"type": "click"}, {"type": "click", "x": 1}, {"type": "click", "x": -1, "y": 2},
    {"type": "click", "element": "button"}, {"type": "type"}, {"type": "key", "key": "f13"},
    {"type": "hotkey", "keys": []}, {"type": "hotkey", "keys": ["cmd", "space bar"]}, {"type": "scroll", "amount": 101},
    {"type": "scroll", "direction": "sideways"}, {"type": "open_browser", "url": "file:///etc/passwd"},
    {"type": "menu"}, {"type": "click", "x": 1, "y": 1, "script": "rm"}, {"type": "type", "text": "x" * 10001},
])
def test_invalid_steps_are_refused(step):
    with pytest.raises(ValueError):
        step_rules.validate_steps([step], "elements")


def test_batch_size_and_pixels_rules():
    with pytest.raises(ValueError): step_rules.validate_steps([], "pixels")
    with pytest.raises(ValueError): step_rules.validate_steps([{"type": "key", "key": "enter"}] * 17, "pixels")
    for step in ({"type": "click", "element": "s0000002a:3"}, {"type": "set_value", "element": "s0000002a:3", "text": "a"},
                 {"type": "menu", "path": ["File", "Save"]}):
        with pytest.raises(step_rules.NeedsElements): step_rules.validate_steps([step], "pixels")
        step_rules.validate_steps([step], "elements")
    step_rules.validate_steps([{"type": "hotkey", "keys": ["cmd", "s"]}, {"type": "click", "x": 3, "y": 4}], "pixels")
    # A pixels batch runs blind after its first step: at most five; an elements batch may hold 16.
    with pytest.raises(ValueError, match="at most 5"): step_rules.validate_steps([{"type": "key", "key": "enter"}] * 6, "pixels")
    step_rules.validate_steps([{"type": "key", "key": "enter"}] * 16, "elements")


async def test_old_computer_runs_a_batch_as_single_actions_and_stops_at_failure():
    sent = []
    answers = [{"status": "input_executed", "observation_id": "o2", "signature": "g2", "image_base64": "A"},
               {"status": "stale_observation", "message": "Screen changed; observe again"}]

    async def send(action):
        sent.append(action)
        return answers.pop(0)
    command = {"type": "steps", "snapshot_id": "o1", "expected_signature": "g1",
               "steps": [{"type": "click", "x": 1, "y": 2}, {"type": "hotkey", "keys": ["ctrl", "a"]},
                         {"type": "type", "text": "never"}]}
    result = await step_rules.run_legacy_steps(send, command)
    assert sent == [{"type": "click", "x": 1, "y": 2, "observation_id": "o1", "expected_signature": "g1"},
                    {"type": "key", "key": "ctrl+a", "observation_id": "o2", "expected_signature": "g2"}]
    assert result["ok"] is False and result["executed"] == 1 and result["failed_step"] == 1
    assert [step["effect"] for step in result["steps"]] == ["unverifiable", "refused"]
    assert result["after"]["snapshot_id"] == "o2" and result["after"]["mode"] == "pixels"


async def test_old_computer_cannot_do_a_new_step_and_says_so():
    async def send(action):
        assert action == {"type": "observe"}
        return {"observation_id": "o1", "signature": "g", "platform": "Windows"}
    result = await step_rules.run_legacy_steps(send, {"type": "steps", "snapshot_id": "o1",
                                                      "steps": [{"type": "scroll", "direction": "left"}]})
    assert result["executed"] == 0 and result["steps"][0]["effect"] == "refused"
    assert result["after"]["platform"] == "windows" and result["after"]["skill"] == "pa-computer-pixels"


async def test_device_without_batch_capabilities_gets_old_commands(monkeypatch):
    queued = []

    async def one(task_id, user_id, ident, action):
        queued.append(action)
        if action["type"] == "observe": return {"observation_id": "o1", "signature": "g1", "platform": "Linux"}
        return {"status": "input_executed", "observation_id": "o2", "signature": "g2"}
    monkeypatch.setattr(devices, "_execute_one", one)
    monkeypatch.setattr(devices, "run_in_session", lambda fn, *args: asyncio.sleep(0, ["observe", "click"]))
    read = await devices.execute_command("t", "owner", "d", {"type": "read", "window": None})
    assert queued == [{"type": "observe"}] and read["mode"] == "pixels" and read["snapshot_id"] == "o1"
    queued.clear()
    done = await devices.execute_command("t", "owner", "d", {"type": "steps", "snapshot_id": "o1",
                                                             "steps": [{"type": "click", "x": 1, "y": 1}]})
    assert queued == [{"type": "click", "x": 1, "y": 1, "observation_id": "o1"}] and done["ok"]
    queued.clear()
    monkeypatch.setattr(devices, "run_in_session", lambda fn, *args: asyncio.sleep(0, NEW))
    await devices.execute_command("t", "owner", "d", {"type": "read"})
    assert queued == [{"type": "read"}]


# -- capabilities, deadlines and receipts ----------------------------------------------------------

def paired(db, capabilities=("observe",), control=True):
    device = devices.pair(db, "owner", "Mac", "macos", list(capabilities))
    devices.heartbeat(db, device["id"], device["token"], control, False)
    return device


def test_heartbeat_reports_and_checks_capabilities(sessions):
    with sessions() as db:
        device = paired(db)
        devices.heartbeat(db, device["id"], device["token"], True, False, NEW)
        assert db.get(PersonalDevice, device["id"]).capabilities == NEW
        # An older desktop says nothing about capabilities: the set stays as it is.
        devices.heartbeat(db, device["id"], device["token"], True, False)
        assert db.get(PersonalDevice, device["id"]).capabilities == NEW
        for bad in (["observe", "run_script"], ["x"] * 33, "read"):
            with pytest.raises(ValueError): devices.heartbeat(db, device["id"], device["token"], True, False, bad)
        assert db.get(PersonalDevice, device["id"]).capabilities == NEW
        devices.heartbeat(db, device["id"], device["token"], True, False, [c for c in NEW if c != "ui.elements"])
        assert "ui.elements" not in db.get(PersonalDevice, device["id"]).capabilities


def test_heartbeat_route_accepts_capabilities_and_stays_compatible(sessions, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db.engine import get_db
    from personal_agent.api.computer import router
    app = FastAPI()
    app.include_router(router)

    def db():
        with sessions() as session: yield session
    app.dependency_overrides[get_db] = db
    with sessions() as db:
        device = paired(db)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + device["token"]}
    url = "/device-link/" + device["id"] + "/heartbeat"
    assert client.post(url, json={"control_enabled": True, "sessions_enabled": False}, headers=headers).status_code == 200
    assert client.post(url, json={"control_enabled": True, "sessions_enabled": False, "capabilities": NEW}, headers=headers).status_code == 200
    with sessions() as db:
        assert db.get(PersonalDevice, device["id"]).capabilities == NEW
    assert client.post(url, json={"control_enabled": True, "sessions_enabled": False, "capabilities": ["evil"]}, headers=headers).status_code == 409


@pytest.mark.parametrize("action,seconds", [({"type": "read"}, 15), ({"type": "steps", "steps": []}, 75), ({"type": "click"}, 30)])
def test_command_deadline_follows_its_type(sessions, world, action, seconds):
    with sessions() as db:
        device = paired(db, NEW)
        start = time.time()
        command_id, _, deadline = devices._queue_command(db, world.task, "owner", device["id"], action)
        assert start + seconds - 1 <= deadline <= time.time() + seconds
        assert db.get(PersonalDeviceCommand, command_id).deadline == deadline


async def test_wait_gives_up_at_the_deadline_and_withdraws_an_untaken_command(sessions, world, monkeypatch):
    with sessions() as db:
        device = paired(db, NEW)
    monkeypatch.setattr(devices, "COMMAND_SECONDS", {"read": 1})
    monkeypatch.setattr(devices, "RECEIPT_GRACE", 0)
    began = time.monotonic()
    result = await devices.execute_command(world.task, "owner", device["id"], {"type": "read"})
    # The wait ends at the command's own (wall-clock) deadline, not after the old fixed 30 s.
    assert result["not_executed"] and time.monotonic() - began < 3
    with sessions() as db:
        row = db.query(PersonalDeviceCommand).one()
        assert row.status == "revoked" and time.time() >= row.deadline


async def test_images_go_to_the_short_lived_store_not_the_row(world):
    receipt = {"ok": True, "steps": [], "after": {"snapshot_id": "s1", "image_base64": "AAAA", "media_type": "image/jpeg"},
               "image_base64": "BBBB"}
    stored = await receipts.offload_images("cmd", receipt)
    assert "image_base64" not in json.dumps(stored) and stored["after"]["image_ref"].startswith("pa:command-image:cmd:")
    assert receipt["after"]["image_base64"] == "AAAA"  # The caller's receipt is not changed.
    restored = await receipts.restore_images(stored)
    assert restored["after"]["image_base64"] == "AAAA" and restored["image_base64"] == "BBBB"
    assert await world.cache.keys("pa:command-image:") == []
    assert (await receipts.restore_images(stored))["after"]["image_expired"] is True


async def test_local_wake_reaches_a_waiter_on_another_thread(world):
    waiter = asyncio.create_task(receipts.wait("cmd-1", 5))
    await asyncio.sleep(0.05)
    began = time.monotonic()
    thread = threading.Thread(target=lambda: asyncio.run(receipts.notify("cmd-1")))
    thread.start(); thread.join()
    assert await waiter and time.monotonic() - began < 1
    # A wake that comes before the wait is not lost, and a wait without one times out.
    await receipts.notify("cmd-2")
    assert await receipts.wait("cmd-2", 1)
    assert not await receipts.wait("cmd-3", 0.1)


async def test_redis_wake_survives_a_push_before_the_wait(monkeypatch):
    import fakeredis
    from core.infra.ephemeral import RedisEphemeralState
    server = fakeredis.FakeServer()
    client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(receipts, "get_redis", lambda blocking=False: client)
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: RedisEphemeralState())
    await receipts.notify("cmd")
    assert await client.ttl("pa:command-done:cmd") > 0
    assert await receipts.wait("cmd", 1)
    assert not await receipts.wait("cmd", 0.2)
    waiter = asyncio.create_task(receipts.wait("later", 3))
    await asyncio.sleep(0.05)
    await receipts.notify("later")
    assert await asyncio.wait_for(waiter, 2)


# -- approvals --------------------------------------------------------------------------------

def steps_identity(app="TextEdit", snapshot="s0000002a", x=1, target="device:mac"):
    return {"kind": "computer.steps", "target": target, "generation": 1, "app": app, "window": "1:2",
            "snapshot_id": snapshot, "steps": [{"type": "click", "x": x, "y": 2}, {"type": "type", "text": "hi"}]}


def test_scope_is_one_app_on_one_computer_and_rejection_ignores_placement():
    assert approvals.scope_key(steps_identity()) == "computer:device:mac:TextEdit"
    assert approvals.scope_key(steps_identity(app=None)) == "computer:device:mac:*"
    assert approvals.scope_key({"kind": "computer.action", "target": "cloud", "action": {}}) == "computer:cloud:*"
    assert approvals.intent(steps_identity()) == approvals.intent(steps_identity(snapshot="s00000099", x=500))
    assert approvals.intent(steps_identity()) != approvals.intent(steps_identity(app="Mail"))
    assert "snapshot_id" not in approvals.intent(steps_identity()) and "generation" not in approvals.intent(steps_identity())
    old = {"kind": "computer.action", "target": "cloud", "action": {"type": "click", "x": 1, "y": 2, "observation_id": "o"}}
    assert approvals.intent(old)["action"] == {"type": "click"}


def test_allow_for_task_covers_the_app_not_the_computer(sessions, world):
    first = steps_identity()
    digest, pending = approvals.authorize(world.task, "owner", first)
    assert pending["pending_approval"]
    with sessions() as db:
        approvals.approve(AgentContext(db, "owner"), world.task, digest, True, scope="task")
    assert approvals.authorize(world.task, "owner", steps_identity(snapshot="s00000002", x=9))[1] is None
    assert approvals.authorize(world.task, "owner", steps_identity(app="Mail"))[1]["pending_approval"]


# -- the tools ------------------------------------------------------------------------------

async def test_pixels_act_adds_the_signature_and_refuses_element_steps(sessions, world, monkeypatch):
    sent = []

    async def driver(state, uid, action):
        sent.append(action)
        if action["type"] == "read":
            return {"observation_id": "o1", "signature": "secret", "width": 8, "height": 8, "image_base64": image(), "platform": "Linux"}
        return {"ok": True, "executed": 1, "failed_step": None, "steps": [{"index": 0, "ok": True, "effect": "unverifiable"}],
                "after": {"mode": "pixels", "snapshot_id": "o2", "signature": "next", "image_base64": image(color="red")}}
    monkeypatch.setattr(computer, "cloud_driver", driver)
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"approval_mode": "full"})
    read = await world.tools["pa_computer_read"]()
    body = text(read)
    assert body["mode"] == "pixels" and "signature" not in body and read.content[1].source.media_type == "image/png"
    refused = text(await world.tools["pa_computer_act"]("cloud", [{"type": "click", "element": "s0000002a:3"}], body["snapshot_id"]))
    assert refused["error"] == "needs_elements_mode" and len(sent) == 1
    unknown = text(await world.tools["pa_computer_act"]("cloud", [{"type": "key", "key": "enter"}], "forged"))
    assert unknown["error"] == "unknown_snapshot" and len(sent) == 1
    acted = await world.tools["pa_computer_act"]("cloud", [{"type": "click", "x": 3, "y": 4}], body["snapshot_id"])
    assert sent[-1] == {"type": "steps", "window": "screen", "snapshot_id": "o1", "expected_signature": "secret",
                        "steps": [{"type": "click", "x": 3, "y": 4}]}
    result = text(acted)
    assert result["after"]["resource_ref"] and "signature" not in result["after"] and acted.content[1].source.media_type == "image/png"
    assert (await computer.snapshot(world.task, "o2"))["signature"] == "next"
    receipt = next(iter(checkpoint(sessions, world.task)["action_receipts"].values()))
    assert "image_base64" not in json.dumps(receipt)


async def test_read_takes_no_desktop_lease(sessions, world, monkeypatch):
    with sessions() as db:
        agent_id = AgentContext(db, "owner").identity.agent().id
    await world.cache.hold(computer.desktop_lease(agent_id), "human", ttl=45)

    async def driver(state, uid, action):
        return {"mode": "pixels", "snapshot_id": "o1", "platform": "linux"}
    monkeypatch.setattr(computer, "cloud_driver", driver)
    assert text(await world.tools["pa_computer_read"]())["snapshot_id"] == "o1"


async def test_old_approved_action_is_replayed_as_one_step_under_its_own_identity(sessions, world, monkeypatch):
    from personal_agent.worker import executor
    sent = []

    async def driver(state, uid, action):
        sent.append(action)
        return {"ok": True, "executed": 1, "failed_step": None, "steps": [], "after": {"mode": "pixels", "snapshot_id": "o9"}}
    monkeypatch.setattr(computer, "cloud_driver", driver)
    monkeypatch.setattr("personal_agent.tools.email.register_email_tools", lambda *args: None)
    # A checkpoint written by the previous tools: an old observation, and its approved action.
    await world.cache.put("pa:observation:o1", "old-signature", ttl=600)
    with sessions() as db:
        generation = AgentContext(db, "owner").identity.agent().generation
    old = {"kind": "computer.action", "target": "cloud", "generation": generation,
           "action": {"type": "click", "x": 4, "y": 5, "observation_id": "o1"}}
    digest, pending = approvals.authorize(world.task, "owner", old)
    assert pending["pending_approval"]
    with sessions() as db:
        approvals.approve(AgentContext(db, "owner"), world.task, digest, True)
    await executor.dispatch_approved(world.task, "owner")
    assert sent == [{"type": "steps", "window": "screen", "snapshot_id": "o1", "expected_signature": "old-signature",
                     "steps": [{"type": "click", "x": 4, "y": 5}]}]
    state = checkpoint(sessions, world.task)
    assert state["action_receipts"][digest]["ok"] is True and state["approved_action_payloads"] == {}


async def test_replay_after_the_computer_was_replaced_does_not_run(sessions, world, monkeypatch):
    async def driver(*args):
        raise AssertionError("must not run")
    monkeypatch.setattr(computer, "cloud_driver", driver)
    identity = {**steps_identity(target="cloud"), "generation": 999}
    await computer.replay(world.task, "owner", identity)
    assert checkpoint(sessions, world.task)["action_receipts"][approvals.fingerprint(identity)]["status"] == "not_executed"


# -- read -> approval -> act -> after against a mock device ------------------------------------

class MockMac:
    """A desktop on the new protocol: reports its capabilities, claims commands, posts receipts."""

    def __init__(self, client, device):
        self.client, self.device = client, device
        self.headers = {"Authorization": "Bearer " + device["token"]}
        self.commands, self.stop = [], False

    def path(self, tail):
        return "/device-link/" + self.device["id"] + tail

    def answer(self, action):
        if action["type"] == "read":
            return {"mode": "elements", "platform": "macos", "skill": "pa-computer-macos", "window": "41:7",
                    "app": "TextEdit", "title": "Untitled", "windows": [{"window": "41:7", "app": "TextEdit", "title": "Untitled", "on_screen": True}],
                    "snapshot_id": "s0000002a", "elements": "- [3] AXTextArea token=s0000002a:3", "truncated": False,
                    "invalidated_snapshot_ids": [], "image_base64": image("JPEG"), "media_type": "image/jpeg", "width": 8, "height": 8}
        return {"ok": True, "executed": len(action["steps"]), "failed_step": None,
                "steps": [{"index": i, "type": s["type"], "ok": True, "effect": "confirmed"} for i, s in enumerate(action["steps"])],
                "after": {"mode": "elements", "platform": "macos", "window": "41:7", "app": "TextEdit", "snapshot_id": "s0000002b",
                          "elements": "- [3] AXTextArea value=hi token=s0000002b:3", "image_base64": image(), "media_type": "image/png"}}

    async def run(self):
        await self.client.post(self.path("/heartbeat"), headers=self.headers,
                               json={"control_enabled": True, "sessions_enabled": False, "capabilities": NEW})
        while not self.stop:
            claim = (await self.client.post(self.path("/claim"), headers=self.headers, json={"kinds": ["command"]})).json()["data"]
            command = claim["command"]
            if not command:
                await asyncio.sleep(0.02)
                continue
            self.commands.append(command)
            response = await self.client.post(self.path("/commands/" + command["id"]), headers=self.headers,
                                              json={"result": self.answer(command["action"])})
            assert response.status_code == 200, response.text


async def test_read_approve_act_after_with_a_mock_device(sessions, world, monkeypatch):
    from fastapi import FastAPI
    from core.db.engine import get_db
    from personal_agent.api.computer import router
    from personal_agent.worker import executor

    def db():
        with sessions() as session: yield session
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = db
    monkeypatch.setattr("personal_agent.tools.email.register_email_tools", lambda *args: None)
    with sessions() as session:
        device = paired(session)
    target = "device:" + device["id"]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cloud")
    mac = MockMac(client, device)
    runner = asyncio.create_task(mac.run())
    try:
        for _ in range(100):
            with sessions() as session:
                if "read" in session.get(PersonalDevice, device["id"]).capabilities: break
            await asyncio.sleep(0.02)
        read = await world.tools["pa_computer_read"](target=target)
        body = text(read)
        assert body["mode"] == "elements" and body["app"] == "TextEdit" and body["resource_ref"]
        assert read.content[1].source.media_type == "image/jpeg"
        assert mac.commands[0]["action"] == {"type": "read", "window": None, "query": None, "since": None, "screenshot": None}

        steps = [{"type": "set_value", "element": "s0000002a:3", "text": "hi"}]
        waiting = text(await world.tools["pa_computer_act"](target, steps, body["snapshot_id"]))
        assert waiting["pending_approval"] and len(mac.commands) == 1
        pending = checkpoint(sessions, world.task)["pending_approval"]
        assert pending["action"]["kind"] == "computer.steps" and pending["action"]["app"] == "TextEdit"
        assert pending["action"]["window"] == "41:7"
        # The card names the element as the read did, not by its row number.
        assert pending["action"]["targets"] == {"s0000002a:3": "文本区"}
        with sessions() as session:
            approvals.approve(AgentContext(session, "owner"), world.task, pending["id"], True, scope="task")
        began = time.monotonic()
        await executor.dispatch_approved(world.task, "owner")
        # Woken by the receipt route, not by polling every 0.25 s or waiting out a slice.
        assert time.monotonic() - began < 0.4
        action = mac.commands[1]["action"]
        assert action == {"type": "steps", "window": "41:7", "snapshot_id": "s0000002a", "steps": steps}
        with sessions() as session:
            rows = session.query(PersonalDeviceCommand).all()
            assert rows and all("image_base64" not in json.dumps(row.result) for row in rows)
            assert any("image_ref" in json.dumps(row.result) for row in rows)
        receipt = checkpoint(sessions, world.task)["action_receipts"][pending["id"]]
        assert receipt["ok"] and receipt["after"]["snapshot_id"] == "s0000002b" and "image_base64" not in json.dumps(receipt)

        # The app is allowed for this task now: the next batch runs at once and answers with the screen after it.
        more = [{"type": "hotkey", "keys": ["cmd", "s"]}]
        acted = await world.tools["pa_computer_act"](target, more, "s0000002b")
        result = text(acted)
        assert result["ok"] and result["after"]["snapshot_id"] == "s0000002b" and result["after"]["resource_ref"]
        assert acted.content[1].source.media_type == "image/png"
        assert mac.commands[2]["action"]["steps"] == more
    finally:
        mac.stop = True
        await runner
        await client.aclose()


async def test_jpeg_screenshot_is_staged_and_resolved_with_its_type(sessions, world):
    from personal_agent.delivery.delivery_resources import prepare, resolve, stage
    scope = await asyncio.to_thread(prepare, world.task, "owner", "cloud")
    ref = await stage(world.task, "owner", scope, image("JPEG"))
    content, media_type = await resolve(world.task, "owner", scope, ref)
    assert media_type == "image/jpeg" and content[:2] == b"\xff\xd8"
    with pytest.raises(ValueError):
        await stage(world.task, "owner", scope, image("GIF"))
