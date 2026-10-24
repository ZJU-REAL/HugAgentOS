"""Database authority for command ownership, output cursors and delivery."""
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import update
from core.db.models import UserShadow
from core.db.engine import SessionLocal
from core.db.models.command import CommandTask

LIVE = ("starting", "running")
TERMINAL = ("completed", "failed", "cancelled", "unknown")
LOG_CHARS = 262144
LEASE_SECONDS = 30
LAUNCH_LEASE_SECONDS = 120

def view(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}

def can_edit(db, chat_id, user_id):
    from core.chat.service import ChatService
    pair = ChatService(db).get_session_with_access(chat_id, user_id)
    return pair is not None and pair[1] in ("admin", "edit")

def create(req, chat_id, source_run_id, owner=None, process_id=None):
    with SessionLocal() as db:
        # Serialize the per-user limit and adoption across all of their chats.
        actor = db.execute(update(UserShadow).where(UserShadow.user_id == (req.user_id or ""))
            .values(username=UserShadow.username))
        if actor.rowcount != 1 or not can_edit(db, chat_id, req.user_id or ""):
            raise PermissionError("Conversation is not available to this user")
        existing = db.query(CommandTask).filter_by(user_id=req.user_id or "",
            sandbox_session_id=req.session_id, process_id=process_id).first() if process_id else None
        if existing:
            return {**view(existing), "created": False}
        if db.query(CommandTask).filter_by(user_id=req.user_id or "").filter(
                CommandTask.status.in_(LIVE)).count() >= 64:
            raise RuntimeError("Too many live commands")
        row = CommandTask(task_id="cmd_" + uuid.uuid4().hex, chat_id=chat_id,
            user_id=req.user_id or "", sandbox_session_id=req.session_id,
            source_run_id=source_run_id, command=req.script_content, owner=owner,
            lease_until=time.time() + LAUNCH_LEASE_SECONDS, process_id=process_id,
            deadline_at=time.time() + req.timeout if req.timeout else None)
        db.add(row)
        db.commit()
        return {**view(row), "created": True}

def snapshot(task_id):
    with SessionLocal() as db:
        row = db.get(CommandTask, task_id)
        if row is None:
            raise LookupError("Command task does not exist")
        return view(row)

def owned(task_id, user_id, session_id):
    row = snapshot(task_id)
    if (row["user_id"], row["sandbox_session_id"]) != (user_id or "", session_id):
        raise PermissionError("Command task does not belong to this user and sandbox")
    return row

@contextmanager
def locked(task_id):
    with SessionLocal() as db:
        # The first UPDATE serializes mutations on PostgreSQL and SQLite.
        result = db.execute(update(CommandTask).where(CommandTask.task_id == task_id)
                            .values(version=CommandTask.version + 1))
        if result.rowcount != 1:
            raise LookupError("Command task does not exist")
        row = db.get(CommandTask, task_id)
        yield db, row
        db.commit()

def _append(row, stream, text):
    current = getattr(row, stream) + (text or "")
    removed = max(0, len(current) - LOG_CHARS)
    setattr(row, stream, current[removed:])
    setattr(row, stream + "_base", getattr(row, stream + "_base") + removed)

def _result(row, result):
    row.result_metadata = {**row.result_metadata, **{key: result[key] for key in (
        "output_files", "lifetime_note", "execution_time_ms", "stdin_supported")
        if key in result}}
    row.result_metadata["output_omitted_chars"] = row.result_metadata.get("output_omitted_chars", 0) + result.get("output_omitted_chars", 0)
    _append(row, "stdout", result.get("stdout", ""))
    _append(row, "stderr", result.get("stderr", ""))
    if result.get("status") == "exited" or result.get("error"):
        row.status = "failed" if result.get("error") or result.get("exit_code") != 0 else "completed"
        row.exit_code = result.get("exit_code")
        row.error = str(result.get("error") or "")[:4000]
        row.finished_at = datetime.now(timezone.utc)
        row.notice_status = "pending" if row.background else "none"

def attach(task_id, owner, result, reference):
    with locked(task_id) as (_, row):
        if row.status != "starting" or row.owner not in (None, owner):
            raise RuntimeError("Command launch ownership lost")
        row.owner, row.lease_until = owner, time.time() + LEASE_SECONDS
        row.process_id = result.get("session_id")
        row.execution_ref = reference
        if result.get("deadline_at") is not None:
            row.deadline_at = result["deadline_at"]
        row.background = result.get("status") == "running"
        row.status = "running"
        _result(row, result)
        return view(row)

def collect(task_id, owner, result):
    with locked(task_id) as (_, row):
        if row.owner != owner or row.lease_until < time.time():
            raise RuntimeError("Command execution lease lost")
        if row.status not in LIVE:
            return view(row)
        row.lease_until = time.time() + LEASE_SECONDS
        if row.execution_ref is not None and "execution_cursor" in result:
            row.execution_ref = {**row.execution_ref, "cursor": result["execution_cursor"]}
        _result(row, result)
        return view(row)

def finish(task_id, status, error="", *, owner=None):
    with locked(task_id) as (_, row):
        if owner is not None and row.owner != owner:
            raise RuntimeError("Command execution lease lost")
        if row.status in TERMINAL:
            return
        row.status, row.error = status, str(error)[:4000]
        row.finished_at = datetime.now(timezone.utc)
        row.notice_status = "pending" if row.background else "none"

