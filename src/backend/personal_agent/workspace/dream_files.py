"""Bounded evidence collection and atomic Dreaming file operations."""
import personal_agent.workspace.constants as _pa_constants
import personal_agent.workspace.files as _pa_files
import hashlib
import os
from sqlalchemy import select
from core.db.models import ChatMessage
from personal_agent.workspace import file_memory
from personal_agent.workspace.dream_evidence import statements

TARGETS = ("USER.md", "MEMORY.md")
SOURCE_LIMIT = 48_000

def read(ident, path, limit=_pa_constants.EDIT_MAX_BYTES):
    try:
        data, info = _pa_files._read(_pa_files.local_workspace(ident), path, limit)
    except FileNotFoundError:
        return ""
    if info.st_size > limit: raise ValueError("Dreaming file exceeds read budget: " + path)
    return data.decode("utf-8")

def pending_query(agent):
    return select(ChatMessage).where(
        ChatMessage.extra_data[file_memory.KEY]["agent_id"].as_string() == agent.id,
        ChatMessage.extra_data[file_memory.KEY]["status"].as_string() == "written",
        ChatMessage.extra_data[file_memory.KEY]["dreamed"].as_boolean().is_not(True),
    ).order_by(ChatMessage.created_at, ChatMessage.message_id)

def pending(db, agent):
    return db.scalar(pending_query(agent).limit(1)) is not None

def collect(db, agent, skipped=None, *, mark_skipped=True):
    """Bound scanning and disk reads, acknowledging changed evidence as review-only."""
    rows = db.scalars(pending_query(agent).limit(100)).all()
    sources, budget, files, read_budget = [], SOURCE_LIMIT, {}, 32 * 1024 * 1024
    for row in rows:
        from personal_agent.conversation.conversations import require_session
        receipt = dict(row.extra_data[file_memory.KEY])
        path = "memory/" + receipt["day"] + ".md"
        reason = None
        try: require_session(db, agent, row.chat_id)
        except LookupError: reason = "conversation unavailable"
        if not reason:
            if path not in files:
                if read_budget < 16 * 1024 * 1024: break
                files[path] = read(agent.id, path, 16 * 1024 * 1024)
                read_budget -= len(files[path].encode())
            marker = "<!-- pa-journal:" + hashlib.sha256(row.message_id.encode()).hexdigest() + " -->"
            text = files[path]
            block = text.split(marker, 1)[1].split("<!-- pa-journal:", 1)[0] if marker in text else None
            expected = file_memory.entry_body(receipt, row.content or "")
            if block is None or block.strip() != expected.strip(): reason = "daily evidence removed or edited; review manually"
        if reason:
            if mark_skipped:
                receipt.update(dreamed=True, dream_skipped=reason)
                row.extra_data = {**row.extra_data, file_memory.KEY: receipt}
            if skipped is not None: skipped.append({"path": path, "message_id": row.message_id, "reason": reason})
            continue
        user, result = receipt.get("input", ""), file_memory.redact(row.content or "")
        approved = statements(user) if row.chat_id == agent.chat_id and receipt["source"] == "conversation" else []
        count = max(1, (max(len(user), len(result)) + 3999) // 4000)
        for index in range(count):
            if index in receipt.get("dreamed_parts", []): continue
            source = {"id": row.message_id + ":" + str(index), "message_id": row.message_id,
                "part": index, "parts": count, "path": path, "at": receipt["at"],
                "user_authored": row.chat_id == agent.chat_id and receipt["source"] == "conversation",
                "input": user[index*4000:(index+1)*4000], "result": result[index*4000:(index+1)*4000]}
            source["memory_statements"] = [line for line in approved if line in source["input"]]
            cost = len(source["input"]) + len(source["result"])
            if cost > budget or len(sources) >= 24: return sources
            sources.append(source)
            budget -= cost
    return sources

def acknowledge(db, sources):
    for source in sources:
        row = db.get(ChatMessage, source["message_id"])
        if row is None: continue
        receipt = dict(row.extra_data[file_memory.KEY])
        parts = set(receipt.get("dreamed_parts", [])) | {source["part"]}
        receipt.update(dreamed_parts=sorted(parts), dreamed=len(parts) >= source["parts"])
        row.extra_data = {**row.extra_data, file_memory.KEY: receipt}

def append_report(ident, run_id, body):
    """Caller holds workspace lock. A marker makes crash replay idempotent."""
    previous = read(ident, "DREAMS.md", 16 * 1024 * 1024)
    marker = "<!-- pa-dream:" + run_id + " -->"
    if marker in previous: return
    data = ((previous or "# Dreaming\n") + "\n" + marker + "\n" + body).encode()
    if len(data) > 16 * 1024 * 1024: raise ValueError("Dreaming report full; archive it before retry")
    fd = _pa_files._open_dir(_pa_files.local_workspace(ident), [])
    temp = ".DREAMS." + os.urandom(6).hex() + ".tmp"
    try:
        target = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(target, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _pa_files._own(temp, fd)
        os.replace(temp, "DREAMS.md", src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try: os.unlink(temp, dir_fd=fd)
        except FileNotFoundError: pass
        os.close(fd)
