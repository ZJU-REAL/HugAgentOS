"""Reclaim owned instances after backend restart."""

from __future__ import annotations

from typing import Any

import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.runtime import (
    _LEGACY_ENTRYPOINTS,
    _LIGHT_ENTRYPOINT,
    _SETUP_ENTRYPOINT,
    _user_bound_sandbox_required,
    logger,
)
from core.sandbox.providers.opensandbox.volumes import (
    _METADATA_OWNER_KEY,
    _METADATA_OWNER_VALUE,
    _METADATA_POOL_KIND_KEY,
    _METADATA_SKILLS_LAYOUT_KEY,
    _METADATA_SKILLS_LAYOUT_VALUE,
    _METADATA_USER_ID_KEY,
)


class _OpenSandboxAdoptionMixin:
    async def _adopt_existing_sandboxes(self) -> None:
        """Find sandboxes this app previously left on the OpenSandbox server and route
        them to adopt / kill.

        Classification rules (strictest to loosest):
        1. ``state != "Running"``                                 → kill (container already dead, metadata leftover)
        2. ``metadata[hugagent-owner] == "backend"``               → 先看 session_registry：
           仍绑在会话上的原样留着（会话下一轮自己 attach 回来），无主的才 adopt 进对应的池
        3. ``entrypoint`` matches ``_LIGHT_ENTRYPOINT`` / a legacy entrypoint,
           but the ``hugagent-owner`` tag is missing               → legacy orphan, kill
        4. anything else (someone else's sandbox, or an unrecognized entrypoint) → leave alone

        Adopted sandboxes go into ``_pool.idle``; surplus adopt candidates beyond
        ``max_idle`` are also killed to avoid unbounded pile-up. This method is called
        once before ``shutdown``; calling it again is idempotent (sandboxes scanned a
        second time are already in the pool, and an SDK ``connect`` failure
        auto-kills).
        """
        stats = {
            "adopted_jupyter": 0,
            "adopted_light": 0,
            "skipped_bound": 0,
            "killed_legacy": 0,
            "killed_dead": 0,
            "killed_overflow": 0,
        }
        # Page through all sandboxes (scaffolding reused from _iter_server_sandbox_infos: create mgr / paginate / dedupe / close).
        async for mgr, info in self._iter_server_sandbox_infos():
            await self._classify_and_adopt(mgr, info, stats)

        logger.info(
            "[opensandbox] sandbox adoption complete: "
            "adopted jupyter=%d light=%d; left to sessions=%d; "
            "killed legacy=%d dead=%d overflow=%d",
            stats["adopted_jupyter"],
            stats["adopted_light"],
            stats["skipped_bound"],
            stats["killed_legacy"],
            stats["killed_dead"],
            stats["killed_overflow"],
        )

    async def _classify_and_adopt(
        self,
        mgr: Any,
        info: Any,
        stats: dict[str, int],
    ) -> None:
        """Classify + act on a single sandbox. Errors are logged as warnings, never raised — one bad sandbox must not block the whole batch."""
        from opensandbox import Sandbox

        try:
            state = (getattr(info.status, "state", None) or "").lower()
            metadata = info.metadata or {}
            entrypoint = list(info.entrypoint or [])
            owner_tag = metadata.get(_METADATA_OWNER_KEY)
            if owner_tag and owner_tag != _METADATA_OWNER_VALUE:
                # Another owner's sandbox (another deployment, or a module's own
                # controller-managed computers) is never adopted or reaped here.
                return
            creation_token = metadata.get("hugagent-creation-token")
            if creation_token and await session_registry.creation_pending(creation_token):
                return  # The creator has not yet delivered this instance.
            kind_tag = metadata.get(_METADATA_POOL_KIND_KEY)

            known_legacy = entrypoint in (_LIGHT_ENTRYPOINT, _SETUP_ENTRYPOINT) or entrypoint in _LEGACY_ENTRYPOINTS
            # Untagged instances with unknown entrypoints belong to other clients.
            if owner_tag != _METADATA_OWNER_VALUE and not known_legacy:
                return
            # 1. Dead owned sandbox → guarded cleanup
            if state != "running":
                if await self._kill_by_id(mgr, info.id, reason=f"non-running state={state}"):
                    stats["killed_dead"] += 1
                return

            if await session_registry.bound_session(str(info.id)):
                stats["skipped_bound"] += 1
                return

            # 2. Our own sandbox → adopt
            if owner_tag == _METADATA_OWNER_VALUE:
                if kind_tag not in ("jupyter", "light"):
                    logger.warning(
                        "[opensandbox] adopt: own sandbox %s missing pool-kind tag, killing",
                        info.id,
                    )
                    if await self._kill_by_id(mgr, info.id, reason="missing pool-kind"):
                        stats["killed_legacy"] += 1
                    return

                # User-bound jupyter sandboxes are re-homed by user_id. When any
                # private-data mount is enabled, a sandbox without a user tag cannot
                # carry the current user's volumes; kill it rather than silently hand
                # out a credential-less or cross-user container.
                user_tag = metadata.get(_METADATA_USER_ID_KEY)
                user_bound_enabled = _user_bound_sandbox_required()
                if kind_tag == "jupyter" and user_bound_enabled and not user_tag:
                    if await self._kill_by_id(
                        mgr,
                        info.id,
                        reason="jupyter sandbox missing user-id tag (legacy / cross-user unsafe)",
                    ):
                        stats["killed_legacy"] += 1
                    return
                # Created before the per-user skill view: its /workspace/skills is the
                # whole shared dir, which no longer holds this user's private skills.
                # Mounts can't be changed after creation, so drop it.
                if (
                    kind_tag == "jupyter"
                    and user_tag
                    and metadata.get(_METADATA_SKILLS_LAYOUT_KEY) != _METADATA_SKILLS_LAYOUT_VALUE
                ):
                    if await self._kill_by_id(
                        mgr, info.id, reason="jupyter sandbox predates the per-user skill view"
                    ):
                        stats["killed_legacy"] += 1
                    return
                # Reverse case: the sandbox still carries a user tag after all
                # user-bound mounts were disabled. Its original volumes remain mounted;
                # generic-pool reuse could expose them to another user, so kill it.
                if kind_tag == "jupyter" and user_tag and not user_bound_enabled:
                    if await self._kill_by_id(
                        mgr,
                        info.id,
                        reason="jupyter sandbox carries user-id tag but user-bound mounts "
                        "are disabled (stale private mounts would cross-user leak)",
                    ):
                        stats["killed_legacy"] += 1
                    return

                # 仍绑在会话上的容器不是无主的，收进空闲池就会被按空闲回收掉——而会话
                # 那边还在用它。绑定活在 session_registry 里（跨进程、比空闲窗口活得
                # 久），重启并不会让它消失，所以这里原样留着：会话下一轮照登记 attach
                # 回来。真没人来接，服务端自己的 TTL 会收走。
                owner_session = await session_registry.bound_session(info.id)
                if owner_session:
                    stats["skipped_bound"] += 1
                    logger.info(
                        "[opensandbox] adopt: sandbox %s still bound to session %s; "
                        "leaving it to that session",
                        info.id,
                        owner_session,
                    )
                    return

                if not await self._track_sandbox(str(info.id)):
                    return  # Another live worker already owns this idle handle.
                try:
                    sbx = await Sandbox.connect(
                        info.id,
                        connection_config=self._make_config(),
                        skip_health_check=(kind_tag == "light"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[opensandbox] adopt: connect %s failed (%s), retaining container",
                        info.id, exc,
                    )
                    await session_registry.untrack(str(info.id), self._tracking_owner_token())
                    self._tracking_ids.discard(str(info.id))
                    return

                # Direct-connect to execd (bypasses the server proxy's ~3s/request overhead); falls back automatically when unreachable.
                await self._repoint_execd_direct(sbx)

                # jupyter + user tag → user-bound pool
                if kind_tag == "jupyter" and user_tag and user_bound_enabled:
                    ok = await self._jupyter_user_pool.adopt(user_tag, sbx)
                    if not ok:
                        await self._destroy_sandbox(sbx)
                        stats["killed_overflow"] += 1
                        return
                    stats["adopted_jupyter"] += 1
                    logger.info(
                        "[opensandbox] adopt: claimed jupyter sandbox %s for user=%s",
                        info.id,
                        user_tag,
                    )
                    return

                # light (or fallback jupyter with all user-bound mounts disabled) → generic pool
                # pool full → kill rather than leak
                bucket = self._pool._buckets[kind_tag]  # noqa: SLF001
                if len(bucket.idle) >= bucket.max_idle:
                    if await self._kill_by_id(
                        mgr,
                        info.id,
                        reason=f"{kind_tag} bucket already at max_idle={bucket.max_idle}",
                    ):
                        stats["killed_overflow"] += 1
                    return
                ok = await self._pool.adopt_idle(kind_tag, sbx)
                if not ok:
                    # adopt_idle refused (race: another entry enqueued concurrently filled the capacity) — destroy
                    await self._destroy_sandbox(sbx)
                    stats["killed_overflow"] += 1
                    return
                stats[f"adopted_{kind_tag}"] += 1
                logger.info(
                    "[opensandbox] adopt: claimed %s sandbox %s",
                    kind_tag,
                    info.id,
                )
                return

            # 3. No tag, but the entrypoint looks like one we previously created (orphan left by an older version) → kill
            if (
                entrypoint in (_LIGHT_ENTRYPOINT, _SETUP_ENTRYPOINT)
                or entrypoint in _LEGACY_ENTRYPOINTS
            ):
                if await self._kill_by_id(
                    mgr,
                    info.id,
                    reason=f"legacy untagged sandbox with our entrypoint {entrypoint}",
                ):
                    stats["killed_legacy"] += 1
                return

            # 4. Someone else's sandbox / unrecognized entrypoint → leave alone
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[opensandbox] adopt: classifying %s failed: %s",
                getattr(info, "id", "?"),
                exc,
            )

    async def _kill_by_id(self, mgr: Any, sandbox_id: str, *, reason: str) -> bool:
        """Startup cleanup uses the same binding and tracker fence as retirement."""
        try:
            async with session_registry.destruction_guard(
                str(sandbox_id), expected_owner=self._tracking_owner_token(),
            ) as allowed:
                if not allowed:
                    logger.info("[opensandbox] startup retained sandbox=%s owned elsewhere", sandbox_id)
                    return False
                logger.info("[opensandbox] kill sandbox %s: %s", sandbox_id, reason)
                await mgr.kill_sandbox(sandbox_id)
                return True
        except Exception as exc:
            logger.warning("[opensandbox] kill sandbox %s failed: %s", sandbox_id, exc)
            return False
