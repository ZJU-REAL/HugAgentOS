"""Performance invariants at the real Chromium command boundary."""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugin_bundles/marketplace/browser-automation/runtime"))
from browser_worker.commands import execute
from browser_worker.protocol import Command
from browser_worker.session import BrowserSession


async def test_pointer_input_does_not_republish_unchanged_browser_state():
    session = BrowserSession({"chromium_sandbox": False, "allowed_hosts": ["127.0.0.1"]})
    await session.start()
    try:
        await execute(session, Command(id=uuid.uuid4().hex, actor="user", connection_id="viewer",
                                      action="take_control", params={"private": True}))
        packets = []
        session.broadcast = lambda data, frame=False: packets.append(data)
        for x in (10, 20, 30):
            await execute(session, Command(id=uuid.uuid4().hex, actor="user", connection_id="viewer",
                action="input", params={"kind": "move", "x": x, "y": 10,
                                        "viewport_revision": session.viewport_revision}))
        assert packets == [], "Unchanged pointer state must not rebuild the viewer UI"
        await execute(session, Command(id=uuid.uuid4().hex, actor="user", connection_id="viewer",
                                      action="release_control", params={}))
        assert len(packets) == 1, "Control changes must still publish promptly"
    finally:
        await session.close()


async def test_worker_ready_marker_follows_chromium_initialization():
    import json
    import os
    import time
    import httpx

    runtime = Path(__file__).parents[1] / "plugin_bundles/marketplace/browser-automation/runtime"
    script = """
import asyncio
from browser_worker.session import BrowserSession
from browser_worker.server import main
original = BrowserSession.start
async def slow_start(self):
    await asyncio.sleep(1)
    await original(self)
BrowserSession.start = slow_start
main({"token": "fixture-only", "chromium_sandbox": False})
"""
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", script,
        env={**os.environ, "PYTHONPATH": str(runtime)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
        assert ready["runtime_ready"]
        assert time.monotonic() - started >= 1
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{ready['port']}/state",
                                        headers={"X-Hugagent-Resource-Token": "fixture-only"}, timeout=2)
        assert response.status_code == 200
        assert len(response.json()["tabs"]) == 1
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
