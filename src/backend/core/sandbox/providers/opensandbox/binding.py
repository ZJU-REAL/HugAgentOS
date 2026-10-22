"""Resolve and use the shared session binding under one distributed claim."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

import core.sandbox.sessions.registry as session_registry
from core.sandbox.sessions.binding import require_available


class _OpenSandboxBindingMixin:
    @asynccontextmanager
    async def _session_use(self, session_id, user_id=None):
        lock = await self._get_session_lock(session_id)
        async with lock, session_registry.claim(session_id):
            sess = await self._resolve_bound_session(session_id, user_id)
            yield sess
            # The claim still protects the same binding after a long transfer.
            if session_registry.claim_valid():
                sess.last_active_ts = time.monotonic()
                await session_registry.remember(session_id, str(sess.sandbox.id))

    async def _get_or_create_session(self, session_id, user_id=None):
        async with self._session_use(session_id, user_id) as sess:
            return sess

    async def _resolve_bound_session(self, session_id, user_id):
        bound = await session_registry.lookup(session_id)
        sess = self._sessions.get(session_id)
        if sess is not None and str(sess.sandbox.id) != bound:
            await self._release_session(session_id, sess, detach_only=True)
            sess = None
        require_available(bound, stale=bool(sess and sess.stale_marked))
        newly_bound = sess is None or sess.stale_marked
        if sess is not None and sess.stale_marked:
            user_id = user_id or sess.user_id
            await self._destroy_session(session_id, sess)
            sess, bound = None, None
        if sess is None:
            if not user_id:
                from core.infra.logging import user_id_var
                user_id = user_id_var.get() or None
            sess = await self._attach_session(bound, user_id) if bound else None
            if sess is None:
                sess = await self._create_session_for(session_id, user_id=user_id)
            self._sessions[session_id] = sess
        sess.last_active_ts = time.monotonic()
        # Creation and registration must remain in the same claim.
        await session_registry.remember(session_id, str(sess.sandbox.id))
        self._maybe_schedule_renew(session_id, sess)
        if newly_bound:
            from core.capabilities.plugins.resources.prewarm import schedule
            schedule(self, session_id, sess.user_id, str(sess.sandbox.id))
        return sess

    async def touch_session(self, session_id):
        lock = await self._get_session_lock(session_id)
        async with lock, session_registry.claim(session_id):
            sess = self._sessions.get(session_id)
            if sess is None:
                return False
            if await session_registry.lookup(session_id) != str(sess.sandbox.id):
                await self._release_session(session_id, sess, detach_only=True)
                return False
            if sess.stale_marked:
                return False
            sess.last_active_ts = time.monotonic()
            await session_registry.remember(session_id, str(sess.sandbox.id))
            self._maybe_schedule_renew(session_id, sess)
            return True

    async def _renew_bound_session(self, session_id, sess):
        lock = await self._get_session_lock(session_id)
        async with lock, session_registry.claim(session_id):
            if self._sessions.get(session_id) is not sess:
                return
            if await session_registry.lookup(session_id) != str(sess.sandbox.id):
                await self._release_session(session_id, sess, detach_only=True)
                return
            await self._renew_in_background(session_id, sess)
