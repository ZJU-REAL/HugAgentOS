"""Publish one immutable output in the owner's main conversation, independently of finish."""
import personal_agent.tasks.guards as _pa_guards
import personal_agent.tools.responses as _pa_responses
import personal_agent.workspace.paths as _pa_paths
import asyncio
import hashlib
import json
import logging
import uuid
import mimetypes
import posixpath
import re
import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.db.engine import SessionLocal
from core.db.models import Artifact, ChatMessage
from personal_agent import access
from personal_agent.models import PersonalAgent, PersonalWorkItem

MAX_BYTES = 20 * 1024 * 1024
log = logging.getLogger(__name__)

def discard_unpublished(key):
    """An attempt owns its unique upload key; never remove any committed reference."""
    try:
        with SessionLocal() as db:
            if db.scalar(select(Artifact.artifact_id).where(Artifact.storage_key == key)):
                return
        from core.files.storage import get_storage
        get_storage().delete(key)
    except Exception as exc:  # noqa: BLE001 - cleanup must preserve the original failure
        log.warning("Personal delivery upload cleanup failed (%s)", type(exc).__name__)

def active_task(db, ident, uid, expected):
    """Fence both reads and durable effects to the admitted execution."""
    task = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == ident).with_for_update())
    agent = db.get(PersonalAgent, task.agent_id) if task else None
    if (not task or not agent or agent.user_id != uid or agent.status != "ready"
            or task.status != "running" or not task.lease_until or task.lease_until <= time.time()
            or task.run_id != expected["run_id"] or task.owner != expected["owner"]
            or agent.generation != expected["generation"]
            or (task.checkpoint or {}).get("pending_approval") or not access.allowed(db, uid)):
        raise PermissionError("Task execution or delivery permission changed")
    if state_target := expected.get("resource_target"):
        from personal_agent.delivery.delivery_resources import device_scope
        device_scope(db, uid, state_target, expected.get("resource_epoch"))
    return task, agent

def artifacts_for_message(db, row, uid):
    if row.role != "assistant" or not (row.extra_data or {}).get("delivery"):
        return []
    items = []
    for ident in (row.extra_data or {}).get("artifact_ids", []):
        item = db.get(Artifact, ident)
        if item and item.user_id == uid and item.chat_id == row.chat_id and not item.deleted_at:
            items.append({"file_id": item.artifact_id, "name": (item.extra_data or {}).get("original_filename", item.filename),
                          "mime_type": item.mime_type, "size": item.size_bytes,
                          "url": "/api/files/" + item.artifact_id, "origin": "cloud", "chat_id": row.chat_id})
    return items

def _receipt(db, row, uid, request_hash):
    metadata = row.extra_data or {}
    if metadata.get("delivery_request_hash") != request_hash:
        raise ValueError("Delivery request_key already belongs to different input")
    files = artifacts_for_message(db, row, uid)
    if not files:
        raise ValueError("The delivered file is no longer available; use a new request_key")
    file = files[0]
    return {"delivered": True, "message_id": row.message_id, "artifact_id": file["file_id"],
            "filename": file["name"], "download_url": file["url"], "mime_type": file["mime_type"],
            "size_bytes": file["size"], "channel_status": (metadata.get("channel_delivery") or {}).get("status", "not_requested")}

def _identity(ident, request_key):
    digest = hashlib.sha256(json.dumps([ident, request_key]).encode()).hexdigest()[:32]
    return "pa_delivery_" + digest, "pa_file_" + digest

def _previous(ident, uid, state, message_id, request_hash):
    with SessionLocal() as db:
        active_task(db, ident, uid, state)
        row = db.get(ChatMessage, message_id)
        return _receipt(db, row, uid, request_hash) if row else None