def claim(task_id, owner):
    with locked(task_id) as (_, row):
        if row.status not in LIVE or row.lease_until > time.time():
            return False
        row.owner, row.lease_until = owner, time.time() + LEASE_SECONDS
        return True

def pending_recovery():
    with SessionLocal() as db:
        return [view(r) for r in db.query(CommandTask).filter(
            CommandTask.status.in_(LIVE), CommandTask.lease_until <= time.time()).limit(64)]

def read(task_id, user_id, session_id, *, consumer="model", consume_notice=True):
    owned(task_id, user_id, session_id)
    with locked(task_id) as (_, row):
        readers = dict(row.readers)
        previous = readers.get(consumer, {})
        result = {**row.result_metadata, "task_id": task_id, "session_id": task_id,
                  "status": "running" if row.status in LIVE else "exited",
                  "task_status": row.status, "exit_code": row.exit_code,
                  "error": row.error or "", "background": row.background}
        upstream_omitted = row.result_metadata.get("output_omitted_chars", 0)
        omitted = max(0, upstream_omitted - previous.get("provider_omitted_chars", 0))
        cursor = {"provider_omitted_chars": upstream_omitted}
        for stream in ("stdout", "stderr"):
            base, text = getattr(row, stream + "_base"), getattr(row, stream)
            position = previous.get(stream, 0)
            omitted += max(0, base - position)
            result[stream] = text[max(0, position - base):]
            cursor[stream] = base + len(text)
        if consumer is not None:
            readers[consumer] = cursor
            row.readers = readers
        result["output_omitted_chars"] = omitted
        if row.status in LIVE:
            result["next"] = "Completion will notify this chat. Do independent work; do not poll or restart. If no independent work remains, end this response once; completion will resume this chat."
        if consume_notice and row.status in TERMINAL and row.notice_status == "pending":
            row.notice_status = "consumed"
        return result

def list_owned(user_id, chat_id):
    with SessionLocal() as db:
        query = db.query(CommandTask).filter_by(user_id=user_id, chat_id=chat_id).order_by(
            CommandTask.created_at.desc())
        rows = query.filter(CommandTask.status.in_(LIVE)).all()
        rows += query.filter(CommandTask.status.in_(TERMINAL)).limit(50).all()
        return [{"task_id": r.task_id, "command": r.command[:200], "status": r.status,
                 "exit_code": r.exit_code, "notice_run_id": r.notice_run_id, "created_at": r.created_at.isoformat()} for r in rows]

def pending_notices(chat_id=None, after=None):
    with SessionLocal() as db:
        query = db.query(CommandTask).filter(CommandTask.notice_status.in_(("pending", "reserved")))
        if chat_id is not None:
            query = query.filter_by(chat_id=chat_id)
        if after is not None:
            query = query.filter(CommandTask.task_id > after)
        return [view(r) for r in query.order_by(CommandTask.task_id).limit(50)]

def find_process(process_id, user_id, session_id):
    with SessionLocal() as db:
        row = db.query(CommandTask).filter_by(process_id=process_id, user_id=user_id or "",
                                               sandbox_session_id=session_id).first()
        return row.task_id if row else None

def resolve_id(process_id, user_id, session_id):
    if process_id.startswith("cmd_"):
        owned(process_id, user_id, session_id)
        return process_id
    task_id = find_process(process_id, user_id, session_id)
    if task_id is None:
        raise LookupError("Process is not managed; adopt it before reading")
    return task_id

def control_result(task_id, owner, status, error="", suppress_notice=False, result=None):
    with locked(task_id) as (_, row):
        if row.owner != owner:
            raise RuntimeError("Command execution lease lost")
        if result is not None:
            if row.execution_ref is not None and "execution_cursor" in result:
                row.execution_ref = {**row.execution_ref, "cursor": result["execution_cursor"]}
            _result(row, result)
        row.status, row.error = status, error
        row.finished_at = datetime.now(timezone.utc)
        row.notice_status = "consumed" if suppress_notice else "pending"

def request_cancel(task_id):
    with locked(task_id) as (_, row):
        if row.status in LIVE:
            row.cancel_requested = True

def release_lease(task_id, owner):
    with locked(task_id) as (_, row):
        if row.owner == owner:
            row.lease_until = 0

def save_recovery(task_id, owner, result, reference):
    with locked(task_id) as (_, row):
        if row.owner != owner:
            raise RuntimeError("Command execution lease lost")
        row.process_id = result.get("session_id")
        row.execution_ref = reference
        _result(row, result)
        return view(row)

def chat_available(task_id):
    with SessionLocal() as db:
        row = db.get(CommandTask, task_id)
        if row is None:
            return False
        return can_edit(db, row.chat_id, row.user_id)


def has_pending(chat_id, user_id):
    with SessionLocal() as db:
        return db.query(CommandTask.task_id).filter_by(chat_id=chat_id, user_id=user_id,
            notice_status="pending").first() is not None

def renew_launch(task_id, owner):
    with locked(task_id) as (_, row):
        if row.owner != owner or row.status != "starting":
            raise RuntimeError("Command launch ownership lost")
        row.lease_until = time.time() + LAUNCH_LEASE_SECONDS


def renew_owned(task_ids, owner):
    """Keep live local handles fenced even while output/stop RPCs are blocked."""
    now = time.time()
    with SessionLocal() as db:
        db.execute(update(CommandTask).where(
            CommandTask.task_id.in_(task_ids), CommandTask.owner == owner,
            CommandTask.status.in_(LIVE), CommandTask.lease_until > now,
        ).values(lease_until=now + LEASE_SECONDS))
        db.commit()
