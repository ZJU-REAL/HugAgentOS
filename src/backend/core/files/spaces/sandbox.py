"""Check account file grants before using a sandbox."""
import asyncio

from fastapi import HTTPException
from core.sandbox.processes.dispatch import _on_service_loop
from core.files.spaces.mounts import MOUNT_REVISION_TAG, authorized_grants


class SpaceSandboxMixin:
    from core.files.edition import workspace_mount_names
    from core.files.spaces.transport.sync.sandbox_io import STATE_DIR_NAME
    # A synchronized space copy keeps its baseline across a reset for reuse.
    workspace_mount_names = (*workspace_mount_names, STATE_DIR_NAME)
    del STATE_DIR_NAME

    def _session_entrypoint(self, user_id):
        from core.files.edition import session_entrypoint
        return session_entrypoint(super()._session_entrypoint(user_id), user_id)

    async def _grants_match(self, session, *, newly_bound):
        """Check the sandbox against current grants before every use.

        A newly bound sandbox may have been created during a permission change, so
        its check waits for that change to commit (``authorized_grants``)."""
        grants, revision = await asyncio.to_thread(
            authorized_grants, session.user_id, ordered=newly_bound)
        info = await session.sandbox.get_info()
        if (info.metadata or {}).get(MOUNT_REVISION_TAG) != revision:
            return False
        from core.config.settings import settings
        from core.files.spaces.recovery import ensure_mounts
        await ensure_mounts(session.sandbox, session.user_id, info, grants,
            domain=settings.sandbox.opensandbox_domain,
            api_key=settings.sandbox.opensandbox_api_key)
        return True

    async def _resolve_bound_session(self, session_id, user_id=None):
        if not user_id:
            from core.infra.logging import user_id_var
            user_id = user_id_var.get() or None
        previous = self._sessions.get(session_id)
        session = await super()._resolve_bound_session(session_id, user_id)
        if user_id and not await self._grants_match(session, newly_bound=session is not previous):
            await self._destroy_session(session_id, session, reuse_sandbox=False)
            raise HTTPException(409, "文件权限已变更，请重新打开沙盒")
        return session

    @_on_service_loop
    async def ensure_user_workspace(self, session_id, user_id):
        await self._get_or_create_session(session_id, user_id)

    async def _renew_in_background(self, session_id, session):
        if session.user_id and (await asyncio.to_thread(
            authorized_grants, session.user_id, ordered=False))[1] != (
            (await session.sandbox.get_info()).metadata or {}
        ).get(MOUNT_REVISION_TAG):
            await self._destroy_session(session_id, session, reuse_sandbox=False)
            return
        from core.files.spaces.transport import get_space_transport
        # Files written after a command returned (background processes).
        await get_space_transport().flush(session.sandbox, session.user_id)
        await super()._renew_in_background(session_id, session)
