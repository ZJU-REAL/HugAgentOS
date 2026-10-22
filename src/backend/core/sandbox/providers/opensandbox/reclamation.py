"""Session retirement and exactly-once release of local pool reservations."""

from __future__ import annotations

from core.config.settings import settings

import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.accounting import finish_accounting
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session
from core.sandbox.providers.opensandbox.runtime import logger


class _OpenSandboxReclamationMixin:
    async def _protect_queued_snapshots(self, entries) -> None:
        """Renew idle candidates before a bounded batch waits for its next scan.

        This also updates older containers after the backlog grows. Renew errors
        retain the session; only the snapshot boundary can prove disappearance.
        """
        import asyncio
        import time

        threshold = settings.sandbox.idle_ttl_s
        slots = asyncio.Semaphore(5)

        async def protect(chat_id, sess):
            if time.monotonic() - sess.last_active_ts < threshold:
                return
            if chat_id in self._parking_in_flight:
                return
            async with slots:
                try:
                    lock = await self._get_session_lock(chat_id)
                    if lock.locked():
                        return
                    async with lock, session_registry.claim(chat_id):
                        if self._sessions.get(chat_id) is not sess:
                            return
                        if await session_registry.lookup(chat_id) != str(sess.sandbox.id):
                            await self._release_session(chat_id, sess, detach_only=True)
                            return
                        if await session_registry.in_use_elsewhere(chat_id, threshold):
                            return
                        await self._renew(sess)
                        await session_registry.renew_binding(chat_id, str(sess.sandbox.id))
                except Exception as exc:
                    logger.warning("[opensandbox] queued snapshot renewal failed session=%s: %s",
                                   chat_id, exc)

        await asyncio.gather(*(protect(chat_id, sess) for chat_id, sess in entries))

    async def _destroy_session(self, session_id, sess, *, reuse_sandbox=False):
        from core.capabilities.plugins.resources.prewarm import closing, stop
        import asyncio
        async with closing(session_id):
            clean = False
            try:
                processes = getattr(self, "_command_processes", None)
                if processes:
                    await processes.close_owner(session_id)
                clean = await stop(session_id)
            except asyncio.CancelledError:
                # Lost claim or caller cancellation must preserve the session.
                raise
            except Exception:
                await self._release_session(session_id, sess, reuse_sandbox=False)
                raise
            else:
                await self._release_session(
                    session_id, sess, reuse_sandbox=reuse_sandbox and clean,
                )

    async def _release_session(
        self,
        session_id: str,
        sess: _Session,
        *,
        reuse_sandbox: bool = False,
        detach_only: bool = False,
    ) -> None:
        """Destroy a chat session.

        Plan F Q2: ``reuse_sandbox=True`` returns the sandbox to the
        ``_jupyter_user_pool`` idle deque instead of truly destroying it; the same
        user's next chat takes it straight from idle, skipping Sandbox.create +
        _persistent_setup (~14s). Only the idle-reaper caller passes True; stale
        paths (renew failures, connection errors, etc.) keep False — those
        sandboxes are broken and must not be reused.

        Conditions that force ``reuse_sandbox`` to False: sess.stale_marked / the
        sandbox was not taken from the user pool (snapshot restore / old fallback
        path) — both destroy directly. Routing must follow ``sess.pool_source``,
        never re-decide from the current settings; toggling the flag mid-flight
        would land the release in the other pool, corrupting the counters / mixing
        across users.
        """
        # The session lock protects the caller; identity makes repeated retirement
        # harmless and cannot pop a replacement created while a cleanup was queued.
        if self._sessions.get(session_id) is not sess:
            return
        # Choose the target pool by sess.pool_source — recorded at acquire time, always accurate
        is_user_pool = sess.pool_source == "user" and bool(sess.user_id)
        reuse_ok = (
            reuse_sandbox
            and not detach_only
            and not sess.stale_marked
            and is_user_pool
        )
        if reuse_ok:
            # 交给下一个会话之前把 /workspace 擦干净：容器是复用的，上一轮的脚本、中间
            # 结果、解压出来的原料会原样留着，下一个会话一 ls 就看见、会当成自己的上文。
            # 擦不干净就不复用 —— 宁可多花十秒新建，也不能把脏容器递出去。
            reuse_ok = await self._wipe_workspace(sess)
        # 先解绑再入池：容器一旦进了空闲队列，归属判定就会在 acquire / reap 时被问到，
        # 这时登记还指着它的话，池子会以为它仍有主而拒绝复用和回收，白白漏掉一台。
        # 撤销只在登记仍指向这一个沙箱时生效，免得抹掉别的 worker 刚建的那条。
        if self._sessions.get(session_id) is not sess:
            return
        self._sessions.pop(session_id, None)
        retired_owner = None
        try:
            if not detach_only and sess.pool_source == "attached":
                sandbox_id = str(sess.sandbox.id)
                if await session_registry.lookup(session_id) == sandbox_id:
                    retired_owner = await session_registry.tracked_owner(sandbox_id)
            if not detach_only:
                await session_registry.forget(session_id, str(getattr(sess.sandbox, "id", "")))
        except Exception as exc:
            # The bounded registry TTL will expire the old binding. A Redis
            # outage must not leak an already-detached local pool reservation.
            logger.warning("[opensandbox] retiring session=%s: binding cleanup failed: %s",
                           session_id, exc)
        finally:
            import asyncio

            async def return_reservation():
                if is_user_pool:
                    await self._jupyter_user_pool.release(
                        sess.user_id, sess.sandbox, reuse=reuse_ok,
                        can_destroy=lambda: not detach_only and session_registry.claim_valid(),
                    )
                elif sess.pool_source == "general":
                    await self._pool.release(
                        "jupyter", sess.sandbox, reuse=False,
                        can_destroy=lambda: not detach_only and session_registry.claim_valid(),
                    )
                elif not detach_only and session_registry.claim_valid():
                    if sess.pool_source == "attached":
                        await self._destroy_sandbox(sess.sandbox, expected_owner=retired_owner)
                    else:
                        await self._destroy_sandbox(sess.sandbox)

            # Once detached, accounting must finish even if the caller is
            # cancelled while waiting for a pool lock. Pools recheck the shared
            # lease after that wait, before reuse or destruction.
            await finish_accounting(return_reservation())