def _publish(ident, uid, state, message_id, artifact_id, request_hash, content, filename, mime, caption):
    from core.files.artifacts.service import store_bytes_as_artifact, artifact_upload_key
    from personal_agent.context import AgentContext
    from personal_agent.channels.channel_bridge import mark_for_delivery
    from personal_agent.scheduling.schedules import is_unattended_run

    upload_key = None
    try:
        with SessionLocal() as db:
            task, agent = active_task(db, ident, uid, state)
            # SQLite ignores FOR UPDATE. A guarded write acquires its writer lock and
            # serializes concurrent deliveries before storage or message-count updates.
            guarded = db.execute(update(PersonalWorkItem).where(
                PersonalWorkItem.id == ident, PersonalWorkItem.status == "running",
                PersonalWorkItem.run_id == state["run_id"], PersonalWorkItem.owner == state["owner"],
                PersonalWorkItem.lease_until > time.time()
            ).values(updated_at=PersonalWorkItem.updated_at)).rowcount
            if guarded != 1: raise PermissionError("Task execution changed")
            db.expire_all()
            task, agent = active_task(db, ident, uid, state)
            row = db.get(ChatMessage, message_id)
            if row: return _receipt(db, row, uid, request_hash)
            digest = hashlib.sha256(content).hexdigest()
            stem, extension = posixpath.splitext(filename)
            stored_name = stem[:100] + "-" + artifact_id[-12:] + "-" + uuid.uuid4().hex[:12] + extension
            upload_key = artifact_upload_key(uid, artifact_id, stored_name)
            from personal_agent.conversation.conversations import task_chat
            source_chat_id = task_chat(db, agent, task)
            item = store_bytes_as_artifact(
                db, user_id=uid, content=content, filename=stored_name, mime_type=mime,
                chat_id=source_chat_id, source="ai_generated", artifact_id=artifact_id, commit=False,
                extra={"personal_work_item_id": ident, "original_filename": filename,
                       "delivery_message_id": message_id, "sha256": digest})
            # Storage may have taken time. Recheck the lease, access and device fence
            # before making the artifact, main message and channel outbox visible together.
            db.expire_all()
            task, agent = active_task(db, ident, uid, state)
            svc = AgentContext(db, uid)
            row = svc.conversation.append(agent, "assistant", caption, ident, relation="delivery", message_id=message_id)
            row.extra_data = {**row.extra_data, "delivery": True, "artifact_ids": [item.artifact_id],
                              "delivery_request_hash": request_hash}
            mark_for_delivery(db, row, task_id=ident, home=is_unattended_run(task))
            db.commit()
            return _receipt(db, row, uid, request_hash)
    except BaseException as exc:
        if upload_key: discard_unpublished(upload_key)
        if isinstance(exc, IntegrityError):
            # A competing identical delivery may already have committed its receipt.
            previous = _previous(ident, uid, state, message_id, request_hash)
            if previous: return previous
        raise

def build(ident, uid):
    async def pa_deliver(request_key: str, path: str = "", resource_ref: str = "",
                         filename: str = "", caption: str = ""):
        """Deliver one image or file NOW in the user's main conversation; the task can continue.
        Give exactly one source: an absolute /workspace/... path, or resource_ref returned by
        pa_computer_observe. For a screenshot, observe then pass its resource_ref here; never use
        Bash/scrot/sudo/Xauthority. request_key names this immutable delivery (e.g. screen-1):
        repeat it with the SAME source/name/caption to recover its receipt after an interruption.
        A changed file or a new delivery needs a NEW key. filename is the visible basename;
        screenshot names must end in .png. caption is optional user-facing text.
        delivered=true means persisted in the main conversation, NOT accepted by an IM channel.
        Expired resource_ref: observe again and deliver the NEW reference with a NEW request_key."""
        from personal_agent.tools import tools
        state = _pa_guards.owned_task(ident, uid)
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_key):
            raise ValueError("request_key must contain 1 to 100 letters, digits, underscores or hyphens")
        if bool(path) == bool(resource_ref):
            raise ValueError("Give exactly one path or resource_ref")
        if path:
            if ".." in path.split("/") or not path.startswith("/workspace/"):
                raise ValueError("Delivery path must be inside /workspace without traversal")
            path = _pa_paths.workspace_path(path)
        filename = filename or (posixpath.basename(path) if path else "screenshot.png")
        if (filename in {".", ".."} or "/" in filename or "\\" in filename
                or len(filename.encode()) > 180 or any(ord(c) < 32 for c in filename)):
            raise ValueError("filename must be a plain basename of at most 180 UTF-8 bytes")
        if not isinstance(caption, str) or len(caption) > 4000:
            raise ValueError("caption exceeds 4000 characters")
        if resource_ref and not filename.lower().endswith(".png"):
            raise ValueError("Screenshot filename must end in .png")
        caption = caption.strip()
        request_hash = hashlib.sha256(json.dumps([path, resource_ref, filename, caption],
                                                ensure_ascii=False).encode()).hexdigest()
        message_id, artifact_id = _identity(ident, request_key)
        previous = await asyncio.to_thread(_previous, ident, uid, state, message_id, request_hash)
        if previous: return _pa_responses.response(previous)
        if resource_ref:
            from personal_agent.delivery.delivery_resources import resolve
            content = await resolve(ident, uid, state, resource_ref)
            mime = "image/png"
        else:
            content = await tools.get_personal_sandbox_provider().get_file(state["box"], path, user_id=uid)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if len(content) > MAX_BYTES: raise ValueError("Delivery exceeds 20 MiB")
        receipt = await asyncio.to_thread(_publish, ident, uid, state, message_id, artifact_id,
                                         request_hash, content, filename, mime, caption)
        return _pa_responses.response(receipt)
    return pa_deliver
