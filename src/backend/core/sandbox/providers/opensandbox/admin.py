"""Read-only OpenSandbox instance and pool queries."""

from __future__ import annotations

from typing import Any, Optional

import core.sandbox.sessions.registry as session_registry
from core.config.settings import settings
from core.sandbox.processes.dispatch import _on_service_loop
from core.sandbox.protocol import SandboxAdminCapabilities, SandboxInfo
from core.sandbox.providers.opensandbox.volumes import (
    _METADATA_OWNER_KEY,
    _METADATA_OWNER_VALUE,
    _METADATA_POOL_KIND_KEY,
    _METADATA_USER_ID_KEY,
)


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
    async def admin_list_sandboxes(self) -> list[SandboxInfo]:
        """Controller inventory is authoritative across all backend workers.

        Local handles may outlive their containers. They are never added to this
        list. A failed controller query raises rather than reporting a false zero.
        """
        out: list[SandboxInfo] = []
        async for _mgr, info in self._iter_server_sandbox_infos():
            metadata = info.metadata or {}
            owner = metadata.get(_METADATA_OWNER_KEY)
            owned = owner == _METADATA_OWNER_VALUE
            state = (getattr(getattr(info, "status", None), "state", "") or "").lower()
            if state != "running":
                continue
            sandbox_id = str(info.id)
            bound = await session_registry.bound_session(sandbox_id) if owned else None
            tracker = await session_registry.tracked_owner(sandbox_id) if owned else None
            managed = owned and bool(bound or tracker)
            out.append(SandboxInfo(
                sandbox_id=sandbox_id,
                session_id=bound,
                user_id=metadata.get(_METADATA_USER_ID_KEY),
                pool_kind=metadata.get(_METADATA_POOL_KIND_KEY),
                state="pooled-idle" if tracker and not bound else state,
                owner_tag=owner,
                backend_managed=managed,
                ownership=None if managed else ("self-orphan" if owned else ("external" if owner else "untagged")),
                extra={"inventory_source": "controller"},
            ))
        return out


    async def _iter_server_sandbox_infos(self) -> Any:
        """Page through **all** sandboxes on the OpenSandbox server, ``yield (mgr, info)``
        one at a time.

        Centralizes the scaffolding of "create SandboxManager → paginate
        list_sandbox_infos → in-page dedupe → page-limit defense → finally close mgr",
        Shared by startup adoption and the authoritative read-only inventory.
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
                    from core.sandbox.errors import SandboxError
                    raise SandboxError("controller inventory exceeded the pagination safety limit")
        finally:
            try:
                await mgr.close()
            except Exception as fallback_error:  # noqa: BLE001
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.sandbox.providers.opensandbox.admin:_iter_server_sandbox_infos', fallback_error)

    @_on_service_loop
    async def admin_get_sandbox(self, sandbox_id: str) -> Optional[SandboxInfo]:
        instances = await self.admin_list_sandboxes()
        return next((item for item in instances if item.sandbox_id == sandbox_id), None)

    def admin_pool_stats(self) -> dict:
        import os

        out: dict = {"scope": "worker", "worker_pid": os.getpid()}
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
