"""Behavior at the runner HTTP boundary, using real child processes."""

import asyncio
import httpx
import pytest
from services.script_runner_service import server


@pytest.fixture
async def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "_AUTH_TOKEN", "")
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://runner"
    ) as client:
        yield client
    if hasattr(server, "process_sessions"):
        await server.process_sessions.close_all()


async def test_yield_does_not_kill_and_followup_is_incremental(runner):
    response = await runner.post(
        "/processes/start",
        json={
            "session_id": "chat-a",
            "user_id": "alice",
            "script_content": "printf first; sleep 0.6; printf second",
            "script_name": "_bash.sh",
            "language": "bash",
            "yield_time_ms": 250,
        },
    )
    assert response.status_code == 200, response.text
    started = response.json()
    assert started["status"] == "running"
    assert started["stdout"] == "first"
    finished = await runner.post(
        "/processes/write",
        json={
            "sandbox_session_id": "chat-a",
            "user_id": "alice",
            "session_id": started["session_id"],
            "yield_time_ms": 2000,
        },
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["stdout"] == "second"
    assert finished.json()["status"] == "exited"
    assert finished.json()["exit_code"] == 0


async def start_command(runner, command, **kwargs):
    response = await runner.post(
        "/processes/start",
        json={
            "session_id": "chat-a",
            "user_id": "alice",
            "script_content": command,
            "script_name": "_bash.sh",
            "language": "bash",
            "yield_time_ms": 250,
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def poll(runner, sid, **kwargs):
    return await runner.post(
        "/processes/write",
        json={
            "sandbox_session_id": "chat-a",
            "user_id": "alice",
            "session_id": sid,
            "yield_time_ms": 2000,
            **kwargs,
        },
    )


async def test_process_cannot_be_read_or_interrupted_by_another_owner(runner):
    result = await start_command(runner, "sleep 10")
    for wrong in ({"user_id": "bob"}, {"sandbox_session_id": "chat-b"}):
        denied = await poll(runner, result["session_id"], chars="\x03", **wrong)
        assert denied.status_code == 400
    alive = await poll(runner, result["session_id"], yield_time_ms=0)
    assert alive.json()["status"] == "running"
    assert (await poll(runner, result["session_id"], chars="\x03")).json()["status"] == "exited"


async def test_nonpty_rejects_input_and_ctrl_c_interrupts_a_concurrent_wait(runner):
    result = await start_command(runner, "sleep 10")
    sid = result["session_id"]
    rejected = await poll(runner, sid, chars="echo bypass\n")
    assert rejected.status_code == 400
    waiting = asyncio.create_task(poll(runner, sid, yield_time_ms=10000))
    await asyncio.sleep(0.05)
    interrupted = await poll(runner, sid, chars="\x03")
    assert interrupted.json()["status"] == "exited"
    assert interrupted.json()["exit_code"] != 0
    assert (await asyncio.wait_for(waiting, 2)).json()["status"] == "exited"


async def test_cancelled_http_wait_does_not_cancel_process(runner):
    started = await start_command(runner, "sleep 0.8; printf survived")
    waiting = asyncio.create_task(poll(runner, started["session_id"]))
    await asyncio.sleep(0.1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    result = (await poll(runner, started["session_id"])).json()
    assert result["stdout"] == "survived"
    assert result["exit_code"] == 0


async def test_explicit_deadline_preserves_output_and_returns_failure(runner):
    started = await start_command(runner, "printf before; sleep 20", timeout=1)
    result = (await poll(runner, started["session_id"])).json()
    assert started["stdout"] + result["stdout"] == "before"
    assert result["exit_code"] == 124
    assert "deadline" in result["error"]


async def test_close_session_cleans_children_and_invalidates_handles(runner, tmp_path):
    marker = tmp_path / "escaped"
    started = await start_command(runner, f"(sleep 0.8; touch {marker}) & wait")
    response = await runner.post("/sessions/close", json={"session_id": "chat-a"})
    assert response.status_code == 200
    await asyncio.sleep(1)
    assert not marker.exists()
    assert (await poll(runner, started["session_id"])).status_code == 400


async def test_output_overflow_is_explicit_and_full_log_is_readable(runner):
    result = await start_command(runner, 'printf "%0400000d\\n" 0')
    all_output = result["stdout"]
    omitted = result.get("output_omitted_chars", 0)
    paths = result["output_files"]
    while result["status"] == "running":
        result = (await poll(runner, result["session_id"])).json()
        all_output += result["stdout"]
        omitted += result.get("output_omitted_chars", 0)
    assert len(all_output) + omitted == 400001
    # Use the existing file API, not a private process buffer.
    import base64

    response = await runner.post(
        "/get_file",
        json={
            "session_id": "chat-a",
            "user_id": "alice",
            "path": paths["stdout"],
        },
    )
    assert response.status_code == 200, response.text
    assert len(base64.b64decode(response.json()["content_b64"])) == 400001


async def test_process_limit_rejects_new_work_without_killing_existing_jobs(runner, monkeypatch):
    from services.script_runner_service import process_sessions

    monkeypatch.setattr(process_sessions, "MAX_PROCESSES", 1)
    first = await start_command(runner, "sleep 10")
    rejected = await runner.post(
        "/processes/start",
        json={
            "session_id": "chat-a",
            "script_content": "echo should-not-run",
            "script_name": "x.sh",
            "language": "bash",
            "yield_time_ms": 250,
        },
    )
    assert rejected.status_code == 400
    assert (await poll(runner, first["session_id"], yield_time_ms=0)).json()["status"] == "running"


async def test_close_during_startup_does_not_leave_an_untracked_process():
    from services.script_runner_service.process_sessions import ProcessSessions, ProcessSessionError

    entered, release = asyncio.Event(), asyncio.Event()

    class Handle:
        closed = False

        async def poll(self):
            return "", "", None

        async def close(self):
            self.closed = True

    handle = Handle()

    async def factory():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A remote spawn can finish despite cancellation of its waiter.
            pass
        return handle

    sessions = ProcessSessions()
    starting = asyncio.create_task(sessions.start(factory, ("chat", "user"), 0))
    await entered.wait()
    await sessions.close_owner("chat")
    with pytest.raises(ProcessSessionError):
        await asyncio.wait_for(starting, 1)
    await sessions.close_all()
    assert handle.closed


async def test_completed_nonzero_exit_keeps_stdout_and_stderr(runner):
    result = await start_command(runner, "printf out; printf err >&2; exit 7")
    assert result["status"] == "exited"
    assert result["exit_code"] == 7
    assert result["stdout"] == "out"
    assert result["stderr"] == "err"


async def test_runner_requires_authentication_for_process_endpoints(runner, monkeypatch):
    monkeypatch.setattr(server, "_AUTH_TOKEN", "test-only-token")
    response = await runner.post(
        "/processes/start",
        json={
            "session_id": "a",
            "script_content": "echo no",
            "script_name": "x.sh",
            "language": "bash",
        },
    )
    assert response.status_code == 401


async def test_capability_identity_survives_start_and_followup(runner):
    started = await start_command(
        runner, "sleep 0.5", capability_run_id="run-1", capability_scope="scope-a"
    )
    finished = (await poll(runner, started["session_id"])).json()
    assert finished["_capability"] == {"run_id": "run-1", "scope": "scope-a", "user_id": "alice"}


@pytest.mark.parametrize("natural_exit", [False, True])
async def test_concurrent_close_does_not_interrupt_inflight_cleanup(natural_exit):
    from services.script_runner_service.process_sessions import ProcessSessions

    entered, release = asyncio.Event(), asyncio.Event()

    class Handle:
        closed = False

        async def poll(self):
            return "", "", 0 if natural_exit else None

        async def interrupt(self):
            pass

        async def close(self):
            entered.set()
            await release.wait()
            self.closed = True

    handle = Handle()

    async def factory():
        return handle

    sessions = ProcessSessions()
    started = asyncio.create_task(sessions.start(factory, ("chat", "user"), 0))
    if natural_exit:
        await entered.wait()
        first = asyncio.create_task(sessions.close_owner("chat"))
    else:
        await started
        first = asyncio.create_task(sessions.close_owner("chat"))
        await entered.wait()
    second = asyncio.create_task(sessions.close_owner("chat"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    await asyncio.gather(started, return_exceptions=True)
    assert handle.closed
    await sessions.close_all()


async def test_removed_execute_endpoint_cannot_launch_commands(runner):
    response = await runner.post(
        "/execute",
        json={
            "session_id": "chat-a",
            "language": "bash",
            "script_name": "old.sh",
            "script_content": "printf should-not-run",
        },
    )
    assert response.status_code == 404


async def test_python_process_preserves_arguments_inputs_and_session_files(runner):
    response = await runner.post(
        "/processes/start",
        json={
            "session_id": "chat-a",
            "language": "python",
            "script_name": "probe.py",
            "script_content": "import json,sys,pathlib; p=json.load(sys.stdin); print(sys.argv[1],p['value'],pathlib.Path('input.txt').read_text()); pathlib.Path('result.txt').write_text('saved')",
            "params": {"_args": ["two words"], "value": 42},
            "input_files": {"input.txt": "seeded"},
            "yield_time_ms": 1000,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["exit_code"] == 0, result
    assert result["stdout"] == "two words 42 seeded\n"
    response = await runner.post("/get_file", json={"session_id": "chat-a", "path": "result.txt"})
    assert response.status_code == 200
    import base64

    assert base64.b64decode(response.json()["content_b64"]) == b"saved"


async def test_python_resources_resolve_next_to_script(runner):
    response = await runner.post(
        "/processes/start",
        json={
            "session_id": "module-chat",
            "script_name": "main.py",
            "language": "python",
            "script_content": "import helper; from pathlib import Path; print(helper.VALUE); print(Path(__file__).with_name('sibling.txt').read_text())",
            "resource_files": {"helper.py": "VALUE=42", "sibling.txt": "sibling"},
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["exit_code"] == 0, result
    assert result["stdout"].splitlines() == ["42", "sibling"]
