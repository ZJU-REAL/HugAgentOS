"""The user's HugAgentOS sessions as one typed tool, built from the shared session contract.

The operations, their arguments and what each is for come from ``hugagent_cli.contract``,
so the tool, the CLI and the desktop device link never drift apart. What a call may do
is decided here and by the approval gate, not by prompt text: a conversation turn gets
the reads only; a task gets every operation, and its writes wait for the user's approval
like any other external action.
"""
import asyncio

from core.db.threads import run_in_session
import hashlib
import json
import time
import uuid
from typing import Literal
from fastapi import HTTPException
from hugagent_cli.contract import (
    FIELDS, GUIDE, READS, STATUS_COMPLETED, STATUS_FAILED, STATUS_NOT_EXECUTED, STATUS_OUTCOME_UNKNOWN,
)
from core.db.engine import SessionLocal

NAME = "pa_hugagent"
# Request keys of this tool's calls. The run watcher follows the sends made this way.
KEY_PREFIX = "tool:"
DEVICE_LIST = "device.list"
RUN_TERMINAL = {"completed", "failed", "cancelled", "needs_attention"}
MAX_WAIT = 600
POLL_SEC = 5
OUTPUT_CHARS = 20000
# A proactive task reads several chats in one run, and every result stays in its context (PROACTIVE_DESIGN §4.2).
PROACTIVE_OUTPUT_CHARS = 6000

COMMON = """The user's own HugAgentOS: their projects, chats and the runs working in those chats.
target is web (the user's HugAgentOS on the web, the default; not your own cloud computer) or
device:<id> (HugAgentOS on one of the user's computers; device.list shows the ids, and the device must
be online with session management on). Use the place the user names. Projects and chats are looked up
by name: before saying one does not exist, search with project.list q=<name> or chat.search, and list
without q when nothing matches.
Every result has a status: completed (result holds the answer); not_executed (it did not run and changed
nothing; message says why and whether retrying can help); outcome_unknown (a change that may have taken
effect: check with a read before repeating it); failed. Tell the user the reason in message, never just
that it failed."""

READ_ONLY = """Reads only, and they return at once. To create chats, send messages, or stop or steer runs,
start a background task with pa_task_create right away; put the project, chat or run you found in its goal.
The user approves each change in an approval card, so do not ask for confirmation first."""

WRITES = """Do the work yourself when your own tools are enough. Delegate to a HugAgentOS chat when the work
needs what you do not have: knowledge bases ("my knowledge base" means these), database queries,
specialist skills you were not given, connectors outside your plugins, publishing a site, batch runs or
very long research. To delegate: chat.create with a clear title (with project_id when the user names a
project, so the chat follows its instructions and files), then chat.send with the complete instruction.
The run works on its own and its result is reported to the user automatically when it ends (for a run
on the user's computer, once the computer is online to say so), so you may finish once it has started. When this task needs the result, call run.status with wait_seconds until the
run ends, then read chat.history. Writes can return pending_approval: then stop and say what waits for
the user's approval. Never repeat a write whose outcome is unknown; check it with a read first. A write
that did not run (not_executed) may be called again with the same arguments once its cause is fixed."""

def operations(writes):
    """The operations one context may call, in the contract's order."""
    names = [name for name in GUIDE if writes or name in READS]
    return [DEVICE_LIST, *names]

def describe(writes):
    lines = [COMMON, WRITES if writes else READ_ONLY, "Operations:", DEVICE_LIST + ": the user's paired computers and whether each is online (never your own cloud computer)"]
    for name in operations(writes)[1:]:
        fields = ", ".join(sorted(FIELDS[name]))
        lines.append(name + ": " + GUIDE[name] + (" (args: " + fields + ")" if fields else ""))
    return "\n".join(lines)

def owner(db, uid):
    from core.auth.backend import UserContext
    from core.db.models import UserShadow
    row = db.get(UserShadow, uid)
    if row is None: raise PermissionError("Owner unavailable")
    return UserContext(user_id=row.user_id, user_center_id=row.user_center_id or "", username=row.username)

def _caller(db, uid, task_id):
    """The owner, and the running task's grant when the call belongs to one."""
    grant = None
    if task_id:
        from personal_agent.models import PersonalWorkItem
        from personal_agent.tasks.guards import owned_task
        state = owned_task(task_id, uid)
        task = db.get(PersonalWorkItem, task_id)
        grant = {"task": task_id, "run": state["run_id"], "owner": task.owner}
    return owner(db, uid), grant

