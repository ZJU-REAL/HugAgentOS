"""Stage one attempt and own its exact managed process; no detached shell children."""

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from core.sandbox import ProcessRequest, get_sandbox_provider
from core.sandbox.providers.opensandbox.recovery import (
    execution_reference,
    recover_execution,
    release_execution,
)

from . import state
from .callback import resolve_callback_base
from .files import _job_directory, _quote_path
from .runner import RUNNER_SOURCE
from .sdk import SDK_SOURCE


@dataclass
class Execution:
    job_id: str
    attempt_id: str
    directory: str
    request: ProcessRequest
    result: dict
    provider: Any

    async def poll(self, wait_ms=250):
        sid = self.result.get("session_id")
        if sid and self.result.get("status") == "running":
            self.result = await self.provider.write_stdin(
                sid,
                sandbox_session_id=self.request.session_id,
                user_id=self.request.user_id,
                yield_time_ms=wait_ms,
            )
        return self.result

    async def stop(self):
        # Renew atomically before I/O: a stale monitor must never kill a new owner.
        if not state.renew(self.job_id, self.attempt_id):
            await self.release()
            return False
        return await asyncio.wait_for(self._stop(), timeout=15)

    async def release(self):
        await asyncio.wait_for(
            release_execution(self.provider, self.result.get("session_id"), self.request),
            timeout=15,
        )

    async def _stop(self):
        sid = self.result.get("session_id")
        if sid and self.result.get("status") == "running":
            self.result = await self.provider.write_stdin(
                sid,
                sandbox_session_id=self.request.session_id,
                user_id=self.request.user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            for _ in range(10):
                if self.result.get("status") != "running":
                    break
                await self.poll(1000)
        if self.result.get("status") != "exited" or self.result.get("error"):
            raise RuntimeError("无法确认原作业进程已停止，禁止自动重启")
        return state.save_execution(self.job_id, self.attempt_id, phase="exited")

    async def receipt(self):
        try:
            raw = await self.provider.get_file(
                self.request.session_id,
                self.directory + "/lifecycle.final",
                user_id=self.request.user_id,
            )
            data = json.loads(raw)
            if data.get("attempt_id") == self.attempt_id and data.get("status") in (
                "completed",
                "failed",
            ):
                return data
        except Exception as fallback_error:
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.jobs.process:receipt', fallback_error)
        return None

    async def log(self):
        # Use a bounded shell read, not an unbounded download of user output.
        from .files import _sbx_bash

        _, out, _ = await _sbx_bash(
            f"tail -c 4000 {_quote_path(self.directory + '/runner.log')} 2>/dev/null",
            session_id=self.request.session_id,
            user_id=self.request.user_id,
            timeout=15,
        )
        return out[-4000:]


def request_for(row, directory, script):
    return ProcessRequest(
        script_content=script,
        script_name="job_runner.sh",
        language="bash",
        session_id=row["session_id"],
        user_id=row["user_id"],
        # The supervisor enforces the durable job budget and always interrupts
        # this exact handle on expiry. HTTP wait duration is not its lifetime.
        timeout=None,
    )


async def launch(row):
    provider = get_sandbox_provider()
    execution = row["meta"]["execution"]
    attempt = execution["attempt_id"]
    directory = attempt_directory(row["session_id"], row["job_id"], attempt)
    callback = await resolve_callback_base(session_id=row["session_id"], user_id=row["user_id"])
    config = {
        "JOB_ID": row["job_id"],
        "JOB_TOKEN": row["meta"]["token"],
        "JOB_CALLBACK_URL": callback,
        "JOB_ATTEMPT_ID": attempt,
        "PYTHONUNBUFFERED": "1",
    }
    for name, content in (
        ("hugagent_job.py", SDK_SOURCE),
        ("_runner.py", RUNNER_SOURCE),
        ("user_script.py", row["script"]),
        ("execution.json", json.dumps(config)),
    ):
        await provider.put_file(
            row["session_id"], directory + "/" + name, content.encode(), user_id=row["user_id"]
        )
    interpreter = row["meta"].get("start_params", {}).get("interpreter") or "${PY_BIN:-python3}"
    if interpreter == "${PY_BIN:-python3}":
        interpreter = '"${PY_BIN:-python3}"'
    script = (
        f"cd {_quote_path(directory)}\n"
        "chmod 600 execution.json\n"
        f"exec {interpreter} -u _runner.py > runner.log 2>&1"
    )
    req = request_for(row, directory, script)
    current = state.snapshot(row["job_id"])
    if current["status"] != "pending":
        raise RuntimeError("作业在准备期间已取消")
    if not state.save_execution(row["job_id"], attempt, phase="launching", directory=directory):
        raise RuntimeError("作业执行权已转移")
    result = await provider.start_process(req, yield_time_ms=250)
    managed = Execution(row["job_id"], attempt, directory, req, result, provider)
    try:
        ref = await execution_reference(provider, result.get("session_id"), req)
        state.save_execution(row["job_id"], attempt, phase="launched", reference=ref)
    except BaseException:
        await managed.stop()
        raise
    return managed


def attempt_directory(session_id, job_id, attempt_id, *, windows_local=None):
    """Keep Windows SDK imports within MAX_PATH without shortening attempt identity."""
    if windows_local is None:
        from core.config.local_mode import local_mode_enabled

        windows_local = sys.platform == "win32" and local_mode_enabled()
    base = _job_directory(session_id, job_id)
    if windows_local:
        # The globally unique 128-bit attempt id already isolates each execution.
        # One less directory avoids SDK filenames crossing MAX_PATH in desktop data roots.
        base = base.rsplit("/", 1)[0]
    return base + "/" + attempt_id


async def recover(row):
    execution = row["meta"].get("execution") or {}
    ref = execution.get("reference")
    if not ref:
        raise RuntimeError("缺少原执行实例的持久引用，无法确认进程状态；禁止重复续跑")
    provider = get_sandbox_provider()
    req = request_for(row, execution["directory"], "")
    result = await recover_execution(provider, ref, req)
    return Execution(
        row["job_id"], execution["attempt_id"], execution["directory"], req, result, provider
    )
