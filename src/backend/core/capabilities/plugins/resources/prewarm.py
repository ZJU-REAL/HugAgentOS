"""Background opt-in resource startup, bound to a conversation sandbox lifetime."""
from __future__ import annotations
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import httpx

from core.db.threads import run_in_session
from fastapi import HTTPException
from sqlalchemy import or_
from core.capabilities.plugins.resources import installation, service, store

logger = logging.getLogger(__name__)


def candidates(db, user_id):
    from core.harness.registry import device_catalog
    if device_catalog.active():
        from core.harness.registry.device_catalog import plugin_entries
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
    with process_state().guard:
        if process_state().shutdown or chat_id in process_state().closing or key in process_state().jobs:
            return
        # Bound completed admission markers; router providers notify repeatedly.
        if len(process_state().jobs) >= 1024:
            for old in [old for old, job in process_state().jobs.items() if job.done()][:256]:
                process_state().jobs.pop(old, None)
        process_state().jobs[key] = loop.create_task(_run(provider, chat_id, user_id, str(sandbox_id)))

def _prewarm_targets(db, chat_id, user_id):
    """``(slug, module_id, install_id)`` of every prewarmed module the chat's owner has."""
    from core.db.models import ChatSession

    chat = db.get(ChatSession, chat_id)
    if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
        return []
    return [(selected.slug, module["id"], selected.install_id) for selected, module in candidates(db, user_id)]


async def _run(provider, chat_id, user_id, sandbox_id):
    from core.sandbox.sessions.binding import bind
    with bind(sandbox_id):
        try:
            for slug, module_id, install_id in await run_in_session(_prewarm_targets, chat_id, user_id):
                await service.create(slug, module_id, user_id, chat_id,
                                     install_id=install_id, _prewarm=True, _provider=provider)
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
    with process_state().guard:
        process_state().closing[chat_id] = process_state().closing.get(chat_id, 0) + 1
    try:
        yield
    finally:
        with process_state().guard:
            process_state().closing[chat_id] -= 1
            if process_state().closing[chat_id] == 0:
                process_state().closing.pop(chat_id)

def resume():
    pass
    with process_state().guard:
        process_state().shutdown = False

async def stop(chat_id=None):
    pass
    if str(chat_id or "").startswith("eval_"):
        return True
    if chat_id is None:
        with process_state().guard:
            process_state().shutdown = True
    async with closing(chat_id):
        return await _stop(chat_id)

def _warm_targets(db, chat_id):
    """``(resource_id, target)`` of every blank worker; target None when unreadable."""
    query = db.query(store.PluginResource).filter(store.PluginResource.status.in_(("warm", "warming")))
    if chat_id is not None:
        query = query.filter_by(chat_id=chat_id)
    targets = []
    for row in query.all():
        try:
            targets.append((row.resource_id, store.descriptor(row)))
        except HTTPException:
            targets.append((row.resource_id, None))
    return targets


def _close_rows(db, resource_ids):
    if resource_ids:
        db.query(store.PluginResource).filter(
            store.PluginResource.resource_id.in_(resource_ids)
        ).update({"status": "closed"}, synchronize_session=False)
        db.commit()


async def _stop(chat_id):
    """Stop starts and blank workers. False forbids returning a dirty sandbox to its pool."""
    with process_state().guard:
        keys = [key for key in process_state().jobs if chat_id is None or key[0] == chat_id]
        jobs = [process_state().jobs.pop(key) for key in keys]
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
    targets = await run_in_session(_warm_targets, chat_id)
    for resource_id, target in targets:
        if target is None:
            clean = False
            continue
        if target.get("url"):
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False, headers=target["headers"]) as client:
                    response = await client.post(target["url"] + "/shutdown", json={})
                    response.raise_for_status()
            except httpx.HTTPError:
                clean = False
    await run_in_session(_close_rows, [resource_id for resource_id, _ in targets])
    return clean


@dataclass
class PrewarmState:
    guard: object = field(default_factory=lambda: threading.Lock())
    jobs: object = field(default_factory=lambda: {})
    closing: object = field(default_factory=lambda: {})
    shutdown: object = False


_STATE = PrewarmState()


def process_state() -> PrewarmState:
    return _STATE
