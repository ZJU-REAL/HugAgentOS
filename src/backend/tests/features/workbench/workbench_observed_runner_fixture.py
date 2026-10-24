"""Test-only actual ScriptRunner process registry observation.

Start with uvicorn tests.features.workbench.workbench_observed_runner_fixture:app.
This module is never registered by production server or desktop entrypoints.
"""

from pydantic import BaseModel, ConfigDict, Field
from tests.features.workbench.workbench_observer_paths import LoopbackOnly, guard_cli_binding, guard_environment

guard_environment("SCRIPT_RUNNER_WORKSPACE")
guard_cli_binding()
from core.sandbox.runner import server

app = LoopbackOnly(server.app)


class ObserverIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=200)


@server.app.post("/workbench-test/process-observations")
async def observe(body: ObserverIdentity):
    running = starting = unknown = 0
    # Runs without await on the registry's own event loop. No output is consumed.
    for entry in list(server.process_sessions.entries.values()):
        if entry.owner[1] != body.user_id or not entry.owner[0].startswith("workbench-"):
            continue
        if entry.done.is_set():
            continue
        if entry.handle is None:
            starting += 1
            continue
        process = getattr(entry.handle, "proc", None)
        if process is None:
            unknown += 1
        elif process.returncode is None:
            running += 1
    return {
        "provider": "script_runner",
        "complete": unknown == 0,
        "appOwnerRunningWorkers": running,
        "appOwnerStartingWorkers": starting,
        "unknownWorkers": unknown,
        "observation": "actual_local_process_handles",
    }


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5219)
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535:
        raise RuntimeError("Invalid isolated test port")
    uvicorn.run(app, host="127.0.0.1", port=options.port, access_log=False)
