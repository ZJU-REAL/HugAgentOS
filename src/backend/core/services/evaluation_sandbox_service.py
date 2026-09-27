"""Evaluation-owned sandboxes; ordinary chat code never receives control credentials."""
import asyncio
import shlex
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import PurePosixPath

from core.config.settings import settings
from core.sandbox.errors import SandboxError

MAX_TRANSFER = 64 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


def connection_config():
    from opensandbox.config import ConnectionConfig
    return ConnectionConfig(
        domain=settings.sandbox.opensandbox_domain,
        api_key=settings.sandbox.opensandbox_api_key or None,
        use_server_proxy=True,
        request_timeout=timedelta(seconds=60),
    )


def session_id(lease_id: str) -> str:
    import re
    value = lease_id if lease_id.startswith("eval_") else "eval_" + lease_id
    if not re.fullmatch(r"eval_[0-9a-f]{32}", value):
        raise SandboxError("invalid_evaluation_lease")
    return value


async def _owned(lease_id, owner, *, allow_expired=False):
    from core.sandbox import evaluation_binding as bindings
    return await bindings.assert_owner(session_id(lease_id), owner, allow_expired=allow_expired)


@asynccontextmanager
async def _connection(lease_id, owner, *, allow_expired=False, allow_closed=False):
    from opensandbox import Sandbox
    binding = await _owned(lease_id, owner, allow_expired=allow_expired)
    if binding.phase == "closed" and not allow_closed:
        raise SandboxError("evaluation_closed")
    sandbox = await Sandbox.connect(
        binding.sandbox_id, connection_config=connection_config(), skip_health_check=True
    )
    try:
        yield sandbox
    finally:
        await sandbox.close()


def _result(execution):
    stdout = "".join(item.text for item in execution.logs.stdout)
    stderr = "".join(item.text for item in execution.logs.stderr)
    code = execution.exit_code
    if code is None:
        raise SandboxError("evaluation_command_missing_exit_status")
    return {
        "exit_code": int(code), "stdout": stdout[-MAX_OUTPUT:], "stderr": stderr[-MAX_OUTPUT:],
        "truncated": len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT,
    }


async def create(owner, *, image, attempt_id="", ttl_seconds=7200, cpu_count=2, memory_mb=4096):
    from opensandbox import Sandbox
    from opensandbox.models.execd import RunCommandOpts
    from core.sandbox import evaluation_binding as bindings
    from core.sandbox.evaluation_process_guard import capture_baseline
    if settings.sandbox.provider != "opensandbox":
        raise SandboxError("opensandbox_required")
    sandbox = await Sandbox.create(
        image, connection_config=connection_config(),
        timeout=timedelta(seconds=ttl_seconds),
        resource={"cpu": str(cpu_count), "memory": f"{memory_mb}Mi"},
        entrypoint=["bash", "-lc", "exec sleep infinity"],
        metadata={"hugagent-evaluation": "true", "attempt-id": attempt_id},
        volumes=None, env=None, skip_health_check=True,
    )
    try:
        # All native tools use /workspace; Ageval uses /attempt/workspace.
        # Do not overwrite a task image's nonempty, distinct workspace.
        setup = (
            "set -eu; mkdir -p /attempt/workspace /attempt/home /attempt/artifacts; "
            "if [ -d /workspace ] && [ ! -L /workspace ]; then rmdir /workspace; fi; "
            "ln -sfn /attempt/workspace /workspace"
        )
        result = await sandbox.commands.run(
            setup, opts=RunCommandOpts(uid=0, gid=0, timeout=timedelta(seconds=30))
        )
        if _result(result)["exit_code"]:
            raise SandboxError("evaluation_workspace_conflict")
        baseline = await capture_baseline(sandbox)
        binding = await bindings.create(
            owner, sandbox.id, ttl_seconds, protected_processes=baseline
        )
        return {
            "lease_id": binding.session_id.removeprefix("eval_"),
            "chat_id": binding.session_id, "sandbox_id": sandbox.id,
            "workdir": "/attempt/workspace",
        }
    except BaseException:
        await sandbox.kill()
        raise
    finally:
        await sandbox.close()


async def execute(lease_id, owner, *, argv, cwd="/attempt/workspace", env=None, timeout_seconds=60, user=None):
    from opensandbox.models.execd import RunCommandOpts
    if user not in (None, "root", "0"):
        raise SandboxError("evaluation_user_unsupported")
    if not argv or any("\x00" in part for part in argv):
        raise SandboxError("evaluation_command_invalid")
    async with _connection(lease_id, owner) as sandbox:
        execution = await asyncio.wait_for(
            sandbox.commands.run(
                shlex.join(argv),
                opts=RunCommandOpts(
                    working_directory=cwd, envs=dict(env or {}), uid=0, gid=0,
                    timeout=timedelta(seconds=timeout_seconds),
                ),
            ),
            timeout=timeout_seconds + 15,
        )
        return _result(execution)


def _path(path):
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts or "\x00" in path:
        raise SandboxError("evaluation_path_invalid")
    return path


async def upload(lease_id, owner, path, content):
    if len(content) > MAX_TRANSFER:
        raise SandboxError("evaluation_transfer_too_large")
    async with _connection(lease_id, owner) as sandbox:
        await sandbox.files.write_file(_path(path), content)


async def download(lease_id, owner, path):
    path = _path(path)
    async with _connection(lease_id, owner) as sandbox:
        chunks = await sandbox.files.read_bytes_stream(path, chunk_size=1024 * 1024)
        result, size = [], 0
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_TRANSFER:
                    raise SandboxError("evaluation_transfer_too_large")
                result.append(chunk)
        finally:
            await chunks.aclose()
        return b"".join(result)


async def freeze(lease_id, owner):
    from core.sandbox import evaluation_binding as bindings
    from core.sandbox.evaluation_provider import get_evaluation_provider
    from core.db.engine import SessionLocal
    from core.db.models import ChatRun
    from orchestration.chat_run_executor import cancel_run
    sid = session_id(lease_id)
    await bindings.assert_owner(sid, owner)
    await bindings.begin_freeze(sid, owner)
    with SessionLocal() as db:
        runs = db.query(ChatRun.run_id).filter(
            ChatRun.chat_id == sid, ChatRun.user_id == owner,
            ChatRun.status.in_(("pending", "running", "needs_attention")),
        ).all()
    for (run_id,) in runs:
        await cancel_run(run_id, user_id=owner)
    await get_evaluation_provider().freeze(sid, owner)
    return {"chat_id": sid, "phase": "frozen"}


async def destroy(lease_id, owner):
    from opensandbox.exceptions import SandboxApiException
    from core.sandbox import evaluation_binding as bindings
    from core.sandbox.evaluation_provider import get_evaluation_provider
    binding = await _owned(lease_id, owner, allow_expired=True)
    if binding.destroyed:
        return {"phase": "closed"}
    async def cleanup():
        try:
            await asyncio.wait_for(freeze(lease_id, owner), timeout=70)
        except (Exception, asyncio.CancelledError):
            # DB/cancel failures cannot prevent destroying an owned instance.
            pass
        async with asyncio.timeout(65):
            try:
                async with _connection(lease_id, owner, allow_expired=True, allow_closed=True) as sandbox:
                    await sandbox.kill()
            except SandboxApiException as exc:
                if exc.status_code != 404:
                    raise
            await bindings.mark_destroyed(binding.session_id, owner)
            await get_evaluation_provider().close_session(binding.session_id)

    task = asyncio.create_task(cleanup())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    return {"phase": "closed"}