def request_key(task_id, target, operation, args):
    """A write keeps one key per task and exact action: after the user approves it, the task's
    next run finds the receipt instead of asking again. Reads always run fresh."""
    if operation in READS or not task_id:
        return KEY_PREFIX + uuid.uuid4().hex
    body = json.dumps([task_id, target, operation, args], sort_keys=True, ensure_ascii=False)
    return KEY_PREFIX + hashlib.sha256(body.encode()).hexdigest()[:48]

def bounded(value, limit=OUTPUT_CHARS):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit: return text
    return text[:limit] + "\n[truncated: ask for fewer items with page_size]"

UPDATE_NOTE = ("The HugAgentOS desktop app on this computer may need an update: it reports a different "
               "session contract than the cloud's. If operations fail, ask the user to update it.")


def _reason(job):
    """The cause in words: the receipt's detail, else its error code, with the HTTP status."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    # Older desktops append advice to the code ("local_failure; check the result before retry"):
    # the advice here is decided by the status, so only the code is kept.
    reason = str(result.get("detail") or str(result.get("error") or "").split(";")[0]).strip()
    if result.get("http_status") and "HTTP" not in reason:
        reason = ("HTTP " + str(result["http_status"]) + (": " + reason if reason else "")).strip()
    return reason


def report(job):
    """What the agent is told about one finished operation: status from session_queue.outcome, the
    receipt's failure fields at top level, and one plain sentence."""
    from personal_agent.sessions import session_queue

    status = session_queue.outcome(job)
    read = job.get("operation") in READS
    if status is None:
        status = STATUS_NOT_EXECUTED if read else STATUS_OUTCOME_UNKNOWN
    if status == STATUS_COMPLETED:
        return {"status": STATUS_COMPLETED, "result": job.get("result")}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    out = {"status": status}
    for key in ("error", "http_status", "detail", "exception", "retryable"):
        if result.get(key) is not None:
            out[key] = result[key]
    if "error" in out:
        out["error"] = str(out["error"]).split(";")[0].strip()  # the code, without an old desktop's advice
    where = "the computer" if str(job.get("target", "")).startswith("device:") else "HugAgentOS"
    state = job.get("status")
    if state == "expired":
        out.setdefault("error", "not_picked_up")
        out["retryable"] = True
        out["message"] = "The computer did not pick this up in time; it did not run. Check that it is online, then retry."
        return out
    if state == "revoked":
        out.setdefault("error", "withdrawn")
        if status == STATUS_OUTCOME_UNKNOWN:
            out["message"] = ("It was withdrawn (session management turned off, the computer unlinked or the task "
                              "changed) after " + where + " took it; it may have taken effect. Check before repeating it.")
        else:
            out["retryable"] = True
            out["message"] = ("It was withdrawn before it ran (session management turned off, the computer unlinked "
                              "or the task changed); it did not run. Safe to retry once that is fixed.")
        return out
    if state == session_queue.RECONCILED:
        out.setdefault("error", "checked_by_user")
        out["retryable"] = True
        out["message"] = "The user checked that this did not take effect. It may be asked again."
        return out
    if state == "outcome_unknown":
        out.setdefault("error", "no_answer")
        seconds = str(session_queue.DEADLINE_SECONDS)
        if read:
            out["retryable"] = True
            out["message"] = "No answer from " + where + " within " + seconds + " s; a read changes nothing. Safe to retry."
        else:
            out["message"] = ("No answer from " + where + " within " + seconds + " s; it may have taken effect. "
                              "Check with a read before repeating it.")
        return out
    reason = _reason(job) or "no reason given"
    if status == STATUS_NOT_EXECUTED:
        if read:
            out.setdefault("retryable", True)
            out["message"] = "Did not run; " + reason + "; safe to retry."
        else:
            out["message"] = "Did not run; " + reason + "; safe to retry after fixing the cause."
    elif status == STATUS_OUTCOME_UNKNOWN:
        out["message"] = "May have taken effect (" + reason + "); check with a read before repeating it."
    else:
        out["message"] = "Failed; " + reason + "." + (" Safe to retry." if read else " Check with a read before repeating it.")
    if status == STATUS_FAILED and read:
        out.setdefault("retryable", True)
    return out


def earlier(receipt):
    """A receipt the approval gate answered with instead of an operation (pending approval, rejection,
    the user's check, or a dispatch whose outcome was never written)."""
    if receipt.get("status") == "uncertain":
        return {"status": STATUS_OUTCOME_UNKNOWN, "error": "unconfirmed",
                "message": "This exact write was sent earlier and its outcome was never confirmed. "
                           "Check with a read; the user can confirm whether it took effect.",
                **({"detail": receipt["message"]} if receipt.get("message") else {})}
    return receipt


