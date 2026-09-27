"""Connect-only native tools for isolated, externally managed evaluation boxes."""
from __future__ import annotations

import asyncio
import base64
import json
import posixpath
import shlex
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from . import evaluation_binding as binding
from .errors import SandboxConnectError, SandboxError
from .protocol import ProcessResult, SandboxAdminCapabilities


async def connect_sandbox(sandbox_id):
    from core.config.settings import settings
    from opensandbox import Sandbox
    from opensandbox.config import ConnectionConfig

    config = settings.sandbox
    return await Sandbox.connect(
        sandbox_id,
        connection_config=ConnectionConfig(
            domain=config.opensandbox_domain,
            api_key=config.opensandbox_api_key or None,
            use_server_proxy=True,
            request_timeout=timedelta(seconds=config.opensandbox_request_timeout_s),
        ),
        skip_health_check=True,
    )


def _owner(user_id=None):
    from core.infra.logging import user_id_var
    value = user_id or user_id_var.get()
    if not value:
        raise SandboxError("Evaluation operation requires its authenticated owner")
    return value


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SandboxError("Invalid evaluation file path")
    return posixpath.normpath(value if value.startswith("/") else "/workspace/" + value)


class EvaluationSandboxProvider:
    name = "opensandbox"
    runs_on_host = False
    myspace_mirror_live = False

    def __init__(self, *, connector=None):
        self._connector = connector or connect_sandbox
        self._connections = {}
        self._cache_lock = threading.Lock()

    async def _connect(self, sandbox_id):
        # SDK clients belong to their creating event loop; children use separate
        # loops. Never reuse a client across loops or initialize ordinary pools.
        loop = asyncio.get_running_loop()
        key = (loop, sandbox_id)
        with self._cache_lock:
            task = self._connections.get(key)
            if task is None:
                task = loop.create_task(self._connector(sandbox_id))
                self._connections[key] = task
        try:
            return await asyncio.shield(task)
        except Exception:
            with self._cache_lock:
                if self._connections.get(key) is task:
                    self._connections.pop(key, None)
            raise SandboxConnectError("Evaluation sandbox attach failed; replacement is forbidden") from None

    @asynccontextmanager
    async def _access(self, session_id, user_id, *, resource=None):
        owner = _owner(user_id)
        async with binding.operation(session_id, owner, resource=resource) as item:
            try:
                yield item, await self._connect(item.sandbox_id)
            except SandboxError:
                raise
            except Exception as exc:
                raise SandboxError("Evaluation operation failed: " + type(exc).__name__) from None

    async def start_process(self, req, yield_time_ms=60000):
        from opensandbox.models.execd import RunCommandOpts

        owner = _owner(req.user_id)
        languages = {"bash": "bash", "python": "python3 -u", "javascript": "node"}
        if req.language not in languages or (req.timeout is not None and req.timeout <= 0):
            raise SandboxError("Unsupported evaluation command or timeout")
        handle = uuid.uuid4().hex
        async with self._access(req.session_id, owner) as (item, sandbox):
            for files, encoded in ((req.resource_files, False), (req.input_files, False),
                                   (req.input_files_b64, True)):
                for path, content in (files or {}).items():
                    await sandbox.files.write_file(_path(path), base64.b64decode(content) if encoded else content)
            args = dict(req.params or {})
            argv = args.pop("_args", [])
            if not isinstance(argv, list):
                raise SandboxError("Evaluation command arguments must be a list")
            suffix = {"bash": ".sh", "python": ".py", "javascript": ".js"}[req.language]
            path = shlex.quote("/workspace/.__eval_" + handle + suffix)
            invocation = languages[req.language] + " " + path
            invocation += "".join(" " + shlex.quote(str(value)) for value in argv)
            wrapper = ("trap " + shlex.quote("rm -f -- " + path) + " EXIT; printf %s "
                       + shlex.quote(req.script_content) + " > " + path + " && " + invocation)
            command = "printf %s " + shlex.quote(json.dumps(args)) + " | bash -c " + shlex.quote(wrapper)
            duration = max(0.001, item.expires_at - time.time())
            if req.timeout is not None:
                duration = min(duration, req.timeout)
            opts = RunCommandOpts(background=True, working_directory="/workspace", uid=0, gid=0,
                                  timeout=timedelta(seconds=duration))
            try:
                execution = await sandbox.commands.run(command, opts=opts)
                if not execution.id:
                    raise SandboxError("Evaluation command returned no ID")
                await binding.record_command(req.session_id, owner, handle, {
                    "id": execution.id, "cursor": 0, "started_at": time.time(),
                })
            except BaseException:
                # An uncertain launch cannot be certified frozen or retried.
                await asyncio.shield(binding.uncertain(req.session_id, owner))
                raise
        return await self.write_stdin(handle, sandbox_session_id=req.session_id,
                                      user_id=owner, yield_time_ms=yield_time_ms)

    async def write_stdin(self, session_id, *, sandbox_session_id, user_id=None,
                          chars="", yield_time_ms=60000):
        if chars not in ("", "\x03"):
            raise SandboxError("Evaluation commands accept polling or Ctrl+C only")
        owner = _owner(user_id)
        async with self._access(sandbox_session_id, owner, resource="command:" + session_id) as (item, sandbox):
            command = item.commands.get(session_id)
            if command is None:
                raise SandboxError("Unknown evaluation command for this session")
            if chars:
                await sandbox.commands.interrupt(command["id"])
            deadline = time.monotonic() + max(0, min(yield_time_ms, 60000)) / 1000
            while True:
                status = await sandbox.commands.get_command_status(command["id"])
                if status.running is False or time.monotonic() >= deadline:
                    break
                # Stop polling promptly when another worker begins freezing.
                if (await binding.get(sandbox_session_id)).phase != "active":
                    break
                await asyncio.sleep(0.1)
            logs = await sandbox.commands.get_background_command_logs(command["id"], cursor=command["cursor"])
            if (type(status.running) is not bool or logs.cursor is None
                    or (status.running is False and status.exit_code is None)):
                raise SandboxError("Evaluation command status is incomplete")
            await binding.record_command(sandbox_session_id, owner, session_id,
                                         {**command, "cursor": logs.cursor,
                                          "finished": status.running is False})
            return {"session_id": session_id if status.running else None,
                    "status": "running" if status.running else "exited",
                    "stdout": logs.content or "", "stderr": status.error or "",
                    "exit_code": status.exit_code,
                    "execution_time_ms": int((time.time() - command["started_at"]) * 1000),
                    "stdin_supported": False}

    async def run_to_completion(self, req):
        result = await self.start_process(req)
        output, errors = [result["stdout"]], [result["stderr"]]
        size = len(result["stdout"]) + len(result["stderr"])
        while result["session_id"]:
            if size > 16 * 1024 * 1024:
                await self.write_stdin(result["session_id"], sandbox_session_id=req.session_id,
                                       user_id=req.user_id, chars="\x03", yield_time_ms=0)
                raise SandboxError("Evaluation command output exceeded 16 MiB")
            result = await self.write_stdin(result["session_id"], sandbox_session_id=req.session_id,
                                           user_id=req.user_id)
            output.append(result["stdout"])
            errors.append(result["stderr"])
            size += len(result["stdout"]) + len(result["stderr"])
        return ProcessResult("".join(output), "".join(errors), result["exit_code"], result["execution_time_ms"])

    async def put_file(self, session_id, path, content, user_id=None):
        async with self._access(session_id, user_id) as (_, sandbox):
            await sandbox.files.write_file(_path(path), content)

    async def get_file(self, session_id, path, user_id=None):
        async with self._access(session_id, user_id) as (_, sandbox):
            content = await sandbox.files.read_bytes(_path(path))
            return content.encode() if isinstance(content, str) else bytes(content)

    async def get_file_to_path(self, session_id, path, destination, *, max_bytes, user_id=None):
        from ._common import stream_to_file
        async with self._access(session_id, user_id) as (_, sandbox):
            chunks = await sandbox.files.read_bytes_stream(_path(path), chunk_size=1024 * 1024)
            try:
                return await stream_to_file(chunks, destination, max_bytes=max_bytes)
            finally:
                await chunks.aclose()

    async def stage_files(self, user_id, files):
        raise SandboxError("Evaluation sandboxes do not stage personal-space files")

    async def current_sandbox_id(self, session_id):
        return (await binding.get(session_id)).sandbox_id

    async def touch_session(self, session_id):
        try:
            return (await binding.get(session_id)).phase in {"ready", "active"}
        except SandboxError:
            return False

    async def freeze(self, session_id, owner_user_id, *, timeout=65):
        try:
            async with asyncio.timeout(timeout):
                item = await binding.begin_freeze(session_id, owner_user_id)
                if item.phase == "frozen":
                    return item
                item = await binding.wait_idle(session_id, owner_user_id, timeout=timeout)
                if item.uncertain:
                    raise SandboxError("Uncertain command launch; destroy the evaluation sandbox instead of grading")
                sandbox = await self._connect(item.sandbox_id)
                for command in item.commands.values():
                    # Only a SDK-confirmed terminal state is cached; detached
                    # descendants still pass through the process sweep below.
                    if command.get("finished") is True:
                        continue
                    status = await sandbox.commands.get_command_status(command["id"])
                    if status.running:
                        await sandbox.commands.interrupt(command["id"])
                        while True:
                            status = await sandbox.commands.get_command_status(command["id"])
                            if status.running is False:
                                break
                            await asyncio.sleep(0.1)
                    if status.running is not False:
                        raise SandboxError("Evaluation command stop is unconfirmed")
                from .evaluation_process_guard import clear_processes
                await clear_processes(sandbox, item.protected_processes)
                return await binding.finish_freeze(session_id, owner_user_id)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError("Evaluation freeze failed: " + type(exc).__name__) from None

    async def close_session(self, session_id):
        # The management API owns remote destruction; never return this box to
        # ordinary pools or snapshot it. Close invalidates all agent operations.
        item = await binding.get(session_id, allow_expired=True)
        await binding.close(session_id, item.owner_user_id)
        current = asyncio.get_running_loop()
        with self._cache_lock:
            cached = [(key, task) for key, task in self._connections.items() if key[1] == item.sandbox_id]
            for key, _ in cached:
                self._connections.pop(key, None)
        for (loop, _), task in cached:
            async def release(connection=task):
                sandbox = await asyncio.shield(connection)
                await sandbox.close()
            try:
                if loop is current:
                    await release()
                elif loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(release(), loop)
                    await asyncio.wrap_future(future)
                elif task.done() and not task.cancelled() and task.exception() is None:
                    await task.result().close()
            except Exception:
                pass

    async def health(self):
        return True

    def admin_capabilities(self):
        return SandboxAdminCapabilities(provider="evaluation")


_PROVIDER = EvaluationSandboxProvider()


def get_evaluation_provider():
    return _PROVIDER
