"""OpenSandbox session/snapshot/creation methods (mixin)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, Optional

import core.sandbox.sessions.registry as session_registry
from core.config.settings import settings
from core.sandbox.errors import SandboxCapacityExhausted, SandboxConnectError
from core.sandbox.credential_dispatch import credential_mounts
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session

# Snapshot persistence (see internal design docs). Imports are
# deferred to runtime to avoid cross-module circular dependencies at startup
# (models.py also imports core.db.engine).
from core.sandbox.providers.opensandbox.runtime import (
    _LIGHT_ENTRYPOINT,
    _SETUP_ENTRYPOINT,
    logger,
)
from core.sandbox.providers.opensandbox.volumes import _make_skills_volumes, _pool_metadata
from core.sandbox.workspace.identity import safe_user_id


class _OpenSandboxCreationMixin:
    workspace_mount_names = ("myspace", "skills")

    def _session_entrypoint(self, user_id):
        return _SETUP_ENTRYPOINT if user_id else _LIGHT_ENTRYPOINT

    async def _additional_workspace_mounts(self, user_id):
        from core.files.spaces.mounts import provision_volumes

        if not user_id:
            return [], {}
        return await asyncio.to_thread(provision_volumes, user_id)

    async def _create_jupyter_sandbox(
        self,
        *,
        from_snapshot_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Any:
        """Create an account sandbox with its authorized file volumes."""
        config = self._make_config()
        extra_volumes, extra_metadata = await self._additional_workspace_mounts(user_id)
        # Reserved API principals get no account/shared host mounts. Their
        # selected skill packages are staged explicitly into this container.
        if str(user_id or "").startswith("api_ak_"):
            volumes = []
        else:
            skills_vols = _make_skills_volumes(user_id)
            volumes = skills_vols + extra_volumes + credential_mounts(user_id)
        # entrypoint / volumes / metadata / timeout are exactly the same on the image
        # and snapshot paths; volumes must also be passed explicitly on the snapshot
        # path — docker commit does not preserve mount configuration, otherwise the
        # /workspace/skills/ bind-mount is lost (verified, see docs §A).
        # skip_health_check=True: same as the light sandbox — execd's own readiness suffices, no waiting for Jupyter.
        # The metadata tag is for cross-process adopt (backend restart reclaiming its own sandboxes, see _adopt_existing_sandboxes).
        # 目录约定（scratch + /myspace 软链）随容器启动一起做完，首条命令不必再花一次
        # execd 往返（实测 ~1s）。uid 走环境变量，entrypoint 保持常量。
        setup_uid = safe_user_id(user_id) if user_id else ""
        common_kwargs = dict(
            timeout=timedelta(seconds=self._session_expiry_s()),
            entrypoint=self._session_entrypoint(setup_uid),
            connection_config=config,
            skip_health_check=True,
            volumes=volumes or None,
            metadata={**_pool_metadata("jupyter", user_id=user_id), **extra_metadata},
        )
        if setup_uid:
            common_kwargs["env"] = {"JX_MYSPACE_UID": setup_uid}
        try:
            if from_snapshot_id:
                sbx = await self._create_owned_sandbox(snapshot_id=from_snapshot_id, **common_kwargs)
            else:
                sbx = await self._create_owned_sandbox(self._image, **common_kwargs)
        except SandboxCapacityExhausted:
            raise
        except Exception as e:
            raise SandboxConnectError(f"无法创建 OpenSandbox 持久会话沙盒: {e}") from e

        # The entrypoint prepared the workspace; avoid repeating setup on first use.
        if setup_uid:
            try:
                sbx._jx_setup_done = True  # type: ignore[attr-defined]
                sbx._jx_myspace_uid = setup_uid  # type: ignore[attr-defined]
            except Exception as fallback_error:  # noqa: BLE001 — defensive: opaque SDK object
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.sandbox.providers.opensandbox.creation:_create_jupyter_sandbox', fallback_error)
        return sbx

    async def _create_light_sandbox(self) -> Any:
        """Create a light sandbox: just the init entrypoint + execd, no Jupyter.
        Used for ephemeral one-shot execution (the frontend "re-run" button, skill calls without a chat_id).

        Key point: ``skip_health_check=True`` ── the SDK's default readiness probe
        waits for the Jupyter endpoint, which doesn't exist in the light sandbox →
        by default it would grind until ``ready_timeout`` before returning. With
        skip, startup drops from ~24s to ~3s, relying on execd's own readiness
        (execd starts in ≤ 1s; the first commands.run establishes the connection
        automatically).
        """
        config = self._make_config()
        # No user id here on purpose: light sandboxes are pre-warmed and handed to
        # whoever asks next, so they may only ever carry shared skills.
        skills_vols = _make_skills_volumes()
        try:
            sbx = await self._create_owned_sandbox(
                self._image,
                timeout=timedelta(seconds=300),  # ephemeral is short-lived; 5min cap as a safety net
                entrypoint=_LIGHT_ENTRYPOINT,
                connection_config=config,
                skip_health_check=True,
                volumes=skills_vols or None,
                metadata=_pool_metadata("light"),
            )
        except SandboxCapacityExhausted:
            raise
        except Exception as e:
            raise SandboxConnectError(f"无法创建 OpenSandbox 轻量沙盒: {e}") from e
        return sbx

    async def _repoint_execd_direct(self, sbx: Any) -> bool:
        """Repoint the sandbox's execd service endpoint from "OpenSandbox server proxy" to a direct connection
        to the sandbox container ``sandbox-<id>:44772``, bypassing the server proxy's ~3s/request buffering overhead.

        Measured (v0.1.13 server proxy vs direct): file read/write 3000ms→<5ms,
        commands 4000ms→1000ms. Toggle: ``OPENSANDBOX_DIRECT_EXECD`` (on by
        default). Requires backend and sandbox containers to be reachable on the
        same docker network — true for this project's compose topology (sandboxes
        join hugagent-network, and backend is on it too).

        A one-time reachability probe is done at creation: any HTTP response
        (including 404) counts as reachable → repoint the endpoint; connection
        failure/timeout → keep the original proxy endpoint (safe fallback for
        non-compose deployments, zero functionality loss). Returns whether we
        switched to direct connection.
        """
        if not settings.sandbox.opensandbox_direct_execd_enabled:
            return False
        # This process has already determined direct connection is unreachable → skip probing; don't let every sandbox burn another 6s probe budget.
        if getattr(self, "_direct_execd_unreachable", False):
            return False
        try:
            from opensandbox.constants import DEFAULT_EXECD_PORT
        except Exception:  # noqa: BLE001
            DEFAULT_EXECD_PORT = 44772  # type: ignore[assignment]

        sbx_id = getattr(sbx, "id", None)
        if not sbx_id:
            return False
        direct = f"sandbox-{sbx_id}:{DEFAULT_EXECD_PORT}"

        # Collect all services holding an execd_endpoint (commands/files/health; deduped by object identity).
        seen: set[int] = set()
        endpoints: list[Any] = []
        for attr in (
            "commands",
            "files",
            "_command_service",
            "_filesystem_service",
            "_health_service",
        ):
            svc = getattr(sbx, attr, None)
            ep = getattr(svc, "execd_endpoint", None) if svc is not None else None
            if ep is None or id(ep) in seen or not getattr(ep, "endpoint", None):
                continue
            seen.add(id(ep))
            endpoints.append(ep)
        if not endpoints:
            return False

        # Reachability probe (returns as soon as any response arrives on the direct network, ~ms scale; if unreachable, retries within the budget then falls back).
        if not await self._probe_direct_execd(direct):
            # Topology-level verdict: mark this process as unreachable so subsequent sandboxes skip probing entirely (see the early-return above).
            self._direct_execd_unreachable = True
            logger.info(
                "[opensandbox] direct execd unreachable in this deployment (first probe %s "
                "exhausted budget); disabling further probes, keeping server proxy",
                direct,
            )
            return False

        from core.sandbox.providers.opensandbox.transport import repoint_execd_clients

        if not repoint_execd_clients(sbx, direct):
            return False
        logger.info(
            "[opensandbox] execd repointed direct -> %s (bypass server proxy)",
            direct,
        )
        return True

    async def _probe_direct_execd(
        self,
        hostport: str,
        *,
        budget_s: float = 6.0,
        interval_s: float = 0.25,
    ) -> bool:
        """Probe whether execd is directly reachable at ``hostport``: any HTTP response counts as reachable
        (execd returning 404 for unknown paths also counts as success — it only proves TCP/HTTP connectivity).

        **With retries**: the endpoint fastpath lets create return in ~1s, at which
        point the sandbox container has just started and execd may not be listening
        yet (boot ~1s). Here we poll until execd is up — essentially moving the
        "execd boot the first command would have waited for anyway" forward into
        creation-time direct-connection confirmation; once confirmed and switched
        to direct, the first command no longer hits a dead endpoint. Any response
        within the budget means directly reachable; exceeding the budget (typically
        a non-compose deployment where the container name is unresolvable) → fall
        back to the server proxy."""
        client = self._get_http_client()
        deadline = time.monotonic() + budget_s
        while True:
            try:
                await client.get(f"http://{hostport}/", timeout=1.5)
                return True
            except Exception:  # noqa: BLE001 — connect refused (execd not up yet) / dns / timeout
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(interval_s)

    async def _destroy_sandbox(
        self, sbx: Any, *, expected_owner: Optional[str] = None,
    ) -> None:
        """Destroy an unbound container after atomically fencing its old tracker."""
        sandbox_id = str(sbx.id)
        original_owner = expected_owner or self._tracking_owner_token()
        try:
            async with session_registry.destruction_guard(
                sandbox_id, expected_owner=original_owner,
            ) as allowed:
                if allowed:
                    await sbx.kill()
                    await self._sandbox_admission().release(sandbox_id)
                else:
                    logger.info("[opensandbox] retained sandbox=%s owned elsewhere", sandbox_id)
        except Exception as exc:
            logger.warning("[opensandbox] destroy deferred sandbox=%s: %s", sandbox_id, exc)
        finally:
            try:
                await sbx.close()
            except Exception as fallback_error:
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.sandbox.providers.opensandbox.creation:_destroy_sandbox', fallback_error)

    async def _pooled_sandbox_owner(self, sbx: Any) -> Optional[str]:
        """池子用的归属谓词：这台容器当前属于哪个会话，无主返回 ``None``。

        注入给 ``_JupyterUserPool``，在 ``acquire`` / ``reap_idle`` 两个消费点把住
        「空闲队列里只有无主容器」这条不变式。判定的真源是跨进程的
        :mod:`core.sandbox.sessions.registry`，不是本进程的 ``_sessions``——会把容器
        重新绑走的恰恰是**别的** worker。
        """
        sandbox_id = getattr(sbx, "id", None)
        if not sandbox_id:
            return None
        bound = await session_registry.bound_session(str(sandbox_id))
        if bound:
            return bound
        # A missing tracker may be an expired lease from a still-live worker.
        # Claim it atomically before delivery so startup adoption cannot take the
        # same container between this predicate and the later session binding.
        if not await self._track_sandbox(str(sandbox_id)):
            return "worker:another-owner"
        return await session_registry.bound_session(str(sandbox_id))

    async def _probe_pooled_sandbox_alive(self, sbx: Any) -> bool:
        """Pre-delivery liveness probe for the pools: injected into both pools' ``acquire`` to decide whether an idle handle may be handed out.

        Root cause (failures like trace 8a338f71): what the idle queues of
        ``SandboxPool`` / ``_JupyterUserPool`` hold are **in-process memory
        handles**, while the corresponding container may long since have been
        reclaimed by the sandbox server per TTL (default 1800s) or reaped
        out-of-band on the node side — the pool knows nothing about this (see the
        ``_pool.idle_sandboxes`` comment). Handing such a dead handle to a session
        makes every subsequent filesystem / execd call hit
        ``404 SANDBOX_NOT_FOUND``; after ``_persistent_setup`` fails it spins in a
        "grab the next dead handle" retry loop, and orphan-leaks the real sandboxes
        freshly created by refill on the server side.

        Here we cheaply probe ``GET /sandboxes/{id}`` once before delivery:

        - The server definitively says it's gone (NOT_FOUND / non-running state) →
          ``False``; the caller discards it and takes the next one.
        - ``get_info`` succeeds and state=running (or state unknown) → ``True``.
        - The probe itself hits network jitter / timeout (ambiguous signal) →
          ``True``: give the questionable handle a chance; never mistakenly kill a
          healthy sandbox over one transient probe failure. Real problems are
          covered by runtime stale rebuild (``_is_stale_error``).

        Triggered only on the cold path of each chat session's **first sandbox
        creation** (``_get_or_create_session`` returns straight from cache on hit,
        without acquiring), which is already a multi-second operation, so one extra
        lightweight GET is negligible.
        """
        sid = getattr(sbx, "id", "?")
        try:
            info = await asyncio.wait_for(
                sbx.get_info(),
                timeout=settings.sandbox.opensandbox_pool_liveness_probe_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[opensandbox] pool liveness probe timeout for %s; keeping (ambiguous)",
                sid,
            )
            return True
        except Exception as e:  # noqa: BLE001
            if self._is_stale_error(e):
                logger.info(
                    "[opensandbox] pool liveness: sandbox %s gone on server (%s); evicting",
                    sid,
                    e,
                )
                return False
            logger.warning(
                "[opensandbox] pool liveness probe error for %s (%s); keeping (ambiguous)",
                sid,
                e,
            )
            return True
        state = (getattr(getattr(info, "status", None), "state", "") or "").lower()
        if state and state != "running":
            logger.info(
                "[opensandbox] pool liveness: sandbox %s state=%s (not running); evicting",
                sid,
                state,
            )
            return False
        return True

    async def _create_session(self, user_id: Optional[str] = None) -> _Session:
        """Route account sessions to their user pool; anonymous sessions use the general pool."""
        use_user_pool = bool(user_id)

        if use_user_pool:
            sandbox = await self._jupyter_user_pool.acquire(user_id)
        else:
            sandbox = await self._pool.acquire("jupyter")
        # No CodeInterpreter: all languages go through execd commands.run (see cloud_processes).
        return _Session(
            sandbox=sandbox,
            user_id=user_id,
            pool_source="user" if use_user_pool else "general",
            last_renew_ts=time.monotonic(),
            last_active_ts=time.monotonic(),
        )

    async def _create_session_from_snapshot(
        self, snapshot_id: str, user_id: Optional[str] = None,
    ) -> _Session:
        """Start a persistent session sandbox directly from an opensandbox snapshot + wrap it into a _Session.

        Differences from ``_create_session``:
        - **Bypasses the pool**: a snapshot-started sandbox is dedicated to this one
          chat and must not enter the general warm pool (whose sandboxes are all
          freshly created from the base image).
        - **Full filesystem restore**: /workspace/, /workspace/skills/ (re-bind-mounted),
          previous artifacts, the scratch directory, and myspace symlinks are all
          restored from the snapshot.
        - **No kernel concept**: execd commands.run runs the code; the sandbox is
          usable as soon as it is up.
        """
        sandbox = await self._create_jupyter_sandbox(
            from_snapshot_id=snapshot_id, user_id=user_id,
        )
        return _Session(
            sandbox=sandbox,
            user_id=user_id,
            # The snapshot contains the filesystem results of the directory conventions; skip redundant setup
            setup_done=True,
            last_renew_ts=time.monotonic(),
            last_active_ts=time.monotonic(),
        )

    def _sandbox_admission(self):
        from core.sandbox.providers.opensandbox.admission import opensandbox_admission

        return opensandbox_admission()

    async def _create_owned_sandbox(self, *args, **kwargs):
        """Prevent startup adoption while a new sandbox is being delivered."""
        from opensandbox import Sandbox

        async with session_registry.creation_guard() as token:
            metadata = {**(kwargs.pop("metadata", None) or {}), "hugagent-creation-token": token}
            sandbox = await self._sandbox_admission().create(
                lambda admitted: Sandbox.create(*args, metadata=admitted, **kwargs),
                metadata=metadata,
            )
            await self._repoint_execd_direct(sandbox)
            if not await self._track_sandbox(str(sandbox.id)):
                raise SandboxConnectError("sandbox ownership was acquired elsewhere")
            return sandbox
