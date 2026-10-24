"""Tools of the agent's own turn: delegate to tasks, read them, remember, schedule.

They act only while the turn that runs them still owns its message (see front.owned).
Work with external effects (Bash, files, email, the computer, writes to the user's
HugAgentOS) belongs to tasks; a turn may only read the user's HugAgentOS sessions.
"""
from core.llm.factory.tools.definitions import function_tool
import json
from contextlib import contextmanager
from typing import Literal
from agentscope.message import TextBlock
from agentscope.tool._response import ToolChunk
from sqlalchemy import select
from core.db.engine import SessionLocal
from core.db.models import ChatMessage
from personal_agent.models import PersonalAgent, PersonalWorkItem
from personal_agent.tasks import task_ops
from core.db.threads import off_loop

def response(value):
    return ToolChunk(content=[TextBlock(type="text", text=json.dumps(value, ensure_ascii=False, default=str))])

def register_front_tools(toolkit, request, loader=None):
    from personal_agent.conversation.front import EVENT_ROLE, current_turn, owned
    from personal_agent.workspace.constants import WORKSPACE
    from personal_agent.workspace.files import read_file
    from personal_agent.workspace.files import write_file
    from personal_agent.scheduling.schedules import create, delete, listing, is_unattended_run
    from personal_agent.scheduling.heartbeat import KEY_PREFIX as BEAT_PREFIX
    from personal_agent.context import AgentContext
    ident, uid = request.profile_ref, str(request.current_user_id)
    versions = {}

    def turn(changes_tasks=False, beat_ok=False):
        """The running turn. changes_tasks: only a user message may change tasks; beat_ok also lets
        a heartbeat check start its one task."""
        state = current_turn.get()
        if not state or state["ident"] != ident: raise PermissionError("This conversation turn is not active")
        if changes_tasks and state["kind"] != "message" and not (beat_ok and state["kind"] == "heartbeat"):
            raise PermissionError("This turn reports a result; tasks change only on the user's request")
        return state

    @contextmanager
    def checked(state, lock=False):
        """A session in which the turn still owns its message."""
        with SessionLocal() as db:
            owned(db, state, lock)
            yield db

    @contextmanager
    def changing(state):
        """The owner's service and the turn's message; commits when the block succeeds.
        Read results inside the block: rows are expired after the commit."""
        with checked(state, lock=True) as db:
            yield AgentContext(db, uid, chat_id=db.get(ChatMessage, ident).chat_id), db.get(ChatMessage, ident)
            db.commit()

    @off_loop
    def pa_task_create(goal: str, title: str = "", related_task_ids: list[str] | None = None) -> ToolChunk:
        """Start a background task. goal: a self-contained instruction in the user's language with the
        requirements and the expected result; the task sees only the last few messages. title: a short
        name of at most 20 characters. related_task_ids: existing tasks this new deliverable builds on.
        Files attached to the user's message go to the task."""
        state = turn(changes_tasks=True, beat_ok=True)
        beat = state["kind"] == "heartbeat"
        if beat and state["count"] >= 1: raise PermissionError("A heartbeat check starts at most one task")
        # The key repeats on a retried turn, so the same task is not started twice.
        # A heartbeat's key marks the task as unattended: its result may stay quiet.
        key = (BEAT_PREFIX + "task:" + ident) if beat else "turn:" + ident + ":" + str(state["count"])
        with changing(state) as (svc, message):
            task = task_ops.create(svc, message, goal, key[:100],
                attachments=state["attachments"], related=related_task_ids or [], title=title)
            result = {"started": True, "task_id": task.id, "title": task.label_text}
        state["count"] += 1
        return response(result)

    @off_loop
    def pa_task_update(task_id: str, instruction: str) -> ToolChunk:
        """Continue or change one existing task, including a completed task. Use only for a clear follow-up to its goal or result, never because it is recent or the only task. Read pa_task_get first before updating a task unless the user explicitly selected its ID. instruction: only what applies to this task."""
        state = turn(changes_tasks=True)
        with checked(state) as db:
            selected = (owned(db, state).payload or {}).get("work_item_ids") or []
        if selected and task_id not in selected:
            raise ValueError("The user selected a different task; do not redirect this message")
        if task_id not in selected and task_id not in state.get("inspected_tasks", set()):
            raise ValueError("Read pa_task_get first and check that the latest user message continues its goal or result. If unrelated, answer directly or create a new task; if ambiguous, ask the user.")
        with changing(state) as (svc, message):
            result = {"updated": True, "status": task_ops.update_task(svc, message, task_id, instruction, attachments=state["attachments"]).status}
        return response(result)

    @off_loop
    def pa_task_control(task_id: str, action: Literal["pause", "resume", "cancel"]) -> ToolChunk:
        """Pause, resume or cancel one task. Resume never approves a pending action; the user approves
        those in the approval card. Resume applies to waiting tasks; to continue a completed, failed or
        cancelled task, read it and use pa_task_update with the user's follow-up instruction."""
        with changing(turn(changes_tasks=True)) as (svc, message):
            result = {"status": task_ops.control(svc, message, task_id, action).status}
        return response(result)

    @off_loop
    def pa_task_get(task_id: str) -> ToolChunk:
        """Read one task: goal, status, progress, the user's updates and the result."""
        from personal_agent.conversation.associations import instructions
        with checked(turn()) as db:
            task = AgentContext(db, uid, chat_id=db.get(ChatMessage, ident).chat_id).tasks.get(task_id)
            state = task.checkpoint or {}
            turn().setdefault("inspected_tasks", set()).add(task_id)
            return response({"title": task.label_text, "goal": task.goal[:4000], "status": task.status,
                "waiting_reason": task.waiting_reason, "progress": state.get("progress"), "pending_approval": bool(state.get("pending_approval")),
                "updates": instructions(db, db.get(ChatMessage, ident).chat_id, task.id, 10),
                "result": (task.result or "")[:12000], "updated": task.updated_at})

    @off_loop
    def pa_history_search(query: str, limit: int = 10) -> ToolChunk:
        """Search earlier messages and task results of this conversation."""
        with checked(turn()) as db:
            chat = db.get(ChatMessage, ident).chat_id
            rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat,
                ChatMessage.role.in_(["user", "assistant", EVENT_ROLE]), ChatMessage.content.ilike("%" + query[:200] + "%"))
                .order_by(ChatMessage.chat_seq.desc()).limit(max(1, min(30, limit)))).all()
            return response([{"role": m.role, "at": m.created_at, "content": m.content[:3000]} for m in rows])

    def workspace():
        """The agent whose workspace files the turn may change."""
        state = turn()
        with checked(state): return state["agent_id"]

    def saved(data):
        # A hand-written name line in IDENTITY.md renames the agent everywhere, as in the editor.
        if data["path"] == WORKSPACE + "/IDENTITY.md":
            with SessionLocal() as db: AgentContext(db, uid).identity.adopt_identity_name(data.get("content") or "")
        return response({"path": data["path"], "saved": True})

    @off_loop
    def pa_memory_read(path: str) -> ToolChunk:
        """Read a workspace persona or memory file, for example USER.md, MEMORY.md or memory/2026-10-10.md."""
        data = read_file(workspace(), path)
        versions[data["path"]] = data["etag"]
        text = data["content"] or ""
        daily = data["path"].startswith(WORKSPACE + "/memory/")
        return response({"path": data["path"], "content": text[-50000:] if daily else text[:50000],
                         "truncated": data["truncated"] or len(text) > 50000, "slice": "tail" if daily else "head"})

    @off_loop
    def pa_memory_edit(path: str, old_string: str, new_string: str) -> ToolChunk:
        """Replace one exact, unique passage of a persona or memory file. Read the file first."""
        agent = workspace()
        current = read_file(agent, path)
        text = current["content"] or ""
        if not old_string or text.count(old_string) != 1: raise ValueError("old_string must occur exactly once; read the file again")
        data = write_file(agent, path, text.replace(old_string, new_string, 1), current["etag"])
        versions[data["path"]] = data["etag"]
        return saved(data)

    @off_loop
    def pa_memory_write(path: str, content: str) -> ToolChunk:
        """Create or replace a persona or memory file, for example a new daily note memory/YYYY-MM-DD.md."""
        from personal_agent.workspace.files import relative
        target = WORKSPACE + "/" + relative(path)
        data = write_file(workspace(), path, content, versions.get(target, ""))
        versions[target] = data["etag"]
        return saved(data)

    @off_loop
    def pa_set_name(name: str) -> ToolChunk:
        """Change your own name when the user gives you a new one."""
        with checked(turn()) as db:
            return response({"name": AgentContext(db, uid).identity.configure({"name": name})["config"]["name"]})

    @off_loop
    def pa_schedule_create(title: str, instruction: str, cron: str = "", run_at: str = "", timezone: str = "") -> ToolChunk:
        """Create a scheduled task that later runs as a background task. cron: five fields in local time, such as
        "0 8 * * *" for 08:00 daily; or run_at: one ISO local time. timezone defaults to the user's."""
        with checked(turn(changes_tasks=True)) as db:
            return response(create(db, uid, title, instruction, cron, run_at, timezone))

    @off_loop
    def pa_schedule_list() -> ToolChunk:
        """List the scheduled tasks and their next run times."""
        with checked(turn()) as db:
            return response(listing(db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == uid))))

    @off_loop
    def pa_schedule_update(schedule_id: str, title: str | None = None, instruction: str | None = None,
                                 cron: str | None = None, run_at: str | None = None,
                                 timezone: str | None = None, enabled: bool | None = None) -> ToolChunk:
        """Edit an existing schedule in place. List schedules first to identify it. Omit unchanged fields;
        use enabled=false to pause. Give cron for repeats or run_at for a one-time ISO local time."""
        from personal_agent.scheduling.schedules import update_schedule
        patch = {k: v for k, v in {"title": title, "instruction": instruction, "cron": cron,
                 "run_at": run_at, "timezone": timezone, "enabled": enabled}.items() if v is not None}
        with checked(turn(changes_tasks=True)) as db:
            return response(update_schedule(db, uid, schedule_id, patch))

    @off_loop
    def pa_schedule_delete(schedule_id: str) -> ToolChunk:
        """Delete one scheduled task when the user asks to stop it."""
        with checked(turn(changes_tasks=True)) as db:
            return response(delete(db, uid, schedule_id))

    @off_loop
    def pa_stay_quiet(reason: str) -> ToolChunk:
        """Send nothing to the user for this scheduled result or heartbeat check, because it has nothing new or noteworthy."""
        state = turn()
        if state["kind"] == "event":
            # Only a scheduled or heartbeat check may end without telling the user; a requested result is always reported.
            with SessionLocal() as db: unattended = is_unattended_run(db.get(PersonalWorkItem, state["task_id"]))
            if not unattended: raise PermissionError("A requested task result must be reported to the user")
        elif state["kind"] != "heartbeat": raise PermissionError("Only a scheduled result or a heartbeat check may stay quiet")
        with checked(state): state["quiet"] = True
        return response({"quiet": True, "next": "Stop now. Do not write a message."})

    state = current_turn.get() or {}
    if state.get("ident") == ident and state.get("kind") == "heartbeat":
        # A heartbeat check reads, notes and may start one task. It never changes the user's tasks or schedules.
        tools = [pa_task_create, pa_task_get, pa_history_search, pa_memory_read, pa_memory_edit, pa_memory_write,
            pa_schedule_list, pa_stay_quiet]
    else:
        # The same tools every turn keep the prompt cache warm; pa_stay_quiet checks the turn when called.
        tools = [pa_task_create, pa_task_update, pa_task_control, pa_task_get, pa_history_search, pa_memory_read, pa_memory_edit,
            pa_memory_write, pa_set_name, pa_schedule_create, pa_schedule_list, pa_schedule_update, pa_schedule_delete, pa_stay_quiet]
    # The user's HugAgentOS projects, chats and runs: reads only in a conversation turn.
    from personal_agent.sessions.session_tool import build as session_tool
    tools.append(session_tool(uid))
    for fn in tools: toolkit.append(function_tool(fn))
