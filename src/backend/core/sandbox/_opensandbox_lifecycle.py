"""OpenSandbox session/snapshot/creation methods (mixin)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Optional


from . import session_registry
from ._common import SANDBOX_RUN_GID, SANDBOX_RUN_UID
from .errors import SandboxError

# Snapshot persistence (see internal design docs). Imports are
# deferred to runtime to avoid cross-module circular dependencies at startup
# (models.py also imports core.db.engine).
# Provider internals moved to core.sandbox._opensandbox_internals; import back
from core.sandbox._opensandbox_internals import logger, _Session


from . import _opensandbox_session as session_runtime

class _OpenSandboxLifecycleMixin:
    async def _create_session_for(
        self, session_id: str, user_id: Optional[str] = None,
    ) -> _Session:
        """Construct a session: prefer restoring from this chat's snapshot in the DB, otherwise create fresh.

        Any failure along the restore path degrades to the fresh path, and the bad
        snapshot is marked for short retention and handed to GC.
        See internal design docs §5.3.

        ``user_id`` is passed through to ``_create_session*`` — required for all
        per-user MySpace/account-credential bind mounts.

        A snapshot wins over the Plan F Q2 idle pool: pool sandboxes are wiped
        clean, so serving them to a chat that has one would silently drop the
        workspace it built up. The ~7s warm path is only correct for a chat with
        nothing to restore, so the idle pool is checked after the lookup misses.
        """
        from core.agent_skills.publication import prepare_skill_view

        # This runs even on a pre-warmed pool hit, before any snapshot/fresh
        # fallback can hide a publication failure.
        if not str(user_id or "").startswith("api_ak_"):
            await asyncio.to_thread(prepare_skill_view, user_id)
        snapshot_id: Optional[str] = None
        if session_runtime.settings.sandbox.opensandbox_snapshot_enabled:
            try:
                snapshot_id = await self._lookup_snapshot(session_id)
            except Exception as e:  # noqa: BLE001 — a DB failure must not block sandbox creation
                logger.warning(
                    "[opensandbox] _lookup_snapshot(%s) failed: %s; falling back to fresh sandbox",
                    session_id, e,
                )

        # Q2: no snapshot to restore + user-bound mounts + idle pool hit → take
        # the fresh path directly (_create_session's internal acquire will get
        # the idle sandbox, returning in seconds)
        if (
            not snapshot_id
            and user_id
            and (session_runtime._user_bound_sandbox_required() or str(user_id).startswith("api_ak_"))
            and self._jupyter_user_pool.has_idle(user_id)
        ):
            sess = await self._create_session(user_id=user_id)
            logger.info(
                "[opensandbox] new session: chat_id=%s sandbox_id=%s (Q2 idle pool hit)",
                session_id, sess.sandbox.id,
            )
            return sess

        sess: Optional[_Session] = None
        if snapshot_id:
            logger.info(
                "[opensandbox] recreating session=%s from snapshot=%s",
                session_id, snapshot_id,
            )
            try:
                sess = await self._create_session_from_snapshot(snapshot_id, user_id=user_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[opensandbox] restore from snapshot=%s failed: %s; falling back to fresh sandbox",
                    snapshot_id, e,
                )
                asyncio.create_task(
                    self._defer_snapshot_cleanup(session_id, snapshot_id)
                )

        if sess is None:
            sess = await self._create_session(user_id=user_id)

        logger.info(
            "[opensandbox] new session: chat_id=%s sandbox_id=%s%s",
            session_id, sess.sandbox.id,
            f" (from snapshot={snapshot_id})" if snapshot_id else "",
        )
        # The snapshot has been successfully used: asynchronously mark it for short
        # retention (→ GC clears the remote + DB row within an hour). Single-use
        # consumption, so the next lookup doesn't hit the same stale data again.
        if snapshot_id:
            asyncio.create_task(
                self._defer_snapshot_cleanup(session_id, snapshot_id)
            )
        return sess


    async def _get_or_create_session(
        self, session_id: str, user_id: Optional[str] = None,
    ) -> _Session:
        """Get or create a session.

        ``user_id`` is used only when **creating** a session, to bind the myspace
        bind mount. An existing session reuses its original user_id (in
        ``sess.user_id``); None means a legacy caller (the put_file / get_file
        paths) provides no user_id information — just keep reusing the existing
        session.
        """
        lock = await self._get_session_lock(session_id)
        async with lock:
            sess = self._sessions.get(session_id)
            from .bound_session import require_available
            require_available(str(sess.sandbox.id) if sess else None,
                              stale=bool(sess and sess.stale_marked))
            newly_bound = sess is None or sess.stale_marked
            # A previous background renew marked it stale (lifecycle signal) → destroy and rebuild here;
            # no synchronous renew on the request path.
            if sess is not None and sess.stale_marked:
                logger.info(
                    "[opensandbox] recreating session=%s (stale_marked by background renew)",
                    session_id,
                )
                # On rebuild, prefer the user_id the caller passes now; otherwise keep the old session's user_id
                # so the bind mount still points at the same user directory.
                user_id = user_id or sess.user_id
                await self._destroy_session(session_id, sess)
                sess = None
            if sess is not None:
                self._maybe_schedule_renew(session_id, sess)
            if sess is None:
                # Fallback user binding: when the caller gives no explicit user_id
                # (legacy paths like put_file/get_file, or future file operations),
                # take it from the request-level ContextVar. This way **any** sandbox
                # operation preceding bash also creates a user-bound session (with
                # credential volumes mounted) instead of a credential-less general
                # sandbox — fixing at the root "read a skill file first, then run
                # bash → Feishu not_configured". Explicit argument takes precedence;
                # in worker threads (sub-agents) the ContextVar does not propagate,
                # so the explicit argument is the fallback — the two complement each other.
                if not user_id:
                    from core.infra.logging import user_id_var
                    user_id = user_id_var.get() or None
                # 跨进程认领：同一会话只能有一个沙箱，无论这一轮落在哪个 worker。
                # 先查登记——别的 worker 已经为它建好容器就连过去，没有才自己建。
                async with session_registry.claim(session_id):
                    bound = await session_registry.lookup(session_id)
                    sess = await self._attach_session(bound, user_id) if bound else None
                    if sess is None:
                        sess = await self._create_session_for(session_id, user_id=user_id)
                self._sessions[session_id] = sess
            # Any _get_or_create_session call (execute/put_file/get_file all pass through here)
            # counts as activity, refreshing the idle baseline. 同一条登记同时记住「用的是
            # 哪台容器」和「最后什么时候用的」，否则只服务过早先几轮的 worker 会把别人
            # 正在用的容器当成空闲回收掉。
            sess.last_active_ts = time.monotonic()
            await session_registry.remember(session_id, str(sess.sandbox.id))
            if newly_bound:
                from core.plugins.resources.prewarm import schedule
                schedule(self, session_id, sess.user_id, str(sess.sandbox.id))
            return sess


    async def _attach_session(
        self, sandbox_id: str, user_id: Optional[str],
    ) -> Optional[_Session]:
        """接管别的 worker 已经为这个会话建好的沙箱；连不上返回 ``None``（交给新建）。

        ``pool_source="none"``：本进程没有从自己的池里 acquire 过它，销毁时必须直接
        销毁，不能往任一个池里 release——那会把 ``in_flight`` 记成负数，或者把别人的
        容器塞进本进程的空闲队列。``setup_done`` 保持 False：目录约定本身幂等
        （``mkdir -p`` + 带判断的软链），首条命令补一次即可。
        """
        from opensandbox import Sandbox

        try:
            sbx = await Sandbox.connect(
                sandbox_id,
                connection_config=self._make_config(),
                skip_health_check=True,
            )
        except Exception as exc:  # noqa: BLE001 — 登记指向的容器可能已经没了
            logger.info(
                "[opensandbox] attach sandbox_id=%s failed (%s); creating a new one",
                sandbox_id, exc,
            )
            return None
        sess = _Session(
            sandbox=sbx,
            interpreter=None,
            contexts={},
            seeded_myspace_mtime={},
            user_id=user_id,
            pool_source="none",
            last_renew_ts=time.monotonic(),
            last_active_ts=time.monotonic(),
        )
        # The old container may be seconds from expiry after a worker restart.
        # Connecting to its metadata does not extend its server-side deadline.
        try:
            await self._renew(sess)
        except Exception as exc:
            if not self._is_stale_error(exc):
                raise
            logger.info("[opensandbox] attached sandbox_id=%s is expired; creating a new one", sandbox_id)
            return None
        await self._repoint_execd_direct(sbx)
        logger.info(
            "[opensandbox] attached to sandbox_id=%s bound by another worker", sandbox_id,
        )
        return sess


    def _maybe_schedule_renew(self, session_id: str, sess: _Session) -> None:
        """Rate-limited fire-and-forget server-TTL renew (request path never blocks ~3s).

        Caller must hold the session lock: the timestamp is stamped **before**
        dispatch precisely so same-second concurrent callers dedupe (N parallel
        renews would be serialized by the server's SQLite lock into an N×3s waste).
        """
        now = time.monotonic()
        if now - sess.last_renew_ts >= self._renew_min_interval_s:
            sess.last_renew_ts = now
            asyncio.create_task(self._renew_in_background(session_id, sess))


    async def touch_session(self, session_id: str) -> bool:
        """Mark a live session as active without executing anything in it.

        See ``core/sandbox/keepalive.py`` for the caller side: long-running
        workflows touch on a timer so the idle reaper does not destroy their
        session during long model-streaming phases with no sandbox calls
        (observed: "reaped idle session=loop-... idle 643s > 600s" → workspace
        wiped, no snapshot because idle < snapshot threshold). Refreshes the
        idle baseline and piggybacks the rate-limited server-TTL renew. No-op
        (False) when the session does not exist yet.
        """
        if session_id not in self._sessions:
            return False
        lock = await self._get_session_lock(session_id)
        async with lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return False
            sess.last_active_ts = time.monotonic()
            self._maybe_schedule_renew(session_id, sess)
            sandbox_id = str(getattr(sess.sandbox, "id", ""))
        await session_registry.remember(session_id, sandbox_id)
        return True


    async def _renew_in_background(self, session_id: str, sess: _Session) -> None:
        """Fire-and-forget renew. Does not block the request path.

        On failure, distinguish stale (lifecycle) from transient (httpx timeout etc.):
        - stale → mark ``sess.stale_marked``; the next ``_get_or_create_session``
          destroys and rebuilds the instant it takes the lock. Do not destroy
          directly here, to avoid racing an in-flight request (which may be running
          codes.run on sess.sandbox right now).
        - transient → log only, keep the session, retry on the next renew. But **N
          consecutive transient failures also escalate to stale** — that usually
          means the sandbox is dead while the server still counts it as existing
          (the window before the 404); otherwise bash would only discover "Sandbox
          not found" on reconnect, after the user has already seen the error. With
          the snapshot restore path in place, the cost of a false-positive rebuild
          is manageable (~2s + files preserved).
        """
        _RENEW_FAIL_STALE_THRESHOLD = 3  # N consecutive transient failures → escalate to stale
        try:
            await self._renew(sess)
            sess.consecutive_renew_failures = 0
        except Exception as e:  # noqa: BLE001
            stale = self._is_stale_error(e)
            if not stale:
                sess.consecutive_renew_failures += 1
                if sess.consecutive_renew_failures >= _RENEW_FAIL_STALE_THRESHOLD:
                    stale = True
            logger.warning(
                "[opensandbox] renew session=%s failed (async; stale=%s; consec_fails=%d; %s): %r",
                session_id,
                stale,
                sess.consecutive_renew_failures,
                "marked for recreate on next acquire" if stale else "keeping current sandbox",
                e,
            )
            if stale:
                sess.stale_marked = True


    @staticmethod
    def _is_stale_error(exc: BaseException) -> bool:
        """Whether an OpenSandbox error proves the sandbox handle is stale.

        Renew can fail transiently under load (for example httpx timeouts while
        many files are being uploaded). Destroying a healthy active session in
        that case loses all materialized skill files and can trap the caller in
        a recreate/re-upload loop. Only lifecycle signals should force rebuild.
        """
        msg = str(exc).lower()
        return (
            "not found" in msg or " 404" in msg or "no such" in msg
            or " 422" in msg or "invalid state" in msg
            or "terminated" in msg or "stopping" in msg
        )


    @staticmethod
    def _looks_like_network_or_timeout(exc: BaseException) -> bool:
        """Walk the exception chain for httpx network/timeout types.

        The SDK wraps all of these as
        ``SandboxInternalException('Network connectivity error: ...')``, while the
        underlying httpx.ReadTimeout's ``str()`` is usually empty — walking the type
        chain is more reliable than string matching. Dedicated to the execute path:
        treat these as stale and rebuild transparently, on the grounds that the user
        is blocked and the cost of one rebuild + re-upload is far smaller than a
        hard error. The renew path keeps the conservative logic (_is_stale_error).
        """
        import httpx
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, (httpx.TimeoutException, httpx.NetworkError)):
                return True
            cur = cur.__cause__ or cur.__context__
        return "network connectivity error" in str(exc).lower()


    async def _renew(self, sess: _Session) -> None:
        # The SDK has no direct renew method → hit the management API directly.
        # The body must carry expiresAt (an absolute UTC RFC3339 time); relative values get a 422
        from datetime import datetime, timezone
        new_expiry = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self._ttl_s)
        )
        url = f"{self._domain.rstrip('/')}/v1/sandboxes/{sess.sandbox.id}/renew-expiration"
        headers = {"Content-Type": "application/json"}
        client = self._get_http_client()
        # No per-request timeout: the server-side ``update sandbox labels`` measures
        # 14-25s in practice (opensandbox-server logs
        # ``action=update sandbox labels | duration=14.83``); the old timeout=5 made
        # every background renew ReadTimeout, exploded the logs, and never actually
        # extended the sandbox's life. Only the client default
        # (session_runtime.settings.opensandbox_request_timeout_s, default 120s) is enough. Renew is
        # already fire-and-forget (_renew_in_background) and does not block the user
        # request path.
        resp = await client.post(
            url, headers=headers,
            json={"expiresAt": new_expiry.isoformat().replace("+00:00", "Z")},
        )
        if resp.status_code >= 400:
            raise SandboxError(f"renew failed: HTTP {resp.status_code} {resp.text}")


    async def _wipe_workspace(self, sess: _Session) -> bool:
        """清空 /workspace，保留当前 provider 声明的挂载点。

        挂载点必须留下：``myspace`` 是用户网盘、``skills`` 是只读技能目录，删了会破坏挂载。
        ``scratch`` 删掉后按沙箱运行身份重建，保持镜像里的目录约定不变。

        返回是否擦干净；失败返回 False，调用方据此放弃复用、直接销毁。
        """
        keep = self.workspace_mount_names
        conds = " ".join(f"! -name {name}" for name in keep)
        cmd = (
            f"find /workspace -mindepth 1 -maxdepth 1 {conds} -exec rm -rf -- {{}} + && "
            f"install -d -o {SANDBOX_RUN_UID} -g {SANDBOX_RUN_GID} /workspace/scratch"
        )
        try:
            from opensandbox.models.execd import RunCommandOpts

            result = await asyncio.wait_for(
                sess.sandbox.commands.run(cmd, opts=RunCommandOpts(timeout=timedelta(seconds=15))),
                timeout=20,
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
        except Exception as exc:  # noqa: BLE001 — 容器可能已经失联
            logger.warning(
                "[opensandbox] 清空 /workspace 失败 sandbox_id=%s: %r；放弃复用",
                getattr(sess.sandbox, "id", "?"), exc,
            )
            return False
        if exit_code != 0:
            logger.warning(
                "[opensandbox] 清空 /workspace 退出码 %d sandbox_id=%s；放弃复用",
                exit_code, getattr(sess.sandbox, "id", "?"),
            )
            return False
        return True


    async def _destroy_session(self, session_id, sess, *, reuse_sandbox=False):
        from core.plugins.resources.prewarm import closing, stop
        async with closing(session_id):
            processes = getattr(self, "_command_processes", None)
            if processes:
                await processes.close_owner(session_id)
            clean = await stop(session_id)
            await self._release_session(session_id, sess, reuse_sandbox=reuse_sandbox and clean)

    async def _release_session(
        self,
        session_id: str,
        sess: _Session,
        *,
        reuse_sandbox: bool = False,
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
        never re-decide from the current session_runtime.settings; toggling the flag mid-flight
        would land the release in the other pool, corrupting the counters / mixing
        across users.
        """
        # Choose the target pool by sess.pool_source — recorded at acquire time, always accurate
        is_user_pool = sess.pool_source == "user" and bool(sess.user_id)
        reuse_ok = (
            reuse_sandbox
            and not sess.stale_marked
            and is_user_pool
        )
        # Kill the jupyter kernel ctx before reuse, otherwise the server-side kernel
        # lives on without retiring, and each reuse creates a fresh ctx in the
        # sandbox → the kernel count inside one container grows linearly with reuse
        # count, eventually hitting the execd per-sandbox kernel cap. The local
        # handles of language ctxs are dropped along with the sess, so all that's
        # needed here is a best-effort remote delete_context.
        if reuse_ok:
            for lang in list(sess.contexts.keys()):
                await self._reset_language_context(sess, lang)
            # 交给下一个会话之前把 /workspace 擦干净：容器是复用的，上一轮的脚本、中间
            # 结果、解压出来的原料会原样留着，下一个会话一 ls 就看见、会当成自己的上文。
            # 擦不干净就不复用 —— 宁可多花十秒新建，也不能把脏容器递出去。
            reuse_ok = await self._wipe_workspace(sess)
        # 先解绑再入池：容器一旦进了空闲队列，归属判定就会在 acquire / reap 时被问到，
        # 这时登记还指着它的话，池子会以为它仍有主而拒绝复用和回收，白白漏掉一台。
        # 撤销只在登记仍指向这一个沙箱时生效，免得抹掉别的 worker 刚建的那条。
        self._sessions.pop(session_id, None)
        await session_registry.forget(session_id, str(getattr(sess.sandbox, "id", "")))
        if is_user_pool:
            await self._jupyter_user_pool.release(
                sess.user_id, sess.sandbox, reuse=reuse_ok,
            )
        elif sess.pool_source == "general":
            await self._pool.release("jupyter", sess.sandbox, reuse=False)
        else:
            # Snapshot restore path: not part of any pool's in_flight, destroy directly —
            # otherwise _pool.release would decrement in_flight negative (pre-existing bug).
            await self._destroy_sandbox(sess.sandbox)
        # Also clear the corresponding locks, so _session_locks doesn't grow unboundedly
        # in a long-running process. 锁是按 (loop, session) 分桶的，所以要清掉所有 loop 下
        # 属于这个 session 的条目，而不只是当前 loop 的那一把。
        with self._registry_lock:
            for key in [k for k in self._session_locks if k[1] == session_id]:
                self._session_locks.pop(key, None)


    async def _reset_language_context(
        self, sess: _Session, language: str,
    ) -> None:
        """Placeholder now that there is no Jupyter kernel: execd commands.run is a fresh shell /
        process every time, so there is no language kernel context to "undo".

        The method is kept (call sites: execute timeout, pre-reuse cleanup) to
        minimize the change surface; it now only discards local contexts leftovers
        (normally always empty) and no longer sends any remote delete_context.
        """
        sess.contexts.pop(language, None)
