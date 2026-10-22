"""OpenSandbox session/snapshot/creation methods (mixin)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Optional

from core.config.settings import settings
from core.sandbox.errors import SandboxNotFoundError, SandboxError
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session

# Snapshot persistence (see internal design docs). Imports are
# deferred to runtime to avoid cross-module circular dependencies at startup
# (models.py also imports core.db.engine).
from core.sandbox.providers.opensandbox.runtime import logger
from core.sandbox.workspace.identity import SANDBOX_RUN_GID, SANDBOX_RUN_UID


from core.sandbox.providers.opensandbox.binding import _OpenSandboxBindingMixin
from core.sandbox.providers.opensandbox.tracking import _OpenSandboxTrackingMixin
from core.sandbox.providers.opensandbox.reclamation import _OpenSandboxReclamationMixin

class _OpenSandboxLifecycleMixin(_OpenSandboxBindingMixin, _OpenSandboxTrackingMixin, _OpenSandboxReclamationMixin):
    async def _create_session_for(
        self, session_id: str, user_id: Optional[str] = None,
    ) -> _Session:
        """Construct a session: prefer restoring from this chat's snapshot in the DB, otherwise create fresh.

        Lookup or restore failures preserve the saved snapshot and fail closed.
        A new workspace is created only when no snapshot exists.
        See internal design docs §5.3.

        ``user_id`` is passed through to ``_create_session*`` — required for all
        per-user MySpace/account-credential bind mounts.

        A snapshot wins over the Plan F Q2 idle pool: pool sandboxes are wiped
        clean, so serving them to a chat that has one would silently drop the
        workspace it built up. The ~7s warm path is only correct for a chat with
        nothing to restore, so the idle pool is checked after the lookup misses.
        """
        from core.capabilities.skills.publication import prepare_skill_view

        # This runs even on a pre-warmed pool hit, before any snapshot/fresh
        # fallback can hide a publication failure.
        if not str(user_id or "").startswith("api_ak_"):
            await asyncio.to_thread(prepare_skill_view, user_id)
        snapshot_id = (
            await self._lookup_snapshot(session_id)
            if settings.sandbox.opensandbox_snapshot_enabled else None
        )
        if snapshot_id:
            sess = await self._create_session_from_snapshot(snapshot_id, user_id=user_id)
            asyncio.create_task(self._defer_snapshot_cleanup(session_id, snapshot_id))
        else:
            sess = await self._create_session(user_id=user_id)
        logger.info("[opensandbox] new session: chat_id=%s sandbox_id=%s restored=%s",
                    session_id, sess.sandbox.id, bool(snapshot_id))
        return sess


    async def _attach_session(
        self, sandbox_id: str, user_id: Optional[str],
    ) -> Optional[_Session]:
        """接管别的 worker 已经为这个会话建好的沙箱；确认容器消失才返回 ``None``（交给恢复/新建）。

        ``pool_source="attached"``：本进程没有从自己的池里 acquire 过它，销毁时必须直接
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
                "[opensandbox] attach sandbox_id=%s failed (%s)",
                sandbox_id, exc,
            )
            if isinstance(exc, SandboxNotFoundError) or getattr(getattr(exc, "error", None), "code", None) == "DOCKER::SANDBOX_NOT_FOUND":
                return None
            # An ambiguous connection error does not authorize replacement.
            raise
        sess = _Session(
            sandbox=sbx,
            user_id=user_id,
            pool_source="attached",
            last_renew_ts=time.monotonic(),
            last_active_ts=time.monotonic(),
        )
        # The old container may be seconds from expiry after a worker restart.
        # Connecting to its metadata does not extend its server-side deadline.
        try:
            await self._renew(sess)
        except Exception as exc:
            if not isinstance(exc, SandboxNotFoundError):
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
            asyncio.create_task(self._renew_bound_session(session_id, sess))


    async def _renew_in_background(self, session_id: str, sess: _Session) -> None:
        """Fire-and-forget renew. Does not block the request path.

        On failure, distinguish stale (lifecycle) from transient (httpx timeout etc.):
        - stale → mark ``sess.stale_marked``; the next ``_get_or_create_session``
          destroys and rebuilds the instant it takes the lock. Do not destroy
          directly here, to avoid racing an in-flight request (which may be running
          codes.run on sess.sandbox right now).
        - transient → log and retain the session. Repeated network or auth
          failures do not prove that a container has disappeared.
        """
        try:
            await self._renew(sess)
            sess.consecutive_renew_failures = 0
        except Exception as e:  # noqa: BLE001
            stale = isinstance(e, SandboxNotFoundError)
            if not stale:
                sess.consecutive_renew_failures += 1
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
        return (
            isinstance(exc, SandboxNotFoundError)
            or getattr(getattr(exc, "error", None), "code", None) == "DOCKER::SANDBOX_NOT_FOUND"
        )


    async def _renew(self, sess: _Session) -> None:
        # The SDK has no direct renew method → hit the management API directly.
        # The body must carry expiresAt (an absolute UTC RFC3339 time); relative values get a 422
        from datetime import datetime, timezone
        new_expiry = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self._session_expiry_s())
        )
        url = f"{self._domain.rstrip('/')}/v1/sandboxes/{sess.sandbox.id}/renew-expiration"
        headers = {"Content-Type": "application/json"}
        client = self._get_http_client()
        # No per-request timeout: the server-side ``update sandbox labels`` measures
        # 14-25s in practice (opensandbox-server logs
        # ``action=update sandbox labels | duration=14.83``); the old timeout=5 made
        # every background renew ReadTimeout, exploded the logs, and never actually
        # extended the sandbox's life. Only the client default
        # (settings.opensandbox_request_timeout_s, default 120s) is enough. Renew is
        # already fire-and-forget (_renew_in_background) and does not block the user
        # request path.
        resp = await client.post(
            url, headers=headers,
            json={"expiresAt": new_expiry.isoformat().replace("+00:00", "Z")},
        )
        if resp.status_code == 404:
            try:
                code = resp.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if code == "DOCKER::SANDBOX_NOT_FOUND":
                raise SandboxNotFoundError(f"sandbox {sess.sandbox.id} no longer exists")
        if resp.status_code >= 400:
            raise SandboxError(f"renew failed: HTTP {resp.status_code} {resp.text[:300]}")

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
                getattr(sess.sandbox, "id", "?"),
                exc,
            )
            return False
        if exit_code != 0:
            logger.warning(
                "[opensandbox] 清空 /workspace 退出码 %d sandbox_id=%s；放弃复用",
                exit_code,
                getattr(sess.sandbox, "id", "?"),
            )
            return False
        return True
