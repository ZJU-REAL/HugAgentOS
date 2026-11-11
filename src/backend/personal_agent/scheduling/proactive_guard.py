"""What a proactive task must leave as it found it: the persona files, the proactive list and the memory.

Its file tools cannot write them, but its command line can: /workspace is one read-write mount
(PROACTIVE_DESIGN §6.1). A line a mail or a page talked the task into writing there would stay
for good and be read into every later turn. So the files are copied when the task starts,
next to the workspace where the sandbox cannot reach, and compared when one of its runs ends:
a file that changed without a record in the memory's write log is put back, and the task's
report says so.

The user hears of it once: in the summary when the task ends with a result (also when it had
nothing else to report), otherwise in a message of its own (tell).

Limits: a persona file or HEARTBEAT.md has no write log, so an edit the owner or the agent made
to one of them during the task is put back as well. A process the task
left running can write again after the comparison.
"""
import json
import logging
import os
import re
import shutil
import stat
import time
from datetime import datetime, timezone

from sqlalchemy import select

from personal_agent.memory import layout
from personal_agent.workspace import files

log = logging.getLogger(__name__)
FOLDER = "proactive-guard"
# Copies of tasks that never came back to be compared are removed after this long.
KEEP_SEC = 2 * 86400
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# The write log is asked for records from a little before the copy: the clocks of the two need not agree.
SLACK_SEC = 5
NOTE = "这次处理中有内容试图修改我的设定，已还原。"
RESTORED = "proactive_restored"
TOLD = "proactive_restored_told"

def _root(agent_id):
    return files.local_workspace(agent_id).parent / FOLDER

def _path(agent_id, task_id):
    if not TASK_ID.match(task_id or ""): raise ValueError("Invalid task id")
    return _root(agent_id) / (task_id + ".json")

def guarded(agent_id):
    """{rel: text, None for a missing file, False for one too large to copy} of the persona files,
    the proactive list and every memory file."""
    from personal_agent.memory import store
    from personal_agent.workspace.constants import CONTEXT_FILES, EDIT_MAX_BYTES, HEARTBEAT_FILE
    found = {**{rel: store.read(agent_id, rel) for rel in (*CONTEXT_FILES, HEARTBEAT_FILE)}, **store.wiki(agent_id)}
    # The platform never writes a file over this size, so one that large cannot be put back whole.
    return {rel: False if text is not None and len(text.encode()) > EDIT_MAX_BYTES else text for rel, text in found.items()}

