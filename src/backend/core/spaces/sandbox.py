"""Check account file grants before using a sandbox."""
import asyncio

from fastapi import HTTPException
from core.sandbox.processes.dispatch import _on_service_loop
from core.spaces.access import resolve_grants
from core.spaces.mounts import MOUNT_REVISION_TAG, mount_revision


def current_revision(actor):
    from core.db.engine import SessionLocal
    with SessionLocal() as db:
        return mount_revision(resolve_grants(db, actor))


class SpaceSandboxMixin:
    from core.spaces.edition_access import workspace_mount_names

    def _session_entrypoint(self, user_id):
        from core.spaces.edition_access import session_entrypoint
        return session_entrypoint(super()._session_entrypoint(user_id), user_id)

    async def _create_jupyter_sandbox(self, *, from_snapshot_id=None, user_id=None):
        from core.spaces.leases import async_space_lock
        if not user_id:
            return await super()._create_jupyter_sandbox(from_snapshot_id=from_snapshot_id)
        async with async_space_lock(user_id):
            return await super()._create_jupyter_sandbox(
                from_snapshot_id=from_snapshot_id, user_id=user_id,
            )

    async def _grants_match(self, session):
        revision = await asyncio.to_thread(current_revision, session.user_id)
        info = await session.sandbox.get_info()
        if (info.metadata or {}).get(MOUNT_REVISION_TAG) != revision:
            return False
        from core.config.settings import settings
        from core.spaces.recovery import ensure_mounts
        await ensure_mounts(session.sandbox, session.user_id, info,
            domain=settings.sandbox.opensandbox_domain,
            api_key=settings.sandbox.opensandbox_api_key)
        return True

    async def _resolve_bound_session(self, session_id, user_id=None):
        if not user_id:
            from core.infra.logging import user_id_var
            user_id = user_id_var.get() or None
        session = await super()._resolve_bound_session(session_id, user_id)
        if user_id and not await self._grants_match(session):
            await self._destroy_session(session_id, session, reuse_sandbox=False)
            raise HTTPException(409, "文件权限已变更，请重新打开沙盒")
        return session

    @_on_service_loop
    async def ensure_user_workspace(self, session_id, user_id):
        await self._get_or_create_session(session_id, user_id)

    @_on_service_loop
    async def write_stdin(
        self, session_id, *, sandbox_session_id, user_id=None, chars="", yield_time_ms=60000
    ):
        await self._get_or_create_session(sandbox_session_id, user_id)
        return await super().write_stdin(
            session_id, sandbox_session_id=sandbox_session_id, user_id=user_id,
            chars=chars, yield_time_ms=yield_time_ms,
        )

    async def _renew_in_background(self, session_id, session):
        if session.user_id and not await self._grants_match(session):
            await self._destroy_session(session_id, session, reuse_sandbox=False)
            return
        await super()._renew_in_background(session_id, session)
