"""Serialize file access, sandbox creation and permission changes per account."""
from __future__ import annotations
from contextlib import contextmanager, asynccontextmanager
import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
import hashlib
import threading

from fastapi import HTTPException
from sqlalchemy import text


@contextmanager
def space_lock(identity, *, engine=None):
    if engine is None:
        from core.db.engine import engine
    key = int.from_bytes(hashlib.sha256(("space:" + identity).encode()).digest()[:8],
                         "big", signed=True)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            yield
    else:
        with process_state().local_guard:
            lock = process_state().local_locks.setdefault((str(engine.url), key), threading.Lock())
        if not lock.acquire(timeout=30):
            raise HTTPException(503, "文件权限正在更新，请重试")
        try:
            yield
        finally:
            lock.release()


def revoke_sandboxes(actor):
    from core.config.settings import settings
    from core.files.edition import revoke_computers
    revoke_computers(actor)
    if settings.sandbox.provider != "opensandbox":
        return
    from opensandbox.config.connection_sync import ConnectionConfigSync
    from opensandbox.models.sandboxes import SandboxFilter
    from opensandbox.sync.manager import SandboxManagerSync
    manager = SandboxManagerSync.create(ConnectionConfigSync(
        domain=settings.sandbox.opensandbox_domain,
        api_key=settings.sandbox.opensandbox_api_key or None,
        request_timeout=timedelta(seconds=30),
    ))
    try:
        ids = set()
        page = 1
        while True:
            result = manager.list_sandbox_infos(SandboxFilter(
                metadata={"hugagent-owner": "backend", "hugagent-user-id": actor},
                page=page, page_size=200,
            ))
            for info in result.sandbox_infos:
                metadata = info.metadata or {}
                if metadata.get("hugagent-owner") != "backend" or metadata.get("hugagent-user-id") != actor:
                    raise RuntimeError("Controller returned an unrelated sandbox")
                ids.add(info.id)
            if not result.pagination.has_next_page:
                break
            page += 1
        for sandbox_id in ids:
            try:
                manager.kill_sandbox(sandbox_id)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
    except Exception as exc:
        raise HTTPException(503, "无法关闭已有文件挂载，权限未更新") from exc
    finally:
        manager.close()


def authorized_operation(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with space_lock(self.actor, engine=self.db.get_bind()):
            self.db.expire_all()
            return method(self, *args, **kwargs)
    return run


@asynccontextmanager
async def async_space_lock(identity):
    guard = space_lock(identity)
    acquisition = asyncio.create_task(asyncio.to_thread(guard.__enter__))
    try:
        await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        await acquisition
        await asyncio.to_thread(guard.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.to_thread(guard.__exit__, None, None, None)


@dataclass
class LeasesState:
    local_guard: object = field(default_factory=lambda: threading.Lock())
    local_locks: object = field(default_factory=lambda: {})


_STATE = LeasesState()


def process_state() -> LeasesState:
    return _STATE
