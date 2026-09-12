"""Local durable change capture and independently scheduled, bounded cloud delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import random

from core.db.models import (
    ChatMessage,
    ChatRun,
    ChatSession,
    HarnessUsageAttempt,
    SkillCallLog,
    SubAgentCallLog,
    ToolCallLog,
    UserShadow,
)
from core.db.models.observability import DesktopOutbox, utcnow
from core.services.desktop_observability import FIELDS, insert_for, key, public_payload
from sqlalchemy import event, func, or_, select, update

logger = logging.getLogger(__name__)
SOURCES = {
    "session": (ChatSession, "chat_id"),
    "message": (ChatMessage, "message_id"),
    "run": (ChatRun, "run_id"),
    "tool": (ToolCallLog, "id"),
    "skill": (SkillCallLog, "id"),
    "agent": (SubAgentCallLog, "id"),
    "usage": (HarnessUsageAttempt, "attempt_id"),
}
capture_errors = 0


def scope(identity):
    return (
        DesktopOutbox.cloud_base == identity["cloud_base"],
        DesktopOutbox.subject == identity["subject"],
        DesktopOutbox.device_id == identity["device_id"],
    )


def local_owner(db, identity):
    return db.scalar(
        select(UserShadow.user_id).where(
            UserShadow.user_center_id == identity["shell_user_center_id"]
        )
    )


def owner_of(db, row):
    owner = getattr(row, "user_id", None)
    if owner:
        return owner
    chat_id = getattr(row, "chat_id", None)
    if chat_id:
        return db.scalar(select(ChatSession.user_id).where(ChatSession.chat_id == chat_id))
    run_id = getattr(row, "run_id", None)
    if run_id:
        return db.scalar(select(ChatRun.user_id).where(ChatRun.run_id == run_id))
    return None


def enqueue(db, identity, local_user_id, kind, oid, *, deleted=False, initial=False):
    values = dict(
        id=key(identity["cloud_base"], identity["subject"], identity["device_id"], kind, oid),
        cloud_base=identity["cloud_base"],
        subject=identity["subject"],
        device_id=identity["device_id"],
        local_user_id=local_user_id,
        kind=kind,
        object_id=oid,
        revision=1,
        pending=True,
        deleted=deleted,
        digest="",
        updated_at=utcnow(),
    )
    stmt = insert_for(db, DesktopOutbox).values(**values)
    if initial:
        stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "revision": DesktopOutbox.revision + 1,
                "pending": True,
                "deleted": deleted,
                "updated_at": utcnow(),
            },
        )
    db.execute(stmt)


def install_capture(factory, identity_provider):
    """One short reference write in the source transaction; no payload serialization or networking."""

    identity_key = "desktop_observability_identity"

    def snapshot_identity(db, transaction):
        if transaction.parent is None:
            # Session transactions begin before connection checkout or SQL.
            # Never acquire the account lock after a database write: capability
            # publication deliberately holds that lock while committing its index.
            identity = identity_provider()
            db.info[identity_key] = dict(identity) if identity else None

    def forget_identity(db, transaction):
        if transaction.parent is None:
            db.info.pop(identity_key, None)

    def transaction_identity(db):
        if not db.in_transaction():
            db.begin()
        return db.info.get(identity_key)

    def capture(db, _context):
        global capture_errors
        changes = [
            (kind, row, pk)
            for row in (list(db.new) + list(db.dirty) + list(db.deleted))
            for kind, (model, pk) in SOURCES.items()
            if isinstance(row, model)
        ]
        if not changes:
            return
        identity = db.info.get(identity_key)
        if not identity or not identity.get("device_id") or not identity.get("subject"):
            return
        try:
            # Isolate a failed capture write so observability never aborts the source transaction.
            with db.connection().begin_nested():
                local_id = local_owner(db, identity)
                if not local_id:
                    return
                for kind, row, pk in changes:
                    if owner_of(db, row) == local_id:
                        enqueue(
                            db,
                            identity,
                            local_id,
                            kind,
                            str(getattr(row, pk)),
                            deleted=row in db.deleted,
                        )
        except Exception:
            capture_errors += 1
            logger.warning("desktop observation capture failed; pending reconciliation required")

    def capture_statement(state):
        global capture_errors
        mapper = state.bind_mapper
        selected = next(
            (
                (kind, model, pk)
                for kind, (model, pk) in SOURCES.items()
                if mapper is not None and mapper.class_ is model
            ),
            None,
        )
        if not selected or not (state.is_update or state.is_delete):
            return state.invoke_statement()
        identity = transaction_identity(state.session)
        if not identity or not identity.get("device_id"):
            return state.invoke_statement()
        kind, model, pk = selected
        db = state.session
        selected_ids = []
        try:
            local_id = local_owner(db, identity)
            if local_id:
                query = select(getattr(model, pk))
                if state.statement.whereclause is not None:
                    query = query.where(state.statement.whereclause)
                if hasattr(model, "user_id"):
                    query = query.where(model.user_id == local_id)
                elif hasattr(model, "chat_id"):
                    query = query.where(
                        model.chat_id.in_(
                            select(ChatSession.chat_id).where(ChatSession.user_id == local_id)
                        )
                    )
                else:
                    query = query.where(
                        model.run_id.in_(select(ChatRun.run_id).where(ChatRun.user_id == local_id))
                    )
                selected_ids = [str(oid) for oid in db.scalars(query.limit(257)).all()]
                if len(selected_ids) > 256:
                    capture_errors += 1
                    selected_ids = selected_ids[:256]
        except Exception:
            capture_errors += 1
        result = state.invoke_statement()
        if selected_ids and getattr(result, "rowcount", 0):
            try:
                with db.connection().begin_nested():
                    for oid in selected_ids:
                        enqueue(db, identity, local_id, kind, oid, deleted=state.is_delete)
            except Exception:
                capture_errors += 1
                logger.warning("desktop bulk observation capture failed")
        return result

    event.listen(factory, "after_transaction_create", snapshot_identity)
    event.listen(factory, "after_transaction_end", forget_identity)
    event.listen(factory, "after_flush", capture)
    event.listen(factory, "do_orm_execute", capture_statement, retval=True)

    def remove():
        event.remove(factory, "after_transaction_create", snapshot_identity)
        event.remove(factory, "after_transaction_end", forget_identity)
        event.remove(factory, "after_flush", capture)
        event.remove(factory, "do_orm_execute", capture_statement)

    return remove


def payload_for(db, item):
    model, _ = SOURCES[item.kind]
    row = db.get(model, item.object_id)
    if row is None or item.deleted:
        return {"deleted": True}
    if owner_of(db, row) != item.local_user_id:
        return {"deleted": True}
    payload = {k: getattr(row, k, None) for k in FIELDS[item.kind].split()}
    payload["execution_location"] = "local"
    if item.kind == "run":
        payload["agent_id"] = (row.request_payload or {}).get("agent_id") or "main_agent"
    elif getattr(row, "message_id", None):
        payload["run_id"] = (
            db.scalar(
                select(ChatRun.run_id)
                .where(
                    ChatRun.user_id == item.local_user_id,
                    or_(
                        ChatRun.message_id == row.message_id,
                        ChatRun.user_message_id == row.message_id,
                    ),
                )
                .limit(1)
            )
            or ""
        )

    if item.kind == "usage":
        payload["call_id"] = (row.attempt_metadata or {}).get("gateway_call_id", "")
        run = db.get(ChatRun, row.run_id)
        if run is not None:
            payload["chat_id"] = run.chat_id
    if item.kind in {"skill", "agent"}:
        source = str(getattr(row, "skill_source", "") or "")
        payload["capability_origin"] = "cloud" if "cloud" in source else "unknown"
    return public_payload(item.kind, payload)


def prepare_batch(db, identity):
    q = select(DesktopOutbox).where(*scope(identity), DesktopOutbox.pending.is_(True))
    pending = db.scalar(select(func.count()).select_from(q.subquery()))
    items = db.scalars(q.order_by(DesktopOutbox.updated_at, DesktopOutbox.id).limit(32)).all()
    events, size = [], 0
    for item in items:
        payload = payload_for(db, item)
        e = dict(kind=item.kind, object_id=item.object_id, revision=item.revision, payload=payload)
        n = len(json.dumps(e, ensure_ascii=False).encode())
        if events and size + n > 1024 * 1024:
            break
        events.append(e)
        size += n
    return {
        "events": events,
        "pending": max(0, pending - len(events)),
        "capture_errors": capture_errors,
    }


def acknowledge(db, identity, response):
    for ack in response.get("acknowledged", []):
        db.execute(
            update(DesktopOutbox)
            .where(
                *scope(identity),
                DesktopOutbox.kind == ack["kind"],
                DesktopOutbox.object_id == ack["object_id"],
                DesktopOutbox.revision == ack["revision"],
            )
            .values(pending=False, digest=key(json.dumps(ack.get("payload", {}), sort_keys=True)))
        )
    db.commit()


def backfill_step(db, identity, cursors):
    """Bounded historical reconciliation, outside task execution; resume on the next worker tick."""
    local_id = local_owner(db, identity)
    if not local_id:
        return
    for kind, (model, pk) in SOURCES.items():
        if cursors.get(kind) is None and kind in cursors:
            continue
        column = getattr(model, pk)
        q = select(model).where(column > cursors.get(kind, "")).order_by(column).limit(64)
        if hasattr(model, "user_id"):
            q = q.where(model.user_id == local_id)
        elif hasattr(model, "chat_id"):
            q = q.where(
                model.chat_id.in_(
                    select(ChatSession.chat_id).where(ChatSession.user_id == local_id)
                )
            )
        else:
            q = q.where(model.run_id.in_(select(ChatRun.run_id).where(ChatRun.user_id == local_id)))
        rows = db.scalars(q).all()
        for row in rows:
            oid = str(getattr(row, pk))
            current = db.get(
                DesktopOutbox,
                key(identity["cloud_base"], identity["subject"], identity["device_id"], kind, oid),
            )
            if current is None:
                enqueue(db, identity, local_id, kind, oid, initial=True)
            elif (
                not current.pending
                and key(json.dumps(payload_for(db, current), sort_keys=True)) != current.digest
            ):
                enqueue(db, identity, local_id, kind, oid)
        cursors[kind] = str(getattr(rows[-1], pk)) if len(rows) == 64 else None
        db.commit()
        break


class DesktopSyncWorker:
    def __init__(self, factory, *, interval=3.0, transport=None):
        self.factory, self.interval, self.transport = factory, interval, transport
        self.task = None
        self.cursors = {}
        self.account = None
        self.reconciled_at = 0.0

    async def sync_once(self):
        import httpx
        from core.services import desktop_cloud_bridge as bridge

        state, identity = bridge.get_state(), bridge.get_identity_state()
        if not state or not identity or not identity.get("device_id"):
            return
        account = key(identity["cloud_base"], identity["subject"], identity["device_id"])
        if account != self.account:
            self.account, self.cursors = account, {}
        import time

        if len(self.cursors) == len(SOURCES) and all(v is None for v in self.cursors.values()):
            if not self.reconciled_at:
                self.reconciled_at = time.monotonic()
            elif time.monotonic() - self.reconciled_at > 30:
                self.cursors, self.reconciled_at = {}, 0.0

        def prepare():
            with self.factory() as db:
                backfill_step(db, identity, self.cursors)
                batch = prepare_batch(db, identity)
                batch["reconciling"] = len(self.cursors) < len(SOURCES) or any(
                    v is not None for v in self.cursors.values()
                )
                return batch

        batch = await asyncio.to_thread(prepare)
        bridge.require_current_account(state)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            reply = await client.post(
                state["cloud_base"].rstrip("/") + "/api/v1/desktop/observability/batch",
                headers=bridge.cloud_headers(state),
                json=batch,
            )
            reply.raise_for_status()
            body = reply.json()
        bridge.require_current_account(state)
        result = body.get("data", {})
        # Accept acknowledgements only for this exact sent batch.
        allowed = {(e["kind"], e["object_id"], e["revision"]) for e in batch["events"]}
        accepted = {
            (a.get("kind"), a.get("object_id"), a.get("revision"))
            for a in result.get("acknowledged", [])
            if isinstance(a, dict)
        }
        acks = [
            e
            for e in batch["events"]
            if (e["kind"], e["object_id"], e["revision"]) in accepted & allowed
        ]

        def finish():
            with self.factory() as db:
                acknowledge(db, identity, {"acknowledged": acks})

        await asyncio.to_thread(finish)

    async def run(self):
        failures = 0
        while True:
            await asyncio.sleep(
                min(60, self.interval * 2 ** min(failures, 5)) + random.random() * 0.3
            )
            try:
                await self.sync_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning("desktop log delivery deferred error_type=%s", type(exc).__name__)

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
