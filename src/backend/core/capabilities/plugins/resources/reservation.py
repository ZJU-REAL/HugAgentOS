"""Serialize resource admission and one-time warm worker adoption per owner."""

import os
import time

from fastapi import HTTPException

from core.capabilities.plugins.resources import store

LIVE_STATUSES = ("active", "starting", "warming", "warm")


def lock_owner(db, user_id):
    from core.db.models import UserShadow

    if db.bind.dialect.name == "sqlite":
        from sqlalchemy import text

        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.query(UserShadow).filter_by(user_id=user_id).with_for_update().one()


def adopt_once(db, selected, module_id, user_id, chat_id, *, prewarm, checkpoint_id):
    """Take the owner lock and look for a warm worker to adopt.

    Returns ``("adopted", row)``, ``("exists", None)`` (prewarm: one is already
    live), ``("wait", None)`` (one is still warming; the lock is released), or
    ``("reserve", None)`` — permission to reserve under the lock still held.
    """
    lock_owner(db, user_id)
    query = (
        db.query(store.PluginResource)
        .filter_by(
            user_id=user_id,
            chat_id=chat_id,
            install_id=selected.install_id,
            revision=selected.revision,
            slug=selected.slug,
            module_id=module_id,
        )
        .filter(store.PluginResource.expires_at > time.time())
    )
    if prewarm:
        if query.filter(store.PluginResource.status.in_(LIVE_STATUSES)).first():
            db.commit()
            return "exists", None
        return "reserve", None
    row = (
        None
        if checkpoint_id
        else query.filter(store.PluginResource.status.in_(("warm", "warming")))
        .order_by(store.PluginResource.created_at)
        .first()
    )
    if row is None:
        return "reserve", None
    if row.status == "warm":
        row.status = "active"
        row.expires_at = time.time() + int(os.getenv("PLUGIN_RESOURCE_TTL_SECONDS", "86400"))
        db.commit()
        return "adopted", row
    db.commit()
    return "wait", None


def check_quota(db, user_id):
    active = (
        db.query(store.PluginResource)
        .filter(
            store.PluginResource.user_id == user_id,
            (
                (
                    store.PluginResource.status.in_(LIVE_STATUSES)
                    & (store.PluginResource.expires_at > time.time())
                )
                | (store.PluginResource.status == "closing")
            ),
        )
        .count()
    )
    if active >= int(os.getenv("PLUGIN_RESOURCE_MAX_PER_USER", "8")):
        db.rollback()
        raise HTTPException(429, "resource_limit")
