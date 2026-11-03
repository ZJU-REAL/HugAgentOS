"""Tools of the agent's own turn: delegate to tasks, read them, remember, schedule.

They act only while the turn that runs them still holds its lease (see router/turn.owned).
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
from personal_agent.models import PersonalAgent
from personal_agent.tasks import task_ops
from core.db.threads import off_loop

def response(value):
    return ToolChunk(content=[TextBlock(type="text", text=json.dumps(value, ensure_ascii=False, default=str))])

def register_front_tools(toolkit, request, loader=None):
    from personal_agent.conversation.router.kinds import EVENT_ROLE, speaker_role
    from personal_agent.conversation.router.turn import current_turn, owned
    from personal_agent.workspace.constants import WORKSPACE
    from personal_agent.workspace.files import read_file
    from personal_agent.workspace.files import write_file
    from personal_agent.scheduling.schedules import create, delete, listing
    from personal_agent.scheduling.heartbeat import KEY_PREFIX as BEAT_PREFIX
    from personal_agent.context import AgentContext
    ident, uid = request.profile_ref, str(request.current_user_id)
    versions = {}

    def turn(changes_tasks=False, beat_ok=False):
        """The running turn. changes_tasks: only the user's messages may change tasks; beat_ok also lets
        a heartbeat check start its one task."""
        state = current_turn.get()
        if not state or state.id != ident: raise PermissionError("This conversation turn is not active")
        if changes_tasks and not state.grants.change_tasks and not (beat_ok and state.grants.create_one_task):
            raise PermissionError("This turn reports a result; tasks change only on the user's request")
        return state

    @contextmanager
    def checked(state, lock=False):
        """A session in which the turn still holds its lease."""
        with SessionLocal() as db:
            owned(db, state, lock)
            yield db

    @contextmanager
    def changing(state, sources=None):
        """The owner's service and the message the change comes from (the first of sources);
        commits when the block succeeds. Read results inside the block: rows are expired after the commit."""
        sources = sources or state.sources()
        with checked(state, lock=True) as db:
            yield AgentContext(db, uid, chat_id=state.chat_id), db.get(ChatMessage, sources[0].message_id)
            db.commit()

    def attribute(db, task, others, context=False):
        """Link the other messages a task change came from. context: a new task's context also covers them."""
        for trigger in others:
            task_ops.link(db, trigger.message_id, task.id, "related")
            seq = db.get(ChatMessage, trigger.message_id).chat_seq
            if context and (task.context_seq or 0) < seq: task.context_seq = seq

    @off_loop
    def pa_task_create(goal: str, title: str = "", related_task_ids: list[str] | None = None,
                       from_messages: list[int] | None = None) -> ToolChunk:
        """Start a background task. goal: a self-contained instruction in the user's language with the
        requirements and the expected result; the task sees only the last few messages. title: a short
        name of at most 20 characters. related_task_ids: existing tasks this new deliverable builds on.
        from_messages: when this turn has numbered inputs, the numbers ([#n]) of the user's messages the task
        comes from; omitted, all of them. Files attached to those messages go to the task."""
        state = turn(changes_tasks=True, beat_ok=True)
        beat = not state.grants.change_tasks
        if beat and state.count >= 1: raise PermissionError("A heartbeat check starts at most one task")
        # The key repeats on a retried turn, so the same task is not started twice.
        # A heartbeat's key marks the task as unattended: its result may stay quiet.
        key = (BEAT_PREFIX + "task:" + state.head) if beat else "turn:" + state.head + ":" + str(state.count)
        sources = state.sources(from_messages)
        with changing(state, sources) as (svc, message):
            task = task_ops.create(svc, message, goal, key[:100],
                attachments=state.attachments(sources), related=related_task_ids or [], title=title)
            attribute(svc.db, task, sources[1:], context=True)
            result = {"started": True, "task_id": task.id, "title": task.label_text}
        state.count += 1
        return response(result)

    @off_loop
    def pa_task_update(task_id: str, instruction: str, from_messages: list[int] | None = None) -> ToolChunk:
        """Continue or change one existing task, including a completed task. Use only for a clear follow-up to its goal or result, never because it is recent or the only task. Read pa_task_get first before updating a task unless the user explicitly selected its ID. instruction: only what applies to this task. from_messages: as for pa_task_create."""
        state = turn(changes_tasks=True)
        sources = state.sources(from_messages)
        selected = state.selected(sources)
        if selected and task_id not in selected:
            # In a turn with several inputs, another message's selection must not block this one.
            raise ValueError("The user selected a different task for these messages; do not redirect them."
                             + (" If another input of this turn asks about this task, pass that input's number"
                                " as from_messages." if from_messages is None and len(state.triggers) > 1 else ""))
        if task_id not in selected and task_id not in state.inspected_tasks:
            raise ValueError("Read pa_task_get first and check that the latest user message continues its goal or result. If unrelated, answer directly or create a new task; if ambiguous, ask the user.")
        with changing(state, sources) as (svc, message):
            task = task_ops.update_task(svc, message, task_id, instruction, attachments=state.attachments(sources))
            attribute(svc.db, task, sources[1:])
            result = {"updated": True, "status": task.status}
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
        current = turn()
        with checked(current) as db:
            task = AgentContext(db, uid, chat_id=current.chat_id).tasks.get(task_id)
            state = task.checkpoint or {}
            current.inspected_tasks.add(task_id)
            return response({"title": task.label_text, "goal": task.goal[:4000], "status": task.status,
                "waiting_reason": task.waiting_reason, "progress": state.get("progress"), "pending_approval": bool(state.get("pending_approval")),
                "updates": instructions(db, current.chat_id, task.id, 10),
                "result": (task.result or "")[:12000], "updated": task.updated_at})

    @off_loop
    def pa_history_search(query: str, limit: int = 10) -> ToolChunk:
        """Search earlier messages and task results of this conversation."""
        current = turn()
        with checked(current) as db:
            rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == current.chat_id,
                ChatMessage.role.in_(["user", "assistant", EVENT_ROLE]), ChatMessage.content.ilike("%" + query[:200] + "%"))
                .order_by(ChatMessage.chat_seq.desc()).limit(max(1, min(30, limit)))).all()
            return response([{"role": speaker_role(m), "at": m.created_at, "content": m.content[:3000]} for m in rows])

    def workspace():
        """The agent whose workspace files the turn may change."""
        state = turn()
        with checked(state): return state.agent_id

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
        # Only a scheduled result or a heartbeat check may end without telling the user. A requested
        # result is always reported, and a user's message always answered, also when they share the turn.
        if not state.grants.quiet:
            if state.messages: raise PermissionError("The user wrote in this turn; answer them")
            raise PermissionError("A requested task result must be reported to the user")
        with checked(state): state.quiet = True
        return response({"quiet": True, "next": "Stop now. Do not write a message."})

    state = current_turn.get()
    if state is not None and state.id == ident and state.grants.readonly:
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
