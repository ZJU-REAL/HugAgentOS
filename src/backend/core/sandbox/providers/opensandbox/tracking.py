"""Shared process tracking and reconciliation, independent of snapshot enablement."""
from __future__ import annotations

from core.config.settings import settings

import uuid

import core.sandbox.sessions.registry as session_registry
from core.sandbox.errors import SandboxNotFoundError
from core.sandbox.providers.opensandbox.runtime import logger


class _OpenSandboxTrackingMixin:
    def _tracking_owner_token(self):
        if not hasattr(self, "_tracking_token"):
            self._tracking_token = uuid.uuid4().hex
            self._tracking_ids = set()
        return self._tracking_token

    async def _track_sandbox(self, sandbox_id):
        token = self._tracking_owner_token()
        if not await session_registry.track(sandbox_id, token):
            return False
        self._tracking_ids.add(str(sandbox_id))
        return True

    async def _refresh_tracking(self):
        token = self._tracking_owner_token()
        live = set()
        for chat, sess in list(self._sessions.items()):
            if sess.pool_source != "attached" and await session_registry.lookup(chat) == str(sess.sandbox.id):
                live.add(str(sess.sandbox.id))
        for _, sbx in self._pool.idle_sandboxes():
            live.add(str(sbx.id))
        for _, sbx, _ in self._jupyter_user_pool.idle_sandboxes():
            live.add(str(sbx.id))
        for sandbox_id in self._tracking_ids - live:
            await session_registry.untrack(sandbox_id, token)
        self._tracking_ids.intersection_update(live)
        for sandbox_id in live:
            await self._track_sandbox(sandbox_id)

    async def _reconcile_sessions(self):
        """Drop only local reservations when another worker retired the binding."""
        # Shared claims below can wait longer than the tracker TTL. Refresh
        # the independent idle pool leases before entering those waits.
        await self._refresh_tracking()
        count = 0
        for chat, sess in list(self._sessions.items()):
            lock = await self._get_session_lock(chat)
            if lock.locked():
                continue
            try:
                async with lock, session_registry.claim(chat):
                    if self._sessions.get(chat) is not sess:
                        continue
                    bound = await session_registry.lookup(chat)
                    if bound != str(sess.sandbox.id):
                        await self._release_session(chat, sess, detach_only=True)
                        count += 1
                    elif not settings.sandbox.opensandbox_snapshot_enabled:
                        # No snapshot worker runs in this mode. A metadata GET
                        # can confirm expiry without ever deleting a live session.
                        try:
                            await self._confirm_sandbox_exists(str(sess.sandbox.id))
                        except SandboxNotFoundError:
                            await self._destroy_session(chat, sess)
                            count += 1
            except Exception as exc:
                logger.warning("[opensandbox] reconcile session=%s failed: %s", chat, exc)
        await self._refresh_tracking()
        return count

    async def _confirm_sandbox_exists(self, sandbox_id):
        from core.sandbox.errors import SandboxError
        response = await self._get_http_client().get(
            f"{self._domain.rstrip('/')}/v1/sandboxes/{sandbox_id}"
        )
        if response.status_code == 404:
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if code == "DOCKER::SANDBOX_NOT_FOUND":
                raise SandboxNotFoundError(f"sandbox {sandbox_id} no longer exists")
        if response.status_code >= 400:
            raise SandboxError(f"sandbox metadata query failed: HTTP {response.status_code}")


    async def _discover_idle_sessions(self):
        """Maintenance owns shared bindings, regardless of which worker served them."""
        import time
        from core.sandbox.providers.opensandbox.volumes import _METADATA_USER_ID_KEY
        for chat, sandbox_id, active_at in await session_registry.bindings():
            if chat in self._sessions or time.time() - active_at < settings.sandbox.idle_ttl_s:
                continue
            lock = await self._get_session_lock(chat)
            if lock.locked():
                continue
            try:
                async with lock, session_registry.claim(chat):
                    if await session_registry.lookup(chat) != sandbox_id:
                        continue
                    response = await self._get_http_client().get(
                        f"{self._domain.rstrip('/')}/v1/sandboxes/{sandbox_id}"
                    )
                    if response.status_code == 404:
                        if response.json().get("code") == "DOCKER::SANDBOX_NOT_FOUND":
                            await session_registry.forget(chat, sandbox_id)
                            continue
                    response.raise_for_status()
                    user_id = (response.json().get("metadata") or {}).get(_METADATA_USER_ID_KEY)
                    session = await self._attach_session(sandbox_id, user_id)
                    if session is not None:
                        session.last_active_ts = time.monotonic() - max(0, time.time() - active_at)
                        self._sessions[chat] = session
            except Exception as exc:
                logger.warning("sandbox discovery deferred chat=%s: %s", chat, exc)
