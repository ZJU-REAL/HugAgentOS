"""Opt-in real PostgreSQL + OpenSandbox + current callback router integration test.

Run in the backend container with this checkout on PYTHONPATH:
python job_managed_smoke.py --source-job <existing-job-id>
Uses the source job's authorized sandbox; creates only synthetic, non-LLM jobs.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import time

import uvicorn
from api.routes.v1.internal_jobs import router
from core.db.engine import SessionLocal
from core.db.models import Job
from fastapi import FastAPI
from orchestration import job_runtime as runtime
from orchestration.jobs import owner, process, state

SCRIPT = """from hugagent_job import ledger, job
import time
items = [{"key": str(i), "payload": {"n": i}} for i in range(24)]
ledger.seed(items)
def handle(item):
    time.sleep(0.05)
    return {"value": item["payload"]["n"] * 2}
job.map(items, handle, concurrency=8)
"""
created = []


async def main(source_job):
    with SessionLocal() as db:
        original = db.query(Job).filter_by(job_id=source_job).one()
        user, session = original.user_id, original.sandbox_session_id
    owner.bind()

    async def start(script=SCRIPT, **options):
        jid = await runtime.start_job(
            user_id=user,
            chat_id=None,
            name="managed-job-integration",
            script_path="integration.py",
            script_text=script,
            session_id=session,
            budget={"max_seconds": 60},
            start_params={"wake_on_finish": False},
            **options,
        )
        created.append(jid)
        return jid

    async def wait(jid):
        return await asyncio.wait_for(runtime.run_and_wait(jid), 75)

    # Real HTTP callbacks hit this checkout's router, with concurrent ledger writes.
    jid = await start()
    result = await wait(jid)
    assert result["status"] == "completed" and result["stats"]["done"] == 24, result
    print("PASS concurrent ledger: 24/24", flush=True)

    # Start from a temporary worker loop, then let that loop close immediately.
    jid = await asyncio.to_thread(lambda: asyncio.run(start()))
    result = await wait(jid)
    assert result["status"] == "completed" and result["stats"]["done"] == 24, result
    print("PASS worker loop exits while job survives", flush=True)

    slow = await start("import time\ntime.sleep(25)")
    survivor = await start("import time\ntime.sleep(4)")
    # Reconstruct an owner-bound handle from a durable OpenSandbox reference.
    recovered = await process.recover(state.snapshot(slow))
    assert (await recovered.poll())["status"] == "running"
    assert await runtime.cancel_job(slow, user_id=user)
    assert (await wait(slow))["status"] == "cancelled"
    assert (await wait(survivor))["status"] == "completed"
    print("PASS durable command recovery and isolated cancellation", flush=True)

    # A separate Python worker adopts the same durable command, without launching it.
    jid = await start("import time\ntime.sleep(8)")
    child_code = """import asyncio, sys
from orchestration.jobs import owner, state, process, supervisor
async def main():
    owner.bind()
    row = state.snapshot(sys.argv[1])
    state.take_control(row['job_id'], row['meta']['execution']['attempt_id'])
    execution = await process.recover(state.snapshot(row['job_id']))
    supervisor.attach(execution)
    result = await supervisor.wait(row['job_id'])
    assert result['status'] == 'completed', result
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        jid,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await asyncio.wait_for(child.communicate(), 45)
    assert child.returncode == 0, (output + errors).decode()[-5000:]
    assert state.snapshot(jid)["status"] == "completed"
    print("PASS another Python worker adopts existing command", flush=True)

    # Resume repeats the script, but SDK skips already settled rows.
    partial = SCRIPT.replace(
        "job.map(items, handle, concurrency=8)",
        "job.map(items[:2], handle, concurrency=2)\ntime.sleep(25)\njob.map(items, handle, concurrency=8)",
    )
    jid = await start(partial)
    for _ in range(40):
        if state.snapshot(jid)["stats"]["done"] >= 2:
            break
        await asyncio.sleep(0.25)
    assert state.snapshot(jid)["stats"]["done"] == 2
    await runtime.cancel_job(jid, user_id=user)
    await wait(jid)
    old_attempt = state.snapshot(jid)["meta"]["execution"]["attempt_id"]
    resumed = await runtime.resume_job(jid, user_id=user)
    assert resumed["ok"], resumed
    result = await wait(jid)
    assert result["status"] == "completed" and result["stats"]["done"] == 24, result
    assert state.snapshot(jid)["meta"]["execution"]["attempt_id"] != old_attempt
    print("PASS cancel/resume with settled ledger retained", flush=True)

    # Invalid interpreter must surface an actual failure, not 'started'.
    try:
        await start(interpreter="/definitely/missing/python")
    except RuntimeError:
        print("PASS missing interpreter fails during startup", flush=True)
    else:
        raise AssertionError("invalid interpreter reported started")

    print(json.dumps({"passed": 7, "jobs": created}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-job", required=True)
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    app = FastAPI()
    app.include_router(router)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("isolated callback server did not start")
    os.environ["JOB_CALLBACK_URL"] = f"http://backend:{args.port}"
    try:
        asyncio.run(main(args.source_job))
    finally:
        server.should_exit = True
        thread.join(timeout=5)
