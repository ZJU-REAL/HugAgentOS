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
from hugagent_cli.contract import FIELDS, GUIDE, READS
from core.db.engine import SessionLocal

NAME = "pa_hugagent"
# Request keys of this tool's calls. The run watcher follows the sends made this way.
KEY_PREFIX = "tool:"
DEVICE_LIST = "device.list"
RUN_TERMINAL = {"completed", "failed", "cancelled", "needs_attention"}
MAX_WAIT = 600
POLL_SEC = 5
OUTPUT_CHARS = 20000

COMMON = """The user's own HugAgentOS: their projects, chats and the runs working in those chats.
target is cloud (the user's web HugAgentOS, the default) or device:<id> (HugAgentOS on one of the
user's computers; device.list shows the ids, and the device must be online with session management on).
Use the place the user names. Projects and chats are looked up by name: before saying one does not
exist, search with project.list q=<name> or chat.search, and list without q when nothing matches."""

READ_ONLY = """Reads only, and they return at once. To create chats, send messages, or stop or steer runs,
start a background task with pa_task_create right away; put the project, chat or run you found in its goal.
The user approves each change in an approval card, so do not ask for confirmation first."""

WRITES = """Do the work yourself when your own tools are enough. Delegate to a HugAgentOS chat when the work
needs what you do not have: knowledge bases ("my knowledge base" means these), database queries,
specialist skills you were not given, connectors outside your plugins, publishing a site, batch runs or
very long research. To delegate: chat.create with a clear title (with project_id when the user names a
project, so the chat follows its instructions and files), then chat.send with the complete instruction.
The run works on its own and its result is reported to the user automatically when it ends, so you may
finish once it has started. When this task needs the result, call run.status with wait_seconds until the
run ends, then read chat.history. Writes can return pending_approval: then stop and say what waits for
the user's approval. Never repeat a write whose outcome is unknown; check it with a read first."""

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

def bounded(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= OUTPUT_CHARS: return text
    return text[:OUTPUT_CHARS] + "\n[truncated: ask for fewer items with page_size]"

async def call(uid, operation, args, target="cloud", task_id=None, wait=60):
    """Run one operation as the owner. task_id binds it to a running task and its approvals;
    without one, only reads are allowed."""
    from personal_agent.computer.devices import list_devices
    from personal_agent.sessions.session_manager import dispatch
    from personal_agent.sessions import session_queue
    args = dict(args or {})
    if operation == DEVICE_LIST:
        return {"status": "completed", "result": list_devices(uid)}
    if operation not in READS and not task_id:
        raise PermissionError("Only a background task may change the user's HugAgentOS")
    user, grant = await run_in_session(_caller, uid, task_id)
    job = await dispatch(user, target, operation, args,
        request_key(task_id, target, operation, args), grant)
    # A device answers through its queue; poll it without holding a session.
    end = time.monotonic() + wait
    while job.get("id") and job.get("status") in {"queued", "sent"} and time.monotonic() < end:
        await asyncio.sleep(0.3)
        job = await run_in_session(session_queue.view, uid, job["id"], task_id)
    if not job.get("id"): return job  # pending_approval, rejected or an earlier receipt
    if job["status"] in {"queued", "sent"}: return {"status": "waiting_for_device", "operation_id": job["id"]}
    return {"status": job["status"], "result": job.get("result")}

async def follow(uid, args, target, task_id, seconds):
    """run.status until the run ends or the time is up."""
    end = time.monotonic() + max(0, min(MAX_WAIT, seconds))
    while True:
        result = await call(uid, "run.status", args, target, task_id)
        status = (result.get("result") or {}).get("status") if isinstance(result.get("result"), dict) else None
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

def build(uid, task_id=None):
    """The tool function for one context: a task (task_id) or a conversation turn."""
    writes = bool(task_id)

    async def pa_hugagent(operation: str, args: dict | None = None, target: str = "cloud", wait_seconds: int = 0):
        try:
            if writes and operation == "run.status" and wait_seconds > 0 and target == "cloud":
                result = await follow(uid, args or {}, target, task_id, wait_seconds)
            else:
                result = await call(uid, operation, args, target, task_id)
                status = (result.get("result") or {}).get("status") if isinstance(result.get("result"), dict) else None
                if operation == "run.status" and task_id and status in RUN_TERMINAL:
                    observed(task_id, uid, (args or {})["run_id"])
        except HTTPException as exc:
            result = {"error": str(exc.detail), "http_status": exc.status_code}
        except (ValueError, LookupError, PermissionError) as exc:
            result = {"error": str(exc)}
        from agentscope.message import TextBlock
        from agentscope.tool._response import ToolChunk
        return ToolChunk(content=[TextBlock(type="text", text=bounded(result))])

    pa_hugagent.__annotations__["operation"] = Literal[tuple(operations(writes))]
    args_doc = ["operation: the operation to run", "args: the operation's arguments, as listed for it",
        "target: cloud, or device:<id> for HugAgentOS on one of the user's computers"]
    if writes: args_doc.append("wait_seconds: run.status only, on cloud: wait up to this many seconds (at most 600) for the run to end")
    else: args_doc.append("wait_seconds: not used in a conversation turn")
    pa_hugagent.__doc__ = describe(writes) + "\n\nArgs:\n" + "\n".join("    " + line for line in args_doc)
    return pa_hugagent
