"""Route reserved evaluation sessions without altering normal provider lifecycle."""
from .errors import SandboxError


class SessionSandboxRouter:
    def __init__(self, ordinary):
        self._ordinary = ordinary

    def __getattr__(self, name):
        return getattr(self._ordinary, name)

    def _for(self, session_id):
        if str(session_id or "").startswith("eval_"):
            from core.config.settings import settings
            from .evaluation_provider import get_evaluation_provider
            if settings.sandbox.provider != "opensandbox":
                raise SandboxError("Evaluation requires OpenSandbox; fallback is forbidden")
            return get_evaluation_provider()
        return self._ordinary

    async def _authorized_for(self, session_id, user_id):
        provider = self._for(session_id)
        if str(session_id or "").startswith("eval_"):
            return provider
        if getattr(provider, "name", "") in ("script_runner", "cube"):
            import asyncio
            from core.services.edition_workspace import resolve_workspaces
            workspaces = await asyncio.to_thread(resolve_workspaces, user_id)
            if workspaces:
                raise SandboxError("当前沙盒服务不支持额外空间的权限挂载，请使用 OpenSandbox")
        return provider

    async def _ready(self, provider, session_id, user_id):
        if str(session_id or "").startswith("eval_"):
            return
        if provider.name != "opensandbox" and user_id and session_id:
            from core.plugins.resources.prewarm import schedule
            sandbox_id = await provider.current_sandbox_id(session_id)
            schedule(provider, session_id, user_id, sandbox_id)

    async def ensure_user_workspace(self, session_id, user_id):
        provider = await self._authorized_for(session_id, user_id)
        await provider.ensure_user_workspace(session_id, user_id)
        await self._ready(provider, session_id, user_id)

    async def run_to_completion(self, req):
        provider = await self._authorized_for(req.session_id, req.user_id)
        result = await provider.run_to_completion(req)
        await self._ready(provider, req.session_id, req.user_id)
        return result

    async def start_process(self, req, yield_time_ms=60000):
        provider = await self._authorized_for(req.session_id, req.user_id)
        result = await provider.start_process(req, yield_time_ms=yield_time_ms)
        await self._ready(provider, req.session_id, req.user_id)
        return result

    async def write_stdin(self, session_id, *, sandbox_session_id, user_id=None, chars="", yield_time_ms=60000):
        provider = await self._authorized_for(sandbox_session_id, user_id)
        return await provider.write_stdin(
            session_id, sandbox_session_id=sandbox_session_id, user_id=user_id,
            chars=chars, yield_time_ms=yield_time_ms,
        )

    async def put_file(self, session_id, path, content, user_id=None):
        provider = await self._authorized_for(session_id, user_id)
        result = await provider.put_file(session_id, path, content, user_id=user_id)
        await self._ready(provider, session_id, user_id)
        return result

    async def get_file(self, session_id, path, user_id=None):
        provider = await self._authorized_for(session_id, user_id)
        result = await provider.get_file(session_id, path, user_id=user_id)
        await self._ready(provider, session_id, user_id)
        return result

    async def get_file_to_path(self, session_id, path, destination, *, max_bytes, user_id=None):
        provider = await self._authorized_for(session_id, user_id)
        result = await provider.get_file_to_path(
            session_id, path, destination, max_bytes=max_bytes, user_id=user_id,
        )
        await self._ready(provider, session_id, user_id)
        return result

    async def current_sandbox_id(self, session_id):
        return await self._for(session_id).current_sandbox_id(session_id)

    async def close_session(self, session_id):
        provider = self._for(session_id)
        if not session_id or str(session_id).startswith("eval_") or provider.name == "opensandbox":
            return await provider.close_session(session_id)
        from core.plugins.resources.prewarm import closing, stop
        async with closing(session_id):
            await stop(session_id)
            return await provider.close_session(session_id)

    async def touch_session(self, session_id):
        return await self._for(session_id).touch_session(session_id)
