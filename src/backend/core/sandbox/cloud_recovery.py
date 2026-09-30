"""Internal durable references for job-owned OpenSandbox commands.

References stay in the job database and never enter model-facing tool responses.
The job row grants ownership; the process service separately checks user/session.
"""

import asyncio

from .cloud_processes import OpenSandboxHandle, process_service
from .errors import SandboxError


async def _provider(provider, req):
    from .session_router import SessionSandboxRouter

    if isinstance(provider, SessionSandboxRouter):
        return await provider._authorized_for(req.session_id, req.user_id)
    return provider


async def _dispatch(provider, action):
    loop = getattr(provider, "_service_loop", None)
    if loop is not None and loop is not asyncio.get_running_loop():
        if not loop.is_running():
            raise SandboxError("Sandbox service loop is not running")
        from .opensandbox_provider import _forward_to_loop

        return await _forward_to_loop(loop, action())
    return await action()


async def execution_reference(provider, process_id, req):
    provider = await _provider(provider, req)
    if provider.name != "opensandbox" or not process_id:
        return None

    async def describe():
        service = process_service(provider)
        entry = service.sessions.entries.get(process_id)
        if entry is None or entry.owner != (req.session_id, req.user_id):
            raise SandboxError("Cannot describe another owner's command")
        handle = entry.handle
        if handle is None:
            return None
        return {
            "provider": "opensandbox",
            "execution_id": handle.execution_id,
            "sandbox_id": await provider.current_sandbox_id(req.session_id),
            "session_id": req.session_id,
            "user_id": req.user_id,
        }

    return await _dispatch(provider, describe)


async def recover_execution(provider, reference, req):
    provider = await _provider(provider, req)
    if provider.name != "opensandbox" or reference.get("provider") != "opensandbox":
        raise SandboxError("This provider cannot reconcile a command after service restart")
    if (reference.get("session_id"), reference.get("user_id")) != (req.session_id, req.user_id):
        raise SandboxError("Execution reference owner mismatch")

    async def recover():
        service = process_service(provider)
        current_id = await provider.current_sandbox_id(req.session_id)
        if current_id != reference.get("sandbox_id"):
            raise SandboxError("Original sandbox binding is unavailable; command state is unknown")
        sess = await provider._get_or_create_session(req.session_id, user_id=req.user_id)
        if str(sess.sandbox.id) != reference["sandbox_id"]:
            raise SandboxError("Sandbox was replaced; refusing to adopt an unrelated command")

        async def factory():
            return OpenSandboxHandle(
                provider, req, sess.sandbox.commands, reference["execution_id"]
            )

        return await service.sessions.start(
            factory, (req.session_id, req.user_id), yield_time_ms=250, timeout=req.timeout
        )

    return await _dispatch(provider, recover)


async def release_execution(provider, process_id, req):
    """Retire only this worker's poller after a job lease moves to another worker."""
    provider = await _provider(provider, req)
    if provider.name != "opensandbox" or not process_id:
        return

    async def release():
        service = process_service(provider)
        entry = service.sessions.entries.get(process_id)
        if entry is None or entry.owner != (req.session_id, req.user_id):
            return
        if entry.handle is not None:
            # Service shutdown normally kills unfinished commands. A handed-off
            # command belongs to the new worker and must survive that cleanup.
            entry.handle.detached = True
        if entry.task and not entry.task.done():
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)

    await _dispatch(provider, release)
