"""Authorized resource lifecycle. Runtime behavior remains package-owned."""

import asyncio
import json
import os
import secrets
import time
import uuid

from core.harness.registry.package_views import find_module
from fastapi import HTTPException

from core.capabilities.plugins.resources import installation, store
from core.capabilities.plugins.resources.crypto import encrypt_secret
from core.capabilities.plugins.resources.transport import RuntimeTransport
from core.db.threads import run_in_session


def authorized(db, resource_id, user_id):
    row = store.get(db, resource_id, user_id)
    if row.status != "active" or row.expires_at < time.time():
        raise HTTPException(410, "resource_expired")
    validate_binding(db, row, user_id)
    return row


def validate_binding(db, row, user_id):
    if row.application_key is not None:
        from core.harness.registry.resource_owners import resource_owner

        resource_owner().authorize(db, row)
    from core.capabilities.plugins.resources.owners import resolve_installation, validate

    selected = resolve_installation(db, row, user_id)
    validate(db, row, user_id, selected)
    return row


def detached(db, row):
    """``row`` with its columns loaded, detached so it can be read after the session closes."""
    db.refresh(row)
    db.expunge(row)
    return row


def authorized_row(db, resource_id, user_id):
    """``authorized`` for async callers: the row comes back detached."""
    return detached(db, authorized(db, resource_id, user_id))


def _authorized_public(db, resource_id, user_id):
    return store.public(authorized(db, resource_id, user_id))


def _existing(db, resource_id, user_id, chat_id, slug, module_id):
    row = authorized(db, resource_id, user_id)
    if row.chat_id != chat_id or row.slug != slug or row.module_id != module_id:
        raise HTTPException(403, "resource_binding_mismatch")
    return detached(db, row)


