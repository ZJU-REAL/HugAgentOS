"""Actual OS worker observation, with an isolated test-only runner surface."""

import asyncio
import importlib
import sys
from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_observer_reads_actual_owner_process_without_consuming_output(monkeypatch):
    root = Path(".scratch/workbench-19/actual-observer").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WB19_METRICS_TESTONLY", "1")
    monkeypatch.setenv("SANDBOX_RUNNER_TOKEN", "wb19-isolated-control")
    monkeypatch.setenv("SCRIPT_RUNNER_WORKSPACE", str(root))
    from core.sandbox.runner import server

    monkeypatch.setattr(server, "_AUTH_TOKEN", "wb19-isolated-control")
    monkeypatch.setattr(server.app.router, "routes", list(server.app.routes))
    monkeypatch.delitem(sys.modules, "tests.features.workbench.workbench_observed_runner_fixture", raising=False)
    observer = importlib.import_module("tests.features.workbench.workbench_observed_runner_fixture")
    sessions = server.process_sessions

    class ActualHandle:
        def __init__(self, process):
            self.proc = process

        async def poll(self):
            return ("", "", self.proc.returncode)

        async def interrupt(self):
            if self.proc.returncode is None:
                self.proc.terminate()

        async def close(self):
            await self.interrupt()
            await self.proc.wait()

    async def start():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)", cwd=root
        )
        return ActualHandle(process)

    owner = ("workbench-19-isolated-owner", "observer-user")
    headers = {"Authorization": "Bearer wb19-isolated-control"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observer.app), base_url="http://127.0.0.1"
    ) as client:
        result = await client.post(
            "/workbench-test/process-observations", headers=headers, json={"user_id": owner[1]}
        )
        assert result.json()["appOwnerRunningWorkers"] == 0
        try:
            value = await sessions.start(start, owner, yield_time_ms=0)
            identity = value["session_id"]
            assert identity
            entry = sessions.entries[identity]
            entry.stdout = "retained-output"
            result = await client.post(
                "/workbench-test/process-observations", headers=headers, json={"user_id": owner[1]}
            )
            assert result.status_code == 200
            assert result.json()["complete"] is True
            assert result.json()["appOwnerRunningWorkers"] == 1
            assert entry.stdout == "retained-output"
            other = await client.post(
                "/workbench-test/process-observations", headers=headers, json={"user_id": "other"}
            )
            assert other.json()["appOwnerRunningWorkers"] == 0
            denied = await client.post(
                "/workbench-test/process-observations", json={"user_id": owner[1]}
            )
            assert denied.status_code == 401
        finally:
            await sessions.close_owner(owner[0])
        result = await client.post(
            "/workbench-test/process-observations", headers=headers, json={"user_id": owner[1]}
        )
        assert result.json()["appOwnerRunningWorkers"] == 0
