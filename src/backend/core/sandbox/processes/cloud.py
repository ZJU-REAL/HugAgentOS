"""Cloud adapters for the shared managed-process contract.

SDK command IDs never leave this module: tool handles are owner-bound opaque IDs.
All operations are routed to the same event loop as the owning provider.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
import uuid
from datetime import timedelta

from core.sandbox.errors import SandboxError
from core.sandbox.processes.sessions import ProcessSessionError, ProcessSessions


# No process outlives its command, officecli's resident included: write each edit to disk at once.
_COMMAND_ENVS = {"OFFICECLI_RESIDENT_FLUSH": "each"}


def _module_owned(session_id) -> bool:
    """A module's dedicated sandbox never receives the platform's extra credentials."""
    from core.harness.registry import sandbox_route

    return sandbox_route(str(session_id or "")) is not None


class CloudProcesses:
    def __init__(self, provider):
        self.provider = provider
        self.sessions = ProcessSessions()
        self.loop = asyncio.get_running_loop()

    async def _dispatch(self, action):
        if asyncio.get_running_loop() is self.loop:
            return await action()
        if self.loop.is_closed():
            raise SandboxError("Process service stopped; old process sessions cannot be resumed")
        from core.sandbox.processes.dispatch import _forward_to_loop

        return await _forward_to_loop(self.loop, action())

    async def start(self, req, yield_time_ms=60000):
        async def run():
            try:
                return await self.sessions.start(
                    lambda: self._spawn(req),
                    (req.session_id or "", req.user_id or ""),
                    max(250, min(yield_time_ms, 60000)),
                    req.timeout,
                    yield_on_output=req.yield_on_output,
                )
            except ProcessSessionError as exc:
                raise SandboxError(str(exc)) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise SandboxError(
                    f"Command start failed; do not automatically rerun if launch state is uncertain: {exc}"
                ) from exc

        return await self._dispatch(run)

    async def write(
        self, session_id, *, sandbox_session_id, user_id=None, chars="", yield_time_ms=60000
    ):
        async def run():
            try:
                return await self.sessions.write(
                    session_id,
                    (sandbox_session_id, user_id or ""),
                    chars,
                    yield_time_ms,
                )
            except ProcessSessionError as exc:
                raise SandboxError(str(exc)) from exc

        return await self._dispatch(run)

    async def close_owner(self, session_id):
        return await self._dispatch(lambda: self.sessions.close_owner(session_id))

    async def close_all(self):
        return await self._dispatch(self.sessions.close_all)

    async def _spawn(self, req):
        from core.sandbox.workspace.identity import SANDBOX_RUN_GID, SANDBOX_RUN_UID
        from core.sandbox.workspace.paths import WORKSPACE
        from core.sandbox.providers.account_env import _firecrawl_extra_envs
        from core.sandbox.credential_dispatch import credential_environment

        provider = self.provider
        interpreters = {"bash": "bash", "python": "python3 -u", "javascript": "node"}
        if req.language not in interpreters:
            raise SandboxError(f"Unsupported language: {req.language}")
        suffix = {"bash": ".sh", "python": ".py", "javascript": ".js"}[req.language]
        path = f"{WORKSPACE}/.__process_script_{uuid.uuid4().hex}{suffix}"
        quoted_path = shlex.quote(path)
        params = dict(req.params)
        raw_args = params.pop("_args", [])
        args = [str(arg) for arg in raw_args] if isinstance(raw_args, list) else []
        invocation = interpreters[req.language] + " " + quoted_path
        if args:
            invocation += " " + " ".join(shlex.quote(arg) for arg in args)
        if req.params or req.language != "bash":
            invocation = (
                "printf %s "
                + shlex.quote(json.dumps(params, ensure_ascii=False))
                + " | "
                + invocation
            )
        else:
            invocation += " < /dev/null"
        # Keep the script alongside staged modules/resources, preserving __file__,
        # require('./module') and argv conventions. Each launch owns one file.
        wrapper = (
            "trap " + shlex.quote("rm -f -- " + quoted_path) + " EXIT; "
            "printf %s "
            + shlex.quote(req.script_content)
            + " > "
            + quoted_path
            + " && "
            + invocation
        )
        command = "bash -c " + shlex.quote(wrapper)
        if provider.name == "opensandbox":
            from core.sandbox.providers.opensandbox.runtime import _EXECD_BASH_ENVS
            from opensandbox.models.execd import RunCommandOpts

            async with provider._session_use(req.session_id, req.user_id) as sess:
                # skip_health_check starts execd asynchronously. Readiness reads may retry;
                # staging and process launch must never be replayed after uncertain results.
                async with asyncio.timeout(provider._ready_timeout_s):
                    while not await sess.sandbox.is_healthy():
                        await asyncio.sleep(0.25)
                await provider._sync_inputs(sess, req)
                opts = RunCommandOpts(
                    background=True,
                    timeout=None if req.timeout is None else timedelta(seconds=req.timeout),
                    working_directory=WORKSPACE,
                    uid=SANDBOX_RUN_UID,
                    gid=SANDBOX_RUN_GID,
                    envs=({**_EXECD_BASH_ENVS, **_COMMAND_ENVS, "EXECD_ACCESS_TOKEN": ""} if _module_owned(req.session_id) else {**_EXECD_BASH_ENVS, **_COMMAND_ENVS, **credential_environment(), **_firecrawl_extra_envs()}),
                )
                # As a subreaper the command keeps everything it starts, however detached,
                # until it ends; whatever is left then reaches PID 1 and is killed there.
                execution = await sess.sandbox.commands.run(
                    "sandbox-init --subreaper " + command, opts=opts
                )
                if not execution.id:
                    raise SandboxError("Background command returned no execution ID")
                return OpenSandboxHandle(provider, req, sess.sandbox.commands, execution.id)
        # Cube uses the E2B-compatible background command handle.
        async with await provider._get_session_lock(req.session_id):
            sbx = await provider._acquire_persistent(req.session_id, req.user_id)
            await provider._prepare_command(sbx, req)
            command = provider._myspace_prefix(req.user_id) + command
            handle = CubeHandle(provider, req)
            handle.command = await sbx.commands.run(
                command,
                background=True,
                cwd=WORKSPACE,
                user="root",
                envs=(
                    {**_COMMAND_ENVS, "EXECD_ACCESS_TOKEN": ""}
                    if _module_owned(req.session_id)
                    else {**_COMMAND_ENVS, **credential_environment(), **_firecrawl_extra_envs()}
                ),
                stdin=False,
                timeout=0,
                request_timeout=provider._request_timeout_s,
                on_stdout=handle.stdout,
                on_stderr=handle.stderr,
            )
            handle.waiter = asyncio.create_task(handle.command.wait())
            handle.sandbox = sbx
            return handle


class CloudHandle:
    poll_interval = 0.5

    def __init__(self, provider, req):
        self.provider = provider
        self.req = req
        self.last_touch = 0.0
        self.metadata = {}
        if provider.name == "cube":
            self.metadata["lifetime_note"] = (
                "Command waits do not impose a runtime limit. This Cube deployment may "
                "still enforce a sandbox lifetime that its API cannot renew."
            )
        self.finished = False

    async def touch(self):
        now = time.monotonic()
        if now - self.last_touch >= 30:
            touched = await self.provider.touch_session(self.req.session_id)
            if self.provider.name == "opensandbox" and not touched:
                raise SandboxError("Original sandbox binding retired; refusing to use an old command handle")
            self.last_touch = now


class OpenSandboxHandle(CloudHandle):
    def __init__(self, provider, req, commands, execution_id):
        super().__init__(provider, req)
        self.commands = commands
        self.execution_id = execution_id
        self.cursor = 0
        self.metadata["execution_cursor"] = 0
        self.detached = False

    async def poll(self):
        await self.touch()
        # Read status before logs: when exit is observed, the subsequent log
        # read includes the final chunk rather than dropping it in a race.
        status = await self.commands.get_command_status(self.execution_id)
        logs = await self.commands.get_background_command_logs(
            self.execution_id, cursor=self.cursor
        )
        if logs.cursor is None:
            raise SandboxError(
                "Background log API returned no cursor; cannot safely read incremental output"
            )
        self.cursor = logs.cursor
        self.metadata["execution_cursor"] = self.cursor
        if status.error and status.exit_code is None:
            raise SandboxError(status.error)
        code = None
        if status.running is False:
            if status.exit_code is None:
                raise SandboxError("Command ended without an exit code")
            code = status.exit_code
            self.finished = True
        # OpenSandbox's background API merges stdout/stderr.
        return logs.content or "", status.error or "", code

    async def interrupt(self):
        await self.commands.interrupt(self.execution_id)

    async def close(self):
        if not self.finished and not self.detached:
            await self.interrupt()


class CubeHandle(CloudHandle):
    def __init__(self, provider, req):
        super().__init__(provider, req)
        self.command = None
        self.waiter = None
        self.sandbox = None
        self.pending = ["", ""]
        self.output_size = 0

    def _append(self, index, data):
        self.pending[index] += data
        self.output_size += len(data)

    def stdout(self, data):
        self._append(0, data)

    def stderr(self, data):
        self._append(1, data)

    async def poll(self):
        await self.touch()
        # The SDK retains its transcript too; bound the total before it can
        # accumulate an unbounded stream inside the SDK.
        if self.output_size > 64 * 1024 * 1024:
            raise SandboxError("Command output exceeded 64 MiB; command stopped")
        out, err = self.pending
        self.pending = ["", ""]
        code = None
        if self.waiter.done():
            try:
                result = self.waiter.result()
                code = result.exit_code
            except Exception as exc:
                from e2b.sandbox.commands.command_handle import CommandExitException

                if not isinstance(exc, CommandExitException):
                    raise
                code = exc.exit_code
            self.finished = True
            from core.sandbox.credential_dispatch import persist_credentials

            await persist_credentials(
                self.provider, self.sandbox, self.req.user_id, self.req.script_content
            )
        return out, err, code

    async def interrupt(self):
        if self.command:
            await self.command.kill()

    async def close(self):
        if not self.finished:
            await self.interrupt()
        if self.waiter:
            if not self.waiter.done():
                self.waiter.cancel()
            await asyncio.gather(self.waiter, return_exceptions=True)


def process_service(provider):
    service = getattr(provider, "_command_processes", None)
    if service is None:
        service = CloudProcesses(provider)
        provider._command_processes = service
    return service


async def start_on_service_loop(provider, req, yield_time_ms):
    loop = getattr(provider, "_service_loop", None)

    async def start():
        return await process_service(provider).start(req, yield_time_ms)

    if loop is not None and not loop.is_closed() and loop is not asyncio.get_running_loop():
        from core.sandbox.processes.dispatch import _forward_to_loop

        return await _forward_to_loop(loop, start())
    return await start()