def _admission_inputs(db, slug, module_id, user_id, chat_id, install_id, checkpoint_id, prewarm, application):
    """Check the owner, resolve the installation and module, and build the runner config."""
    if application is None:
        from core.db.models import ChatSession

        chat = db.get(ChatSession, chat_id)
        if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
            raise HTTPException(403, "conversation_unavailable")
    else:
        if chat_id is not None or checkpoint_id or prewarm:
            raise HTTPException(403, "resource_owner_invalid")
        from core.capabilities.plugins.resources.owners import application_binding

        application_binding(
            db,
            user_id,
            application["application_key"],
            install_id,
            application["revision"],
            application["scope"],
            module_id,
        )
    resolver = installation.resolve
    if application:
        from core.harness.registry.resource_owners import resource_owner
        resolver = resource_owner().exact_installation

    selected = resolver(db, slug, user_id, install_id)
    if application and selected.install_id != install_id:
        raise HTTPException(403, "resource_installation_mismatch")
    if application and selected.revision != application["revision"]:
        raise HTTPException(409, "plugin_revision_changed")
    module = find_module(selected.ui, module_id)
    if module is None or not module.get("resource"):
        raise HTTPException(404, "resource_module_unavailable")
    if application and "resource.attach" not in module.get("grants", []):
        raise HTTPException(403, "resource_attach_not_granted")
    from core.capabilities.plugins.resources.configuration import runtime_configuration

    config = {
        **runtime_configuration(module["resource"]),
        "token": secrets.token_urlsafe(32),
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
        from core.capabilities.plugins.resources.crypto import decrypt_secret

        state = decrypt_secret(checkpoint.state_enc)
        if state is None:
            raise HTTPException(410, "checkpoint_expired")
        config["checkpoint"] = json.loads(state)
    return selected, module, config


def _reserve(db, selected, module_id, user_id, chat_id, prewarm, application):
    """Insert a starting row under the owner lock the caller already holds."""
    from core.capabilities.plugins.resources.reservation import check_quota
    from core.capabilities.plugins.resources.owners import runtime_session
    from core.config.settings import settings

    check_quota(db, user_id)
    row = store.PluginResource(
        resource_id=application["resource_id"] if application else uuid.uuid4().hex,
        user_id=user_id,
        chat_id=chat_id,
        application_key=application["application_key"] if application else None,
        instance_id=application["instance_id"] if application else None,
        request_digest=application["request_digest"] if application else None,
        install_id=selected.install_id,
        revision=selected.revision,
        slug=selected.slug,
        module_id=module_id,
        scope=(
            application["scope"]
            if application
            else ("local" if settings.deploy.is_local else "cloud")
        ),
        descriptor=encrypt_secret("{}"),
        status="warming" if prewarm else "starting",
        created_at=time.time(),
        expires_at=time.time() + 90,
    )
    session_id = runtime_session(row)
    db.add(row)
    db.commit()
    return detached(db, row), session_id


def _admit(db, selected, module_id, user_id, chat_id, prewarm, checkpoint_id, application, attempt, fresh):
    """One admission attempt under the owner lock.

    Returns ``("adopted", row)`` for a reusable worker, ``("exists", None)`` when
    a prewarm finds one already live, ``("wait", None)`` while another start is
    in flight, or ``("reserved", (row, session_id))`` for a new starting row.
    """
    from core.capabilities.plugins.resources.reservation import adopt_once, lock_owner

    if attempt:
        # Each wait releases the transaction: installation switches and revocation
        # must be noticed before the worker can be adopted.
        if application is None:
            current = installation.resolve(db, selected.slug, user_id, selected.install_id)
            if current.revision != selected.revision:
                raise HTTPException(409, "plugin_revision_changed")
        else:
            from core.capabilities.plugins.resources.owners import application_binding

            application_binding(
                db,
                user_id,
                application["application_key"],
                selected.install_id,
                selected.revision,
                application["scope"],
                module_id,
            )
    if fresh:
        lock_owner(db, user_id)
        outcome, row = "reserve", None
    elif application is None:
        outcome, row = adopt_once(
            db, selected, module_id, user_id, chat_id, prewarm=prewarm, checkpoint_id=checkpoint_id
        )
    else:
        from core.harness.registry.resource_owners import resource_owner

        outcome, row = resource_owner().reserve(db, selected, module_id, user_id, application)
    if outcome == "reserve":
        return "reserved", _reserve(db, selected, module_id, user_id, chat_id, prewarm, application)
    if outcome == "adopted":
        return outcome, detached(db, row)
    return outcome, None


def _mark(db, resource_id, status, descriptor=None):
    row = db.get(store.PluginResource, resource_id)
    row.status = status
    if descriptor is not None:
        # Retain the exact handle before cleanup so a failed stop is janitor-retryable.
        row.descriptor = encrypt_secret(json.dumps(descriptor))
    db.commit()


def _activate(db, resource_id, user_id, descriptor, prewarm, application):
    row = db.get(store.PluginResource, resource_id)
    validate_binding(db, row, user_id)
    updated = (
        db.query(store.PluginResource)
        .filter_by(resource_id=resource_id, status="warming" if prewarm else "starting")
        .update(
            {
                "descriptor": encrypt_secret(json.dumps(descriptor)),
                "status": "warm" if prewarm else "active",
                "expires_at": time.time()
                + (
                    90
                    if application
                    else (
                        300
                        if prewarm
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


def _observe_started(db, resource_id):
    from core.harness.registry.resource_owners import resource_owner

    row = db.get(store.PluginResource, resource_id)
    resource_owner().observe(db, row, "resource", "ok")
    return store.public(row)


async def _worker_alive(row, provider, chat_id):
    target = store.descriptor(row)
    if (
        target.get("sandbox_id") != await provider.current_sandbox_id(chat_id)
        or target.get("provider") != provider.name
    ):
        return False
    try:
        state = await request(row, "/state")
    except HTTPException:
        return False
    return not state.get("closed")


async def create(
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
    """Start (or adopt) a resource worker for a conversation or an application.

    Every database step runs on a session of its own; the owner lock is only
    held inside one step, so no connection waits on a runner or the sandbox.
    """
    if resource_id:
        row = await run_in_session(_existing, resource_id, user_id, chat_id, slug, module_id)
        await request(row, "/state")
        return store.public(row)
    selected, module, config = await run_in_session(
        _admission_inputs, slug, module_id, user_id, chat_id, install_id, checkpoint_id,
        _prewarm, _application,
    )
    from core.capabilities.plugins.resources.lifecycle import reconcile
    from core.sandbox.factory import get_sandbox_provider

    await reconcile(user_id)
    provider = _provider or get_sandbox_provider()
    deadline = time.monotonic() + (95 if _application else 90)
    attempt, fresh = 0, False
    while True:
        outcome, value = await run_in_session(
            _admit, selected, module_id, user_id, chat_id, _prewarm, checkpoint_id,
            _application, attempt, fresh,
        )
        attempt += 1
        if outcome == "exists":
            return None
        if outcome == "reserved":
            break
        if outcome == "adopted":
            if _application is not None:
                return store.public(value)
            if await _worker_alive(value, provider, chat_id):
                return await run_in_session(_authorized_public, value.resource_id, user_id)
            await close(value)
            # A dead warm worker has performed no user actions; reserve a fresh one.
            fresh = True
            continue
        if time.monotonic() >= deadline:
            raise HTTPException(
                503, "resource_start_pending" if _application else "resource_prewarm_timeout"
            )
        await asyncio.sleep(0.05)
    row, session_id = value
    from core.sandbox.processes.interactive import launch

    try:
        descriptor = await launch(provider, selected, module, user_id, session_id, config)
    except BaseException as exc:
        await run_in_session(_mark, row.resource_id, "closed")
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
        await run_in_session(_activate, row.resource_id, user_id, descriptor, _prewarm, _application)
        if _application:
            from core.capabilities.plugins.resources.deadlines import arm

            await arm(row.resource_id)
    except BaseException:
        if _application:
            await run_in_session(_mark, row.resource_id, "closing", descriptor)
            try:
                await close(row)
            except Exception:
                pass
        else:
            await provider.write_stdin(
                descriptor["process_id"],
                sandbox_session_id=session_id,
                user_id=user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            await run_in_session(_mark, row.resource_id, "closed")
        raise
    return await run_in_session(_observe_started, row.resource_id)


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


def _save_checkpoint(db, user_id, install_id, name, state):
    record = store.PluginResourceCheckpoint(
        checkpoint_id=uuid.uuid4().hex,
        user_id=user_id,
        install_id=install_id,
        name=name[:80],
        state_enc=encrypt_secret(json.dumps(state)),
    )
    db.add(record)
    db.commit()
    return {"checkpoint_id": record.checkpoint_id, "name": record.name}


async def checkpoint(row, name, connection_id, *, transport=None):
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
    return await run_in_session(_save_checkpoint, row.user_id, row.install_id, name, state["state"])


def _mark_closed(db, resource_id):
    db.query(store.PluginResource).filter_by(resource_id=resource_id).update(
        {"status": "closed"}, synchronize_session=False
    )
    db.query(store.PluginResourceTicket).filter_by(resource_id=resource_id).delete()
    db.query(store.PluginResourceAssetTicket).filter_by(resource_id=resource_id).delete()
    db.commit()


async def close(row):
    """Stop a resource worker, then retire its row and tickets.

    ``row`` only supplies the worker's coordinates; the database work runs on a
    session of its own after the network calls, so callers never hold a
    connection across the shutdown.
    """
    if row.application_key is not None:
        from core.harness.registry.resource_owners import resource_owner

        return await resource_owner().close(row)
    from core.capabilities.plugins.resources.owners import runtime_session

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
                except Exception as fallback_error:
                    from core.infra.failures import record_best_effort_failure
                    record_best_effort_failure('core.capabilities.plugins.resources.service:close', fallback_error)
    await run_in_session(_mark_closed, row.resource_id)
