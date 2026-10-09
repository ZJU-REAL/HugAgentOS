"""Background opt-in resource startup, bound to a conversation sandbox lifetime."""
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
import httpx
from fastapi import HTTPException
from sqlalchemy import or_
from core.db import engine
from . import installation, service, store

logger = logging.getLogger(__name__)
_guard = threading.Lock()
_jobs = {}
_closing = {}
_shutdown = False

def candidates(db, user_id):
    from core.capabilities import device_catalog
    if device_catalog.active():
        from core.capabilities.device_plugin_catalog import plugin_entries
        sources = [(entry["slug"], entry["install_id"]) for entry in plugin_entries(user_id)
                   if entry["enabled"] and entry["callable"]]
    else:
        from core.db.models import InstalledPlugin
        sources = [(row.slug, row.install_id) for row in db.query(InstalledPlugin).filter(
            or_(InstalledPlugin.owner_user_id == user_id, InstalledPlugin.owner_user_id.is_(None))).all()]
    for slug, install_id in sources:
        try:
            selected = installation.resolve(db, slug, user_id, install_id)
        except HTTPException:
            continue
        for module in (selected.ui.get("contributes") or {}).get("modules") or []:
            if (module.get("resource") or {}).get("prewarm") is True:
                yield selected, module

def schedule(provider, chat_id, user_id, sandbox_id):
    if not user_id or not chat_id or not sandbox_id or str(chat_id).startswith("eval_"):
        return
    key = (chat_id, user_id, str(sandbox_id))
    loop = asyncio.get_running_loop()
    with _guard:
        if _shutdown or chat_id in _closing or key in _jobs:
            return
        # Bound completed admission markers; router providers notify repeatedly.
        if len(_jobs) >= 1024:
            for old in [old for old, job in _jobs.items() if job.done()][:256]:
                _jobs.pop(old, None)
        _jobs[key] = loop.create_task(_run(provider, chat_id, user_id, str(sandbox_id)))

async def _run(provider, chat_id, user_id, sandbox_id):
    from core.sandbox.bound_session import bind
    with bind(sandbox_id):
        try:
            from core.db.models import ChatSession
            with engine.SessionLocal() as db:
                chat = db.get(ChatSession, chat_id)
                if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
                    return
                for selected, module in candidates(db, user_id):
                    await service.create(db, selected.slug, module["id"], user_id, chat_id,
                                         install_id=selected.install_id, _prewarm=True, _provider=provider)
        except Exception as exc:
            logger.info("Resource prewarm skipped for chat=%s: %s", chat_id, type(exc).__name__)
            return False

async def _cancel(task):
    if not task.done():
        task.cancel()
    result, = await asyncio.gather(task, return_exceptions=True)
    return result is not False and (not isinstance(result, BaseException) or isinstance(result, asyncio.CancelledError))

@asynccontextmanager
async def closing(chat_id):
    with _guard:
        _closing[chat_id] = _closing.get(chat_id, 0) + 1
    try:
        yield
    finally:
        with _guard:
            _closing[chat_id] -= 1
            if _closing[chat_id] == 0:
                _closing.pop(chat_id)

def resume():
    global _shutdown
    with _guard:
        _shutdown = False

async def stop(chat_id=None):
    global _shutdown
    if str(chat_id or "").startswith("eval_"):
        return True
    if chat_id is None:
        with _guard:
            _shutdown = True
    async with closing(chat_id):
        return await _stop(chat_id)

async def _stop(chat_id):
    """Stop starts and blank workers. False forbids returning a dirty sandbox to its pool."""
    with _guard:
        keys = [key for key in _jobs if chat_id is None or key[0] == chat_id]
        jobs = [_jobs.pop(key) for key in keys]
    loop = asyncio.get_running_loop()
    clean = True
    for task in jobs:
        owner = task.get_loop()
        if owner is loop:
            clean = await _cancel(task) and clean
        elif owner.is_running():
            clean = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_cancel(task), owner)) and clean
        elif not task.done():
            clean = False
        else:
            try:
                clean = task.result() is not False and clean
            except asyncio.CancelledError:
                pass
            except Exception:
                clean = False
    with engine.SessionLocal() as db:
        query = db.query(store.PluginResource).filter(store.PluginResource.status.in_(("warm", "warming")))
        if chat_id is not None:
            query = query.filter_by(chat_id=chat_id)
        for row in query.all():
            try:
                target = store.descriptor(row)
            except HTTPException:
                clean = False
                row.status = "closed"
                continue
            if target.get("url"):
                try:
                    async with httpx.AsyncClient(timeout=3, trust_env=False, headers=target["headers"]) as client:
                        response = await client.post(target["url"] + "/shutdown", json={})
                        response.raise_for_status()
                except httpx.HTTPError:
                    clean = False
            row.status = "closed"
        db.commit()
    return clean