def _device_note(db, uid, target):
    """UPDATE_NOTE when the target desktop reports a contract other than this cloud's."""
    if not str(target).startswith("device:"):
        return None
    from personal_agent.computer.devices import contract_matches
    from personal_agent.models import PersonalDevice
    row = db.get(PersonalDevice, target[7:])
    if row is None or row.user_id != uid:
        return None
    return None if contract_matches(row) else UPDATE_NOTE


async def call(uid, operation, args, target="web", task_id=None, wait=60):
    """Run one operation as the owner and wait for its answer. task_id binds it to a running task
    and its approvals; without one, only reads are allowed. ``wait`` is kept for callers; the
    operation's own deadline (60 s plus the receipt grace) ends the wait."""
    from personal_agent.computer.devices import list_devices
    from personal_agent.sessions.session_manager import dispatch, normalize_target
    args = dict(args or {})
    if operation == DEVICE_LIST:
        return {"status": "completed", "result": list_devices(uid)}
    if operation not in READS and not task_id:
        raise PermissionError("Only a background task may change the user's HugAgentOS")
    target = normalize_target(target)
    user, grant = await run_in_session(_caller, uid, task_id)
    job = await dispatch(user, target, operation, args,
        request_key(task_id, target, operation, args), grant, wait=True)
    result = report(job) if job.get("id") else earlier(job)
    note = await run_in_session(_device_note, uid, target)
    if note:
        result["desktop_note"] = note
    return result


def refused(exc, status=None):
    """An operation refused before it was queued (device offline, session management off, invalid
    arguments, a task that changed): it certainly did not run."""
    detail = str(getattr(exc, "detail", None) or exc)
    out = {"status": STATUS_NOT_EXECUTED, "error": detail}
    if status is not None:
        out["http_status"] = status
    out["message"] = "Did not run; " + detail + "; safe to retry after fixing the cause."
    return out


async def follow(uid, args, target, task_id, seconds):
    """run.status until the run ends or the time is up."""
    end = time.monotonic() + max(0, min(MAX_WAIT, seconds))
    while True:
        result = await call(uid, "run.status", args, target, task_id)
        status = result["result"].get("status") if isinstance(result.get("result"), dict) else None
        if status in RUN_TERMINAL or status is None or time.monotonic() >= end:
            if status in RUN_TERMINAL and task_id: observed(task_id, uid, args["run_id"])
            return result
        await asyncio.sleep(POLL_SEC)

def observed(task_id, uid, run_id):
    """The task saw this run end, so it reports the result itself; the watcher stays quiet."""
    from personal_agent.context import AgentContext
    with SessionLocal() as db:
        task = AgentContext(db, uid).tasks.get(task_id, lock=True)
        state = dict(task.checkpoint or {})
        state["observed_runs"] = list(dict.fromkeys([*state.get("observed_runs", []), run_id]))[-50:]
        task.checkpoint = state
        db.commit()

def build(uid, task_id=None, output_chars=OUTPUT_CHARS):
    """The tool function for one context: a task (task_id) or a conversation turn.
    output_chars bounds the text of one result."""
    writes = bool(task_id)

    async def pa_hugagent(operation: str, args: dict | None = None, target: str = "web", wait_seconds: int = 0):
        try:
            # On the user's computer too: each status read goes through the device queue.
            if writes and operation == "run.status" and wait_seconds > 0:
                result = await follow(uid, args or {}, target, task_id, wait_seconds)
            else:
                result = await call(uid, operation, args, target, task_id)
                status = (result.get("result") or {}).get("status") if isinstance(result.get("result"), dict) else None
                if operation == "run.status" and task_id and status in RUN_TERMINAL:
                    observed(task_id, uid, (args or {})["run_id"])
        except HTTPException as exc:
            result = refused(exc, exc.status_code)
        except (ValueError, LookupError, PermissionError) as exc:
            result = refused(exc)
        from agentscope.message import TextBlock
        from agentscope.tool._response import ToolChunk
        return ToolChunk(content=[TextBlock(type="text", text=bounded(result, output_chars))])

    pa_hugagent.__annotations__["operation"] = Literal[tuple(operations(writes))]
    args_doc = ["operation: the operation to run", "args: the operation's arguments, as listed for it",
        "target: web (the user's HugAgentOS on the web; \"cloud\" means the same), or device:<id> for HugAgentOS on one of the user's computers"]
    if writes: args_doc.append("wait_seconds: run.status only: wait up to this many seconds (at most 600) for the run to end")
    else: args_doc.append("wait_seconds: not used in a conversation turn")
    pa_hugagent.__doc__ = describe(writes) + "\n\nArgs:\n" + "\n".join("    " + line for line in args_doc)
    return pa_hugagent
