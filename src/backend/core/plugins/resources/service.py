"""Authorized resource lifecycle. Runtime behavior remains package-owned."""
import json
import os
import secrets
import time
import uuid
import httpx
from fastapi import HTTPException
from .crypto import encrypt_secret
from core.plugins.ui.contract import find_module
from . import installation, store

def authorized(db, resource_id, user_id):
    row = store.get(db, resource_id, user_id)
    if row.status != "active" or row.expires_at < time.time():
        raise HTTPException(410, "resource_expired")
    selected = installation.resolve(db, row.slug, user_id, row.install_id)
    if selected.revision != row.revision:
        raise HTTPException(409, "plugin_revision_changed")
    from core.db.models import ChatSession
    chat = db.get(ChatSession, row.chat_id)
    if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
        raise HTTPException(403, "conversation_unavailable")
    return row

async def create(db, slug, module_id, user_id, chat_id, *, resource_id=None, install_id=None, checkpoint_id=None):
    if resource_id:
        row = authorized(db, resource_id, user_id)
        if row.chat_id != chat_id or row.slug != slug or row.module_id != module_id:
            raise HTTPException(403, "resource_binding_mismatch")
        await request(row, "/state")
        return store.public(row)
    from core.db.models import ChatSession
    chat = db.get(ChatSession, chat_id)
    if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
        raise HTTPException(403, "conversation_unavailable")
    selected = installation.resolve(db, slug, user_id, install_id)
    module = find_module(selected.ui, module_id)
    if module is None or not module.get("resource"):
        raise HTTPException(404, "resource_module_unavailable")
    token = secrets.token_urlsafe(32)
    from .configuration import runtime_configuration
    config = {**runtime_configuration(module["resource"]), "token": token,
              "idle_seconds": int(os.getenv("PLUGIN_RESOURCE_IDLE_SECONDS", "1800"))}
    if checkpoint_id:
        checkpoint = db.get(store.PluginResourceCheckpoint, checkpoint_id)
        if checkpoint is None or checkpoint.user_id != user_id or checkpoint.install_id != selected.install_id:
            raise HTTPException(404, "checkpoint_unavailable")
        from .crypto import decrypt_secret
        state = decrypt_secret(checkpoint.state_enc)
        if state is None:
            raise HTTPException(410, "checkpoint_expired")
        config["checkpoint"] = json.loads(state)
    from .lifecycle import reconcile
    await reconcile(db, user_id)
    from core.db.models import UserShadow
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.query(UserShadow).filter_by(user_id=user_id).with_for_update().one()
    active = db.query(store.PluginResource).filter(
        store.PluginResource.user_id == user_id,
        store.PluginResource.status.in_(["active", "starting"]),
        store.PluginResource.expires_at > time.time(),
    ).count()
    if active >= int(os.getenv("PLUGIN_RESOURCE_MAX_PER_USER", "8")):
        db.rollback()
        raise HTTPException(429, "resource_limit")
    from core.config.settings import settings
    row = store.PluginResource(
        resource_id=uuid.uuid4().hex, user_id=user_id, chat_id=chat_id,
        install_id=selected.install_id, revision=selected.revision, slug=slug,
        module_id=module_id, scope="local" if settings.deploy.is_local else "cloud",
        descriptor=encrypt_secret("{}"), status="starting",
        created_at=time.time(), expires_at=time.time() + 90,
    )
    db.add(row)
    db.commit()
    from core.sandbox.factory import get_sandbox_provider
    from core.sandbox.interactive import launch
    provider = get_sandbox_provider()
    try:
        descriptor = await launch(provider, selected, module, user_id, chat_id, config)
    except BaseException as exc:
        row.status = "closed"
        db.commit()
        if not isinstance(exc, Exception):
            raise
        raise HTTPException(503, "resource_runtime_unavailable: " + type(exc).__name__) from exc
    row.descriptor = encrypt_secret(json.dumps(descriptor))
    row.status = "active"
    row.expires_at = time.time() + int(os.getenv("PLUGIN_RESOURCE_TTL_SECONDS", "86400"))
    try:
        db.add(row)
        db.commit()
    except BaseException:
        await provider.write_stdin(descriptor["process_id"], sandbox_session_id=chat_id, user_id=user_id, chars="\x03", yield_time_ms=1000)
        raise
    return store.public(row)

async def request(row, path, body=None):
    target = store.descriptor(row)
    try:
        async with httpx.AsyncClient(timeout=40, trust_env=False, headers=target["headers"]) as client:
            response = await client.get(target["url"] + path) if body is None else await client.post(target["url"] + path, json=body)
            if response.status_code >= 400:
                detail = response.json().get("detail", "runtime_request_failed")
                raise HTTPException(response.status_code, detail)
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "runtime_disconnected_result_unknown") from exc

async def command(row, payload, *, actor, connection_id=""):
    body = {**payload, "actor": actor, "connection_id": connection_id}
    if body.get("action") in {"storage_state", "checkpoint"}:
        raise HTTPException(403, "checkpoint_requires_explicit_save")
    return await request(row, "/command", body)

async def checkpoint(db, row, name, connection_id):
    state = await request(row, "/command", {"id": uuid.uuid4().hex, "actor": "user", "connection_id": connection_id, "action": "checkpoint", "params": {}})
    record = store.PluginResourceCheckpoint(checkpoint_id=uuid.uuid4().hex, user_id=row.user_id, install_id=row.install_id, name=name[:80], state_enc=encrypt_secret(json.dumps(state["state"])))
    db.add(record)
    db.commit()
    return {"checkpoint_id": record.checkpoint_id, "name": record.name}

async def close(db, row):
    try:
        target = store.descriptor(row)
    except HTTPException:
        target = {}
    if target.get("url") and target.get("headers"):
        try:
            await request(row, "/shutdown", {})
        except HTTPException:
            from core.sandbox.factory import get_sandbox_provider
            provider = get_sandbox_provider()
            if target.get("provider") == provider.name and target.get("process_id"):
                try:
                    await provider.write_stdin(target["process_id"], sandbox_session_id=row.chat_id, user_id=row.user_id, chars="\x03", yield_time_ms=1000)
                except Exception:
                    pass
    row.status = "closed"
    db.query(store.PluginResourceTicket).filter_by(resource_id=row.resource_id).delete()
    db.query(store.PluginResourceAssetTicket).filter_by(resource_id=row.resource_id).delete()
    db.commit()
