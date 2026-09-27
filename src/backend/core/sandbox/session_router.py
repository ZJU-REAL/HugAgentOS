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

    async def run_to_completion(self, req):
        return await self._for(req.session_id).run_to_completion(req)

    async def start_process(self, req, yield_time_ms=60000):
        return await self._for(req.session_id).start_process(req, yield_time_ms=yield_time_ms)

    async def write_stdin(self, session_id, *, sandbox_session_id, user_id=None, chars="", yield_time_ms=60000):
        return await self._for(sandbox_session_id).write_stdin(
            session_id, sandbox_session_id=sandbox_session_id, user_id=user_id,
            chars=chars, yield_time_ms=yield_time_ms,
        )

    async def put_file(self, session_id, path, content, user_id=None):
        return await self._for(session_id).put_file(session_id, path, content, user_id=user_id)

    async def get_file(self, session_id, path, user_id=None):
        return await self._for(session_id).get_file(session_id, path, user_id=user_id)

    async def get_file_to_path(self, session_id, path, destination, *, max_bytes, user_id=None):
        return await self._for(session_id).get_file_to_path(
            session_id, path, destination, max_bytes=max_bytes, user_id=user_id,
        )

    async def current_sandbox_id(self, session_id):
        return await self._for(session_id).current_sandbox_id(session_id)

    async def close_session(self, session_id):
        return await self._for(session_id).close_session(session_id)

    async def touch_session(self, session_id):
        return await self._for(session_id).touch_session(session_id)
