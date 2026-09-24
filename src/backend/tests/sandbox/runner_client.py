"""In-process HTTP client for runner contract tests."""

import asyncio
from types import SimpleNamespace
import httpx
from services.script_runner_service import server


async def run_runner(request):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="http://runner",
        headers={"Authorization": "Bearer " + server._AUTH_TOKEN} if server._AUTH_TOKEN else {},
    ) as client:
        response = await client.post("/processes/start", json=request.model_dump())
        response.raise_for_status()
        result = response.json()
        out, err = result.get("stdout", ""), result.get("stderr", "")
        while result["status"] == "running":
            response = await client.post(
                "/processes/write",
                json={
                    "session_id": result["session_id"],
                    "sandbox_session_id": request.session_id,
                    "user_id": request.user_id,
                    "yield_time_ms": 1000,
                },
            )
            response.raise_for_status()
            result = response.json()
            out += result.get("stdout", "")
            err += result.get("stderr", "")
        result.update(stdout=out, stderr=err)
        return SimpleNamespace(**result)


async def run_spawn(cmd, stdin_data, timeout, cwd, sandbox_launch=None):
    """Exercise the native spawn boundary with the managed lifetime controller."""
    import tempfile
    from pathlib import Path
    from services.script_runner_service.process_api import LocalHandle
    from services.script_runner_service.process_sessions import ProcessSessions

    sessions = ProcessSessions()

    async def spawn():
        directory = Path(tempfile.mkdtemp(prefix=".__test_process_", dir=cwd))
        handle = LocalHandle(server, directory)
        handle.stdin.close()
        path = directory / "stdin.json"
        path.write_text(stdin_data)
        handle.stdin = path.open("rb")
        handle.proc = await server._spawn_subprocess(
            cmd, cwd, sandbox_launch, (handle.stdin, handle.stdout, handle.stderr)
        )
        return handle

    result = await sessions.start(spawn, ("native-test", ""), 1000, timeout)
    out, err = result["stdout"], result["stderr"]
    while result["status"] == "running":
        result = await sessions.write(result["session_id"], ("native-test", ""), "", 1000)
        out += result["stdout"]
        err += result["stderr"]
    await sessions.close_all()
    result.update(stdout=out, stderr=err)
    return result
