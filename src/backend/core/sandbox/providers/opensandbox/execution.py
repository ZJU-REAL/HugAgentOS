"""OpenSandbox execute/run/file-ops methods (mixin)."""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
from shlex import quote as shell_escape
from typing import Any

from core.config.settings import settings
from core.sandbox.protocol import ProcessRequest
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session

# Snapshot persistence (see internal design docs). Deferred to a
# runtime import to avoid a cross-module circular dependency at startup
# (models.py also imports core.db.engine).
from core.sandbox.providers.opensandbox.runtime import _join_logs, logger, write_file
from core.sandbox.workspace.identity import SANDBOX_RUN_GID, SANDBOX_RUN_UID, safe_user_id
from core.sandbox.workspace.paths import WORKSPACE


class _OpenSandboxExecMixin:
    async def reap_idle_sessions(self) -> int:
        """Maintain the sandbox pools: sweep dead warm handles, reap idle ones.

        Chat-bound sessions are deliberately NOT reclaimed here. Releasing one
        wipes /workspace, so doing it without a snapshot loses the chat's files;
        that job belongs to ``_snapshot_idle_sessions_once``, which snapshots
        first and releases second on the same
        ``settings.sandbox.idle_ttl_s``. This method only handles pool
        entries, which carry no chat state.

        Returns the number of pool sandboxes reaped this round.
        """
        reconciled = await self._reconcile_sessions()
        # Keep the shared capacity ledger in step with the controller even when
        # nobody is creating; admission also reconciles on demand.
        try:
            await self._sandbox_admission().reconcile_if_due()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[opensandbox] capacity reconciliation failed: %s", exc)
        # First maintain the general warm pool: evict idle dead handles the
        # server already reclaimed by TTL + top back up to min_idle. No renewal;
        # when one dies, refill proactively in the background — otherwise the
        # warm pool gets drained after 30min and every subsequent acquire pays
        # the full creation cost. Independent of the session-reap threshold;
        # runs every cycle (when threshold<=0 we return early below, but
        # warm-pool keep-alive must not be turned off along with it).
        try:
            n_evicted = await self._pool.sweep_and_refill()
            if n_evicted:
                logger.info(
                    "[opensandbox] warm-pool swept %d dead idle sandbox(es), refilled",
                    n_evicted,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[opensandbox] warm-pool sweep failed: %s", exc)

        threshold = settings.sandbox.idle_ttl_s

        # Reap idle sandboxes past their TTL in _jupyter_user_pool (actually destroyed)
        try:
            return reconciled + (await self._jupyter_user_pool.reap_idle(ttl_s=threshold) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[opensandbox] jupyter idle pool reap failed: %s", exc)
            return 0

    # ───── Execution paths ─────────────────────────────────────────────────

    async def _sync_inputs(self, sess: _Session, req: ProcessRequest) -> None:
        await self._persistent_setup(sess, req)
        await self._sync_inputs_to_sandbox(
            sess.sandbox,
            req,
        )

    async def _persistent_setup(self, sess: _Session, req: ProcessRequest) -> None:
        """Set up scratch and account file links before execution."""
        uid = safe_user_id(req.user_id)
        # 「scratch 建好」且「本次请求的用户已有 /myspace 软链」才算做完：漏了后者，
        # 不带 user_id 的调用方会把残缺 setup 记成完成，之后的 bash 再也补不上软链。
        if sess.setup_done and (not uid or sess.myspace_link_uid == uid):
            return
        # Q2: sandbox-level marker; skip when reused across sessions
        if getattr(sess.sandbox, "_jx_setup_done", False) and (
            not uid or getattr(sess.sandbox, "_jx_myspace_uid", None) == uid
        ):
            sess.setup_done = True
            sess.myspace_link_uid = getattr(sess.sandbox, "_jx_myspace_uid", None)
            return

        # Assemble the shell: scratch must always be created; when uid is
        # missing skip the myspace symlink, but scratch must not be skipped.
        # ln -sfn would create the link INSIDE /myspace when it's already a real
        # directory, so it must first be rm'd under the 'symlink or missing'
        # check — equivalent to the Python fallback. When bind mount is on,
        # /workspace/myspace/{uid} is already there as the mount point (owned by
        # the host directory the backend pre-created). When it is off, we create
        # it ourselves — with ``install -d`` owned by SANDBOX_RUN_UID, because
        # this command runs as root and a plain mkdir would leave a root-owned
        # directory that user code cannot write.
        parts = ["mkdir -p /workspace/scratch"]
        if uid:
            mp = f"/workspace/myspace/{uid}"
            mp_q = shell_escape(mp)
            own = f"-o {SANDBOX_RUN_UID} -g {SANDBOX_RUN_GID}"
            parts.append(f"{{ [ -d {mp_q} ] || install -d {own} {mp_q}; }}")
            # Only rm when /myspace is a symlink or missing (leave real directories left by the user/container alone)
            parts.append(
                "if [ -L /myspace ] || [ ! -e /myspace ]; then "
                f"rm -f /myspace; ln -s {mp_q} /myspace; fi"
            )
        cmd = " && ".join(parts)

        # Marking setup_done must wait for the command to actually succeed —
        # early versions treated an execd 200 response with exit_code != 0 as
        # success; not checking exit_code lets the marker permanently taint the
        # sandbox (idle-pool reuse then skips setup every time → the failed
        # state becomes permanent).
        #
        # **Do NOT pass uid/gid=1000 here**: ``ln -s ... /myspace`` writes to
        # ``/``, and ``/`` is root-owned mode 755 — ubuntu (1000) has no
        # permission. Running the setup command as execd's default identity
        # (root) is exactly right; the artifact directory is /workspace/scratch
        # (pre-created in the Dockerfile and chowned to 1000) + the /myspace
        # symlink (a root-owned link pointing at an ubuntu-owned target — actual
        # read/write permission on linked files follows ubuntu, no problem).
        # User code goes through ``cloud_processes``'s separate RunCommandOpts
        # with its own uid=1000 — the two paths are decoupled and don't affect
        # each other. No Jupyter kernel → no fallback path; on execd failure we
        # only log, don't mark done, and retry on the next execution.
        try:
            from opensandbox.models.execd import RunCommandOpts

            opts = RunCommandOpts(
                timeout=timedelta(seconds=8),
            )
            result = await asyncio.wait_for(
                sess.sandbox.commands.run(cmd, opts=opts),
                timeout=12,
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            if exit_code == 0:
                sess.setup_done = True
                if uid:
                    sess.myspace_link_uid = uid
                try:
                    sess.sandbox._jx_setup_done = True  # type: ignore[attr-defined]
                    if uid:
                        sess.sandbox._jx_myspace_uid = uid  # type: ignore[attr-defined]
                except Exception as fallback_error:  # noqa: BLE001 — defensive: opaque SDK object
                    from core.infra.failures import record_best_effort_failure
                    record_best_effort_failure('core.sandbox.providers.opensandbox.execution:_persistent_setup', fallback_error)
            else:
                stderr = _join_logs(getattr(getattr(result, "logs", None), "stderr", []) or [])
                logger.warning(
                    "[opensandbox] _persistent_setup execd exited %d (will retry next exec): %s",
                    exit_code,
                    stderr[:300],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[opensandbox] _persistent_setup execd failed (will retry next exec): %r",
                exc,
            )

    async def _sync_inputs_to_sandbox(
        self,
        sbx: Any,
        req: ProcessRequest,
    ) -> None:
        """Stage explicit request attachments."""
        tasks: list[Any] = []
        for name, text in (req.resource_files or {}).items():
            tasks.append(write_file(sbx, f"{WORKSPACE}/{name}", text))
        for name, text in (req.input_files or {}).items():
            tasks.append(write_file(sbx, f"{WORKSPACE}/{name}", text))
        for name, b64 in (req.input_files_b64 or {}).items():
            tasks.append(write_file(sbx, f"{WORKSPACE}/{name}", base64.b64decode(b64)))

        if tasks:
            await asyncio.gather(*tasks)