def take(agent_id, task_id):
    """Copy the guarded files once per task; a later run of the task keeps the first copy."""
    try:
        target = _path(agent_id, task_id)
        if target.exists(): return
        root = target.parent
        root.mkdir(mode=0o700, exist_ok=True)
        for old in root.iterdir():
            if old.stat().st_mtime < time.time() - KEEP_SEC:
                shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
        with files.workspace_lock(agent_id):
            copy = {"at": time.time(), "files": guarded(agent_id)}
        temp = root / ("." + task_id + ".tmp")
        temp.write_text(json.dumps(copy, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, target)
    except (files.WorkspaceUnavailable, OSError) as exc:
        # Without the copy nothing can be put back; the task itself is not held up for that.
        log.warning("Proactive guard copy failed (%s)", type(exc).__name__)

def _recorded(db, agent_id, rel, text, since):
    """True when the memory's write log has this content as a write made since the copy: a turn of
    the agent, the owner's editor or Dreaming wrote it, not the task's command line."""
    from personal_agent.memory.models import PersonalMemoryVersion
    from personal_agent.memory.versions import digest
    after = PersonalMemoryVersion.after_hash
    return db.scalar(select(PersonalMemoryVersion.id).where(PersonalMemoryVersion.agent_id == agent_id,
        PersonalMemoryVersion.path == rel, after.is_(None) if text is None else after == digest(text),
        PersonalMemoryVersion.created_at >= datetime.fromtimestamp(since - SLACK_SEC, timezone.utc)).limit(1)) is not None

def _put_back(agent_id, rel, text):
    """Make the file what it was. Whatever sits at the path now (a link, a new file) goes first."""
    parts = rel.split("/")
    fd = files._open_dir(files.local_workspace(agent_id), parts[:-1], create=True)
    try:
        try: info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError: info = None
        from personal_agent.workspace.constants import EDIT_MAX_BYTES
        if info is not None and (text is None or not stat.S_ISREG(info.st_mode) or info.st_size > EDIT_MAX_BYTES):
            os.unlink(parts[-1], dir_fd=fd)
            os.fsync(fd)
    finally:
        os.close(fd)
    if text is not None: files._write_file(agent_id, rel, text, None)

def restore(db, agent_id, task_id):
    """Put back every guarded file that changed since the copy without a record; the paths put back.
    A memory file gets a SYSTEM version, so the write log shows what was undone."""
    from personal_agent.memory import versions
    try:
        target = _path(agent_id, task_id)
        if not target.is_file(): return []
        copy = json.loads(target.read_text(encoding="utf-8"))
        before, undone = copy["files"], []
        with files.workspace_lock(agent_id):
            now = guarded(agent_id)
            for rel in sorted(set(before) | set(now)):
                was, became = before.get(rel), now.get(rel)
                if was == became or was is False: continue
                memory = layout.is_memory(rel)
                if memory and became is not False and _recorded(db, agent_id, rel, became, copy["at"]): continue
                _put_back(agent_id, rel, was)
                if memory: versions.record(db, agent_id, rel, became or None, was, layout.SYSTEM)
                undone.append(rel)
        if undone: log.warning("Proactive task %s changed guarded files; put back: %s", task_id, ", ".join(undone))
        return undone
    except Exception as exc:  # noqa: BLE001 - settling the task matters more than the comparison
        log.warning("Proactive guard comparison failed (%s)", type(exc).__name__, exc_info=True)
        return []

def discard(agent_id, task_id):
    try: _path(agent_id, task_id).unlink(missing_ok=True)
    except (files.WorkspaceUnavailable, OSError, ValueError): pass

def close(db, task):
    """One run of the task has ended: compare, put back, and note it on the task so that its report tells
    the user. After the user took the task over nothing is put back: that run did what they asked for."""
    from personal_agent.scheduling.schedules import is_proactive
    if not is_proactive(task):
        discard(task.agent_id, task.id)
        return []
    undone = restore(db, task.agent_id, task.id)
    if not undone: return []
    state = dict(task.checkpoint or {})
    state[RESTORED] = sorted({*state.get(RESTORED, []), *undone})
    report = dict(state.get("report") or {})
    if report.get("status") in ("ready", "dropped") and NOTE not in report.get("summary", ""):
        # Worth telling the user even when the check found nothing else.
        kept = report["summary"].strip() + "\n\n" if report["status"] == "ready" else ""
        state["report"] = {**report, "status": "ready", "summary": kept + NOTE}
    task.checkpoint = state
    return undone

def tell(db, task):
    """The task ended without a result (it failed, ran out of time or was stopped), so no summary will
    carry the note: say it in the conversation now, once. True when it was said.

    A task that ends with a result is told by its result turn, which reads RESTORED (router/kinds.restored)."""
    state = dict(task.checkpoint or {})
    paths = state.pop(RESTORED, None)
    if not paths: return False
    # Moved, not kept: if the task is run again, its result does not repeat the note.
    first = TOLD not in state
    state[TOLD] = sorted({*state.get(TOLD, []), *paths})
    task.checkpoint = state
    if not first: return False
    from personal_agent.tasks.worker import notify
    notify(db, task, NOTE)
    return True

def noted(task, text):
    """The text of a result that had no report, with the note when files were put back."""
    if (task.checkpoint or {}).get(RESTORED) and (text or "").strip() and NOTE not in text:
        return text.strip() + "\n\n" + NOTE
    return text
