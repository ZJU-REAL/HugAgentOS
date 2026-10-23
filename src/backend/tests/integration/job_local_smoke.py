"""Native local run_job acceptance using isolated data and real HTTP/processes.

Run with the desktop's private Python, without packaging or changing client data:
python job_local_smoke.py --output <new absolute directory>
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import secrets
import socket
import sys
from pathlib import Path


def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def configure(root, backend_port, runner_port):
    data = root / "isolated data 中文 with spaces"
    data.mkdir()
    workspace = data / "workspace"
    workspace.mkdir()
    os.environ.update(
        DEPLOY_PROFILE="local",
        JX_EDITION="ce",
        HUGAGENT_HOME=str(data),
        HUGAGENT_CAPS_ROOT=str(data / "capabilities"),
        DATABASE_URL="sqlite:///" + (data / "jobs.sqlite").as_posix(),
        REDIS_URL="",
        SESSION_STORE="memory",
        AUTH_MODE="mock",
        PORT=str(backend_port),
        BACKEND_PORT=str(backend_port),
        BACKEND_INTERNAL_URL=f"http://127.0.0.1:{backend_port}",
        SANDBOX_PROVIDER="script_runner",
        SANDBOX_RUNNER_URL=f"http://127.0.0.1:{runner_port}",
        SANDBOX_RUNNER_TOKEN=secrets.token_urlsafe(32),
        SANDBOX_TOOLS_ENABLED="true",
        SCRIPT_RUNNER_WORKSPACE=str(workspace),
        SANDBOX_SKILLS_DIR=str(data / "skills"),
        STORAGE_TYPE="local",
        STORAGE_PATH=str(data / "storage"),
        NO_PROXY="127.0.0.1,localhost,::1",
        no_proxy="127.0.0.1,localhost,::1",
    )
    os.environ.pop("JOB_CALLBACK_URL", None)
    return data


SCRIPT = """from hugagent_job import ledger, log
import time
items = [{"key": str(i), "payload": {"n": i}} for i in range(5)]
ledger.seed(items)
for item in items:
    time.sleep(0.4)
    ledger.update(item["key"], status="done", result={"value": item["payload"]["n"] * 2})
    log("progress " + item["key"])
