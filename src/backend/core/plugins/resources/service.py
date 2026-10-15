"""Authorized resource lifecycle. Runtime behavior remains package-owned."""

import json
import os
import secrets
import time
import uuid

from core.plugins.ui.contract import find_module
from fastapi import HTTPException

from . import installation, store
from .crypto import encrypt_secret
from .transport import RuntimeTransport


def authorized(db, resource_id, user_id):
    row = store.get(db, resource_id, user_id)
    if row.status != "active" or row.expires_at < time.time():
        raise HTTPException(410, "resource_expired")
    validate_binding(db, row, user_id)
    return row


def validate_binding(db, row, user_id):
    if row.application_key is not None:
        from core.applications import background

        background.authorized_resource(db, row)
    from .owners import resolve_installation, validate

    selected = resolve_installation(db, row, user_id)
    validate(db, row, user_id, selected)
    return row


async def create(
    db,
    slug,
    module_id,
    user_id,
    chat_id,
    *,
    resource_id=None,
    install_id=None,
    checkpoint_id=None,
    _prewarm=False,
    _provider=None,
    _application=None,
):
    if resource_id:
        row = authorized(db, resource_id, user_id)
        if row.chat_id != chat_id or row.slug != slug or row.module_id != module_id:
            raise HTTPException(403, "resource_binding_mismatch")
        await request(row, "/state")
        return store.public(row)
    if _application is None:
        from core.db.models import ChatSession

        chat = db.get(ChatSession, chat_id)
        if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
            raise HTTPException(403, "conversation_unavailable")
    else:
        if chat_id is not None or checkpoint_id or _prewarm:
            raise HTTPException(403, "resource_owner_invalid")
        from .owners import application_binding

        application_binding(
            db,
            user_id,
            _application["application_key"],
            install_id,
            _application["revision"],
            _application["scope"],
            module_id,
        )
    resolver = installation.resolve
    if _application:
        from core.applications.installation_binding import exact as resolver

    selected = resolver(db, slug, user_id, install_id)
    if _application and selected.install_id != install_id:
        raise HTTPException(403, "resource_installation_mismatch")
    if _application and selected.revision != _application["revision"]:
        raise HTTPException(409, "plugin_revision_changed")
    module = find_module(selected.ui, module_id)
    if module is None or not module.get("resource"):
        raise HTTPException(404, "resource_module_unavailable")
    if _application and "resource.attach" not in module.get("grants", []):
        raise HTTPException(403, "resource_attach_not_granted")
    token = secrets.token_urlsafe(32)
    from .configuration import runtime_configuration

    config = {
        **runtime_configuration(module["resource"]),
        "token": token,
        "idle_seconds": int(os.getenv("PLUGIN_RESOURCE_IDLE_SECONDS", "1800")),
    }
    if checkpoint_id:
        checkpoint = db.get(store.PluginResourceCheckpoint, checkpoint_id)
        if (
            checkpoint is None
            or checkpoint.user_id != user_id
            or checkpoint.install_id != selected.install_id
        ):
            raise HTTPException(404, "checkpoint_unavailable")
        from .crypto import decrypt_secret

        state = decrypt_secret(checkpoint.state_enc)
        if state is None:
            raise HTTPException(410, "checkpoint_expired")
        config["checkpoint"] = json.loads(state)
    from .lifecycle import reconcile

    await reconcile(db, user_id)
    from .reservation import adopt, check_quota

    if _application is None:
        row, reused = await adopt(
            db, selected, module_id, user_id, chat_id, prewarm=_prewarm, checkpoint_id=checkpoint_id
        )
    else:
        from .application import reserve

        row, reused = await reserve(db, selected, module_id, user_id, _application)
        if reused:
            return store.public(row)
    if reused:
        if row is None:
            return None
        from core.sandbox.factory import get_sandbox_provider

        provider = _provider or get_sandbox_provider()
        target = store.descriptor(row)
        if (
            target.get("sandbox_id") == await provider.current_sandbox_id(chat_id)
            and target.get("provider") == provider.name
        ):
            try:
                state = await request(row, "/state")
                if not state.get("closed"):
                    alive = True
                else:
                    alive = False
            except HTTPException:
                alive = False
            if alive:
                db.expire_all()
                row = authorized(db, row.resource_id, user_id)
                return store.public(row)
        await close(db, row)
        # A dead warm worker has performed no user actions; reserve a fresh one.
        from .reservation import lock_owner

        lock_owner(db, user_id)
    check_quota(db, user_id)
    from core.config.settings import settings

    row = store.PluginResource(
        resource_id=_application["resource_id"] if _application else uuid.uuid4().hex,
        user_id=user_id,
        chat_id=chat_id,
        application_key=_application["application_key"] if _application else None,
        instance_id=_application["instance_id"] if _application else None,
        request_digest=_application["request_digest"] if _application else None,
        install_id=selected.install_id,
        revision=selected.revision,
        slug=slug,
        module_id=module_id,
        scope=(
            _application["scope"]
            if _application
            else ("local" if settings.deploy.is_local else "cloud")
        ),
        descriptor=encrypt_secret("{}"),
        status="warming" if _prewarm else "starting",
        created_at=time.time(),
        expires_at=time.time() + 90,
    )
    from .owners import runtime_session

    session_id = runtime_session(row)
    db.add(row)
    db.commit()
    from core.sandbox.factory import get_sandbox_provider
    from core.sandbox.interactive import launch

    provider = _provider or get_sandbox_provider()
    try:
        descriptor = await launch(provider, selected, module, user_id, session_id, config)
    except BaseException as exc:
        row.status = "closed"
        db.commit()
        if not isinstance(exc, Exception):
            raise
        code = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc)
            in {
                "chromium_sandbox_unavailable",
                "chromium_runtime_missing",
                "chromium_launch_failed",
                "runtime_process_failed",
                "runtime_start_timeout",
                "runtime_start_failed",
            }
            else type(exc).__name__
        )
        raise HTTPException(503, "resource_runtime_unavailable: " + code) from exc
    try:
        db.expire_all()
        validate_binding(db, row, user_id)
        updated = (
            db.query(store.PluginResource)
            .filter_by(
                resource_id=row.resource_id,
                status="warming" if _prewarm else "starting",
            )
            .update(
                {
                    "descriptor": encrypt_secret(json.dumps(descriptor)),
                    "status": "warm" if _prewarm else "active",
                    "expires_at": time.time()
                    + (
                        90
                        if _application
                        else (
                            300
                            if _prewarm
                            else int(os.getenv("PLUGIN_RESOURCE_TTL_SECONDS", "86400"))
                        )
                    ),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if updated != 1:
            raise HTTPException(410, "resource_start_cancelled")
        db.refresh(row)
        if _application:
            from .deadlines import arm

            await arm(db, row)
    except BaseException:
        db.rollback()
        if _application:
            # Retain the exact handle before cleanup so a failed stop is janitor-retryable.
            row.descriptor = encrypt_secret(json.dumps(descriptor))
            row.status = "closing"
            db.commit()
            try:
                await close(db, row)
            except Exception:
                db.rollback()
        else:
            await provider.write_stdin(
                descriptor["process_id"],
                sandbox_session_id=session_id,
                user_id=user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            row.status = "closed"
            db.commit()
        raise
    from core.applications.diagnostics import observe_resource

    observe_resource(db, row, "resource", "ok")
    return store.public(row)


async def request(row, path, body=None, *, transport=None):
    if transport is not None:
        return await transport.request(path, body)
    async with RuntimeTransport(store.descriptor(row)) as connection:
        return await connection.request(path, body)


async def command(row, payload, *, actor, connection_id="", transport=None):
    body = {**payload, "actor": actor, "connection_id": connection_id}
    if body.get("action") in {"storage_state", "checkpoint"}:
        raise HTTPException(403, "checkpoint_requires_explicit_save")
    return await request(row, "/command", body, transport=transport)


async def checkpoint(db, row, name, connection_id, *, transport=None):
    if row.application_key is not None:
        raise HTTPException(403, "application_checkpoint_not_granted")
    state = await request(
        row,
        "/command",
        {
            "id": uuid.uuid4().hex,
            "actor": "user",
            "connection_id": connection_id,
            "action": "checkpoint",
            "params": {},
        },
        transport=transport,
    )
    record = store.PluginResourceCheckpoint(
        checkpoint_id=uuid.uuid4().hex,
        user_id=row.user_id,
        install_id=row.install_id,
        name=name[:80],
        state_enc=encrypt_secret(json.dumps(state["state"])),
    )
    db.add(record)
    db.commit()
    return {"checkpoint_id": record.checkpoint_id, "name": record.name}


async def close(db, row):
    if row.application_key is not None:
        from .application_cleanup import close

        return await close(db, row)
    from .owners import runtime_session

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
                    await provider.write_stdin(
                        target["process_id"],
                        sandbox_session_id=runtime_session(row),
                        user_id=row.user_id,
                        chars="\x03",
                        yield_time_ms=1000,
                    )
                except Exception:
                    pass
    row.status = "closed"
    db.query(store.PluginResourceTicket).filter_by(resource_id=row.resource_id).delete()
    db.query(store.PluginResourceAssetTicket).filter_by(resource_id=row.resource_id).delete()
    db.commit()
