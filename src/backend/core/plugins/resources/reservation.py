"""Serialize resource admission and one-time warm worker adoption per owner."""
import asyncio
import os
import time
from fastapi import HTTPException
from . import store

LIVE_STATUSES = ("active", "starting", "warming", "warm")

def lock_owner(db, user_id):
    from core.db.models import UserShadow
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.query(UserShadow).filter_by(user_id=user_id).with_for_update().one()

async def adopt(db, selected, module_id, user_id, chat_id, *, prewarm, checkpoint_id):
    """Return an adopted resource, or permission to reserve under the held lock."""
    deadline = time.monotonic() + 90
    while True:
        db.commit()
        lock_owner(db, user_id)
        query = db.query(store.PluginResource).filter_by(
            user_id=user_id, chat_id=chat_id, install_id=selected.install_id,
            revision=selected.revision, slug=selected.slug, module_id=module_id,
        ).filter(store.PluginResource.expires_at > time.time())
        if prewarm:
            if query.filter(store.PluginResource.status.in_(LIVE_STATUSES)).first():
                db.commit()
                return None, True
            return None, False
        row = None if checkpoint_id else query.filter(
            store.PluginResource.status.in_(("warm", "warming"))
        ).order_by(store.PluginResource.created_at).first()
        if row is None:
            return None, False
        if row.status == "warm":
            row.status = "active"
            row.expires_at = time.time() + int(os.getenv("PLUGIN_RESOURCE_TTL_SECONDS", "86400"))
            db.commit()
            return row, True
        db.commit()
        if time.monotonic() >= deadline:
            raise HTTPException(503, "resource_prewarm_timeout")
        await asyncio.sleep(0.05)
        db.expire_all()
        # Each wait releases the transaction: installation switches and revocation
        # must be noticed before the worker can be adopted.
        from . import installation
        current = installation.resolve(db, selected.slug, user_id, selected.install_id)
        if current.revision != selected.revision:
            raise HTTPException(409, "plugin_revision_changed")

def check_quota(db, user_id):
    active = db.query(store.PluginResource).filter(
        store.PluginResource.user_id == user_id,
        store.PluginResource.status.in_(LIVE_STATUSES),
        store.PluginResource.expires_at > time.time(),
    ).count()
    if active >= int(os.getenv("PLUGIN_RESOURCE_MAX_PER_USER", "8")):
        db.rollback()
        raise HTTPException(429, "resource_limit")
