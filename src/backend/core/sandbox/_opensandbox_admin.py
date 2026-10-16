"""Read-only OpenSandbox instance and pool queries."""

from __future__ import annotations

import time
from typing import Any, Optional

from core.config.settings import settings
from core.sandbox._opensandbox_internals import (
    _METADATA_OWNER_KEY,
    _METADATA_OWNER_VALUE,
    _METADATA_POOL_KIND_KEY,
    _METADATA_USER_ID_KEY,
    logger,
)

from . import session_registry
from ._opensandbox_loop import _on_service_loop
from .protocol import SandboxAdminCapabilities, SandboxInfo


class _OpenSandboxAdminMixin:
    def admin_capabilities(self) -> SandboxAdminCapabilities:
        return SandboxAdminCapabilities(
            provider=self.name,
            can_list=True,
            can_inspect=True,
            can_pool_stats=True,
            can_snapshots=settings.sandbox.opensandbox_snapshot_enabled,
        )

    @_on_service_loop
    async def admin_list_sandboxes(self, include_server: bool = False) -> list[SandboxInfo]:
        now = time.monotonic()
        out: list[SandboxInfo] = []
        known_ids: set[str] = (
            set()
        )  # sandbox_ids already managed in backend memory; used to dedupe the server-side enumeration
        # 1) Active chat-bound sessions (executing, or just finished and not yet returned)
        for sid, sess in list(self._sessions.items()):
            try:
                sbx_id = getattr(getattr(sess, "sandbox", None), "id", None) or "?"
                known_ids.add(sbx_id)
                out.append(
                    SandboxInfo(
                        sandbox_id=sbx_id,
                        session_id=sid,
                        user_id=getattr(sess, "user_id", None),
                        pool_kind=getattr(sess, "pool_source", None),
                        state="stale" if getattr(sess, "stale_marked", False) else "active",
                        idle_seconds=max(0.0, now - getattr(sess, "last_active_ts", now)),
                        backend_managed=True,
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        # 2) Idle sandboxes in the warm pool — otherwise the panel is always empty when
        #    no chat is executing, contradicting "the pool clearly has warm sandboxes".
        #    state=pooled-idle means "an idle pre-warmed instance from the backend
        #    memory's viewpoint"; note that when the OpenSandbox server reclaims by TTL
        #    while the generic pool does not renew, this may contain dead references
        #    (see the known pool-desync issue).
        try:
            for kind, sbx in self._pool.idle_sandboxes():
                sbx_id = getattr(sbx, "id", None) or "?"
                known_ids.add(sbx_id)
                out.append(
                    SandboxInfo(
                        sandbox_id=sbx_id,
                        session_id=None,
                        user_id=None,
                        pool_kind=kind,
                        state="pooled-idle",
                        idle_seconds=None,
                        backend_managed=True,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        # 3) Per-user jupyter idle pool (warm sandboxes reused by the same user across chats)
        try:
            for uid, sbx, release_ts in self._jupyter_user_pool.idle_sandboxes():
                sbx_id = getattr(sbx, "id", None) or "?"
                known_ids.add(sbx_id)
                out.append(
                    SandboxInfo(
                        sandbox_id=sbx_id,
                        session_id=None,
                        user_id=uid,
                        pool_kind="jupyter-user",
                        state="pooled-idle",
                        idle_seconds=(max(0.0, now - release_ts) if release_ts else None),
                        backend_managed=True,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        # 4) Sandboxes that exist on the OpenSandbox server but are not tracked in
        #    backend memory — i.e. stray instances that are "still alive in the
        #    background, not yet killed" (orphans from a restart window / leftovers
        #    from pool desync / occupied by another client). The security admin panel
        #    uses this to detect leaks. Pure query; failure does not affect the managed
        #    instances already collected. This network enumeration only happens when
        #    include_server is explicitly set (the overview count uses the default False).
        if include_server:
            try:
                out.extend(await self._admin_list_server_extras(known_ids))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[opensandbox] admin list server extras failed: %s", exc)
        return out

    async def _admin_list_server_extras(self, known_ids: set[str]) -> list[SandboxInfo]:
        """Enumerate sandboxes that exist on the OpenSandbox server but are not tracked
        by this process's registry (_sessions + the two idle pools), returned as stray
        instances with ``backend_managed=False``.

        Ownership classification (written to ``owner_tag`` + ``ownership``):
        - ``hugagent-owner == backend``      → ``self-orphan`` (an orphan we left behind)
        - any other owner tag               → ``external`` (another client's sandbox)
        - no owner tag                      → ``untagged``

        ``session_id`` 取自 session_registry 的反向索引：本进程没服务过、但仍绑在某个
        会话上的容器不是泄漏，别让面板把它当孤儿看。

        Pure query, zero side effects: only reads the server list, never kill/connect.
        """
        extras: list[SandboxInfo] = []
        async for _mgr, info in self._iter_server_sandbox_infos():
            sid = getattr(info, "id", None)
            if not sid or sid in known_ids:
                continue  # no id, or already shown in the "managed" list; avoid duplicates
            metadata = info.metadata or {}
            owner_tag = metadata.get(_METADATA_OWNER_KEY)
            if owner_tag == _METADATA_OWNER_VALUE:
                ownership = "self-orphan"
            elif owner_tag:
                ownership = "external"
            else:
                ownership = "untagged"
            server_state = (
                getattr(getattr(info, "status", None), "state", None) or "unknown"
            ).lower()
            extras.append(
                SandboxInfo(
                    sandbox_id=sid,
                    session_id=await session_registry.bound_session(sid),
                    user_id=metadata.get(_METADATA_USER_ID_KEY),
                    pool_kind=metadata.get(_METADATA_POOL_KIND_KEY),
                    state=server_state,
                    idle_seconds=None,
                    owner_tag=owner_tag,
                    backend_managed=False,
                    ownership=ownership,
                )
            )
        return extras

    async def _iter_server_sandbox_infos(self) -> Any:
        """Page through **all** sandboxes on the OpenSandbox server, ``yield (mgr, info)``
        one at a time.

        Centralizes the scaffolding of "create SandboxManager → paginate
        list_sandbox_infos → in-page dedupe → page-limit defense → finally close mgr",
        shared by ``_adopt_existing_sandboxes`` (adopt/kill, needs the mgr) and
        ``_admin_list_server_extras`` (read-only listing).
        """
        from opensandbox import SandboxManager
        from opensandbox.models.sandboxes import SandboxFilter

        mgr = await SandboxManager.create(self._make_config())
        try:
            page = 1
            seen: set[str] = set()
            while True:
                res = await mgr.list_sandbox_infos(SandboxFilter(page_size=200, page=page))
                if not res.sandbox_infos:
                    break
                for info in res.sandbox_infos:
                    sid = getattr(info, "id", None)
                    if sid and sid in seen:
                        continue
                    if sid:
                        seen.add(sid)
                    yield mgr, info
                page += 1
                if (
                    page > 10
                ):  # defense: there can't possibly be 2000 sandboxes; hitting the limit means the server is misbehaving
                    logger.warning("[opensandbox] list: aborting at page=%d", page)
                    break
        finally:
            try:
                await mgr.close()
            except Exception:  # noqa: BLE001
                pass

    @_on_service_loop
    async def admin_get_sandbox(self, sandbox_id: str) -> Optional[SandboxInfo]:
        now = time.monotonic()
        for sid, sess in list(self._sessions.items()):
            try:
                sbx_id = getattr(getattr(sess, "sandbox", None), "id", None) or "?"
                if sbx_id != sandbox_id:
                    continue
                contexts = getattr(sess, "contexts", {}) or {}
                last_renew = getattr(sess, "last_renew_ts", 0) or 0
                extra = {
                    "consecutive_renew_failures": getattr(sess, "consecutive_renew_failures", None),
                    "setup_done": getattr(sess, "setup_done", None),
                    "languages": sorted(contexts.keys()),
                    "last_renew_seconds_ago": (max(0.0, now - last_renew) if last_renew else None),
                }
                return SandboxInfo(
                    sandbox_id=sbx_id,
                    session_id=sid,
                    user_id=getattr(sess, "user_id", None),
                    pool_kind=getattr(sess, "pool_source", None),
                    state="stale" if getattr(sess, "stale_marked", False) else "active",
                    idle_seconds=max(0.0, now - getattr(sess, "last_active_ts", now)),
                    backend_managed=True,
                    extra=extra,
                )
            except Exception:  # noqa: BLE001
                continue
        return None

    def admin_pool_stats(self) -> dict:
        out: dict = {}
        try:
            out["general"] = self._pool.stats()
        except Exception:  # noqa: BLE001
            out["general"] = {}
        try:
            out["jupyter_user"] = self._jupyter_user_pool.stats()
        except Exception:  # noqa: BLE001
            out["jupyter_user"] = {}
        out["sessions"] = len(self._sessions)
        return out