"""


async def acceptance(root, backend_socket, runner_socket):
    import core.jobs.callback as callback
    import core.jobs.files as files
    import core.jobs.owner as owner
    import core.jobs.runtime as job_runtime
    import core.jobs.state as state
    import core.jobs.supervisor as supervisor
    import uvicorn
    from api.routes.v1 import internal_jobs
    from core.db.engine import Base, SessionLocal, engine
    from core.db.models import UserShadow
    from core.llm.factory.tools.definitions import ToolAssembly
    from core.llm.tools.job_tool import register_run_job
    from core.sandbox import get_sandbox_provider
    from core.sandbox.runner import server as runner
    from fastapi import FastAPI

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(UserShadow(user_id="acceptance-user", username="synthetic"))
        db.commit()

    app = FastAPI()
    app.include_router(internal_jobs.router)
    events = []

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.middleware("http")
    async def capture(request, call_next):
        body = await request.json() if request.method == "POST" else {}
        response = await call_next(request)
        if "/v1/internal/jobs/" in request.url.path:
            events.append(
                {
                    "endpoint": request.url.path.rsplit("/", 1)[-1],
                    "lifecycle": body.get("lifecycle"),
                    "op": body.get("op"),
                    "status": body.get("status"),
                    "http_status": response.status_code,
                }
            )
        return response

    servers = [
        uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)),
        uvicorn.Server(uvicorn.Config(runner.app, log_level="error", access_log=False)),
    ]
    tasks = [
        asyncio.create_task(service.serve(sockets=[sock]))
        for service, sock in zip(servers, (backend_socket, runner_socket))
    ]

    def payload(response):
        return json.loads(
            "".join(
                block.get("text", "") if isinstance(block, dict) else block.text
                for block in response.content
            )
        )

    try:
        for _ in range(200):
            if all(service.started for service in servers):
                break
            await asyncio.sleep(0.025)
        assert all(service.started for service in servers), "HTTP service did not start"
        owner.bind()
        provider = get_sandbox_provider()
        session_id = "local-acceptance"
        user_id = "acceptance-user"
        await provider.put_file(session_id, "acceptance.py", SCRIPT.encode(), user_id=user_id)

        collector = ToolAssembly()
        register_run_job(
            collector,
            user_id=user_id,
            chat_id=None,
            sandbox_session_id=session_id,
        )
        tool = next(t for t in collector if t.name == "run_job")
        assert tool is not None, "run_job registration failed"
        run_job = tool._func
        started = payload(
            await asyncio.wait_for(
                run_job(action="start", script_path="acceptance.py", wait=False, max_seconds=30),
                timeout=40,
            )
        )
        assert started.get("ok") and started["status"] == "started", started
        jid = started["job_id"]
        observed = []
        for _ in range(200):
            progress = payload(await run_job(action="status", job_id=jid))
            observed.append({"status": progress["status"], "done": progress["stats"]["done"]})
            if progress["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.025)
        final = await asyncio.wait_for(job_runtime.run_and_wait(jid), timeout=10)
        assert final["status"] == "completed" and final["stats"]["done"] == 5, final
        assert any(0 < row["done"] < 5 and row["status"] == "running" for row in observed), observed
        assert any(
            event["lifecycle"] == "running" and event["http_status"] == 200 for event in events
        )
        assert any(
            event["lifecycle"] == "completed" and event["http_status"] == 200 for event in events
        )
        assert sum(event["op"] == "update" and event["http_status"] == 200 for event in events) == 5
        row = state.snapshot(jid)
        receipt_path = row["meta"]["execution"]["directory"] + "/lifecycle.final"
        receipt = json.loads(await provider.get_file(session_id, receipt_path, user_id=user_id))
        assert receipt["status"] == "completed"

        # Exercise missing private Python through the real runner, not a stub.
        original_python = runner.SAFE_ENV.get("PY_BIN")
        original_environment = runner._workspace_rules().subprocess_environment
        workspace_rules = runner._workspace_rules()
        workspace_rules.subprocess_environment = lambda cwd: {
            "PY_BIN": str(root / "missing python.exe")
        }
        try:
            try:
                await callback.resolve_callback_base(session_id=session_id, user_id=user_id)
            except RuntimeError as error:
                diagnostic = str(error)
                assert "脚本执行失败" in diagnostic and "127" in diagnostic, diagnostic
                assert "沙箱连不上后端" not in diagnostic, diagnostic
            else:
                raise AssertionError("missing interpreter accepted")
        finally:
            workspace_rules.subprocess_environment = original_environment
            if original_python:
                runner.SAFE_ENV["PY_BIN"] = original_python

        # A deterministic Windows registry proxy model, under native SAFE_ENV.
        # It never reads or writes the user's proxy configuration.
        from core.jobs.sdk import SDK_SOURCE
        from tests.sandbox.runner_client import run_spawn

        assert not runner.SAFE_ENV.get("NO_PROXY")
        assert not runner.SAFE_ENV.get("no_proxy")
        base = callback.callback_base_candidates()[0]
        bootstrap = (
            "import os, urllib.request; "
            "assert not os.environ.get('NO_PROXY'); "
            "assert not os.environ.get('no_proxy'); "
            "urllib.request.proxy_bypass=lambda host: False; "
            "urllib.request.getproxies=lambda: {'http':'http://127.0.0.1:1'}; "
        )
        probe = await run_spawn(
            [sys.executable, "-c", bootstrap + callback._PROBE_SOURCE, json.dumps([base])],
            "{}",
            10,
            str(root),
        )
        assert probe["exit_code"] == 0 and "PICK " + base in probe["stdout"], probe
        # A fenced completed job must return 403 directly, rather than hit proxy.
        sdk_source = (
            bootstrap
            + f"import json; os.environ.update(json.load(open({(row['meta']['execution']['directory'] + '/execution.json')!r}, encoding='utf-8'))); "
            + SDK_SOURCE
            + "\ntry:\n    _post('log', {'message':'expired'}, timeout=1)\n"
            + "except JobError as exc:\n    assert 'HTTP 403' in str(exc), str(exc)\n"
            + "else:\n    raise AssertionError('expired job token accepted')\n"
            + "print('DIRECT CALLBACK FENCED')\n"
        )
        fenced = await run_spawn([sys.executable, "-c", sdk_source], "{}", 10, str(root))
        assert fenced["exit_code"] == 0 and "DIRECT CALLBACK FENCED" in fenced["stdout"], fenced

        report = {
            "platform": platform.platform(),
            "python": sys.executable,
            "bash": runner.INTERPRETERS["bash"][0],
            "clean_runner_no_proxy": {
                key: runner.SAFE_ENV.get(key) for key in ("NO_PROXY", "no_proxy")
            },
            "callback_base": callback.callback_base_candidates(),
            "job_id": jid,
            "started": started["status"],
            "final_status": final["status"],
            "done": final["stats"]["done"],
            "observed_partial_progress": True,
            "registry_proxy_model_bypassed": True,
            "expired_job_token_rejected": True,
            "callbacks": events,
            "receipt": receipt["status"],
            "missing_interpreter_diagnostic": diagnostic,
            "source_sha256": {
                name: hashlib.sha256(
                    Path(callback.__file__).with_name(name).read_bytes()
                ).hexdigest()
                for name in ("callback.py", "sdk.py")
            },
        }
        (root / "acceptance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True), flush=True)
    finally:
        await supervisor.shutdown()
        await runner.process_sessions.close_all()
        for service in servers:
            service.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.expanduser().resolve()
    if not args.output.is_absolute() or root.exists():
        parser.error("--output must be a new absolute directory")
    root.mkdir(parents=True)
    backend_socket, runner_socket = listener(), listener()
    configure(root, backend_socket.getsockname()[1], runner_socket.getsockname()[1])
    try:
        asyncio.run(acceptance(root, backend_socket, runner_socket))
    finally:
        backend_socket.close()
        runner_socket.close()
