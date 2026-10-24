"""Explicit Personal Agent tool surface; all targets derive from authenticated task."""
from core.llm.factory.tools.definitions import function_tool
import personal_agent.workspace.files as _pa_files

from personal_agent.tasks.guards import owned_task
from personal_agent.tools.responses import response
from personal_agent.workspace.paths import workspace_path

from agentscope.tool._response import ToolChunk
from core.db.engine import SessionLocal
from sqlalchemy import select
from core.db.models import ChatMessage
from core.sandbox.protocol import ProcessRequest
from personal_agent.models import PersonalWorkItem
from personal_agent.computer.sandbox import get_personal_sandbox_provider
from core.db.threads import off_loop

def register_personal_tools(toolkit, request, loader=None):
    ident, uid = request.profile_ref, str(request.current_user_id)
    # Paths read or written in this run. An edit needs the current content first.
    known = set()
    versions = {}
    from personal_agent.workspace import persona

    @off_loop
    def pa_task_status() -> ToolChunk:
        """Read this task's latest user updates and pending actions."""
        state = owned_task(ident, uid, allow_pending=True)
        with SessionLocal() as db:
            task = db.get(PersonalWorkItem, ident)
            from personal_agent.conversation.associations import instructions
            return response({"goal": task.goal,
                "updates": instructions(db, state["chat"], ident, 100), "checkpoint": task.checkpoint})

    @off_loop
    def pa_task_checkpoint(summary: str, next_step: str = ""):
        """Save bounded task progress before a long step, interruption or approval."""
        owned_task(ident, uid, allow_pending=True)
        if len(summary) > 4000 or len(next_step) > 1000: raise ValueError("Checkpoint is too long")
        from personal_agent.context import AgentContext
        with SessionLocal() as db:
            task = AgentContext(db, uid).tasks.get(ident, lock=True)
            owned_task(ident, uid, allow_pending=True)
            task.checkpoint = {**(task.checkpoint or {}), "progress": {"summary": summary, "next_step": next_step}}
            db.commit()
        return response({"saved": True})

    async def pa_bash(command: str, timeout_seconds: int = 60) -> ToolChunk:
        """Run Bash inside the dedicated cloud computer. Never runs on the user's device."""
        state = owned_task(ident, uid)
        duration = max(1, min(300, timeout_seconds))
        provider = get_personal_sandbox_provider()
        from personal_agent.sessions.session_cli_install import ensure_cli
        await ensure_cli(provider, state["box"], uid)
        import os, shlex
        from personal_agent.sessions.session_auth import mint, key
        from core.infra.ephemeral import get_ephemeral_state
        token = await mint(ident, uid, duration + 30)
        server = os.environ.get("PERSONAL_AGENT_CLI_SERVER", "http://backend:3001")
        from hugagent_cli.transport import enabled
        # Most deployments are private networks without TLS, so plain HTTP is allowed unless disabled.
        allow_http = "1" if enabled(os.environ.get("PERSONAL_AGENT_CLI_ALLOW_HTTP", "1")) else "0"
        script = ("export HUGAGENT_SERVER=" + shlex.quote(server) + " HUGAGENT_API_KEY=" + shlex.quote(token) + " HUGAGENT_ALLOW_HTTP=" + allow_http
                  + "\n" + command)
        try:
            result = await provider.run_to_completion(ProcessRequest(script_content=script, script_name="personal-task.sh", language="bash", timeout=duration, user_id=uid, session_id=state["box"]))
        finally:
            await get_ephemeral_state().drop(key(token))
        return response({"stdout": result.stdout[-50000:].replace(token,"[task credential]"), "stderr": result.stderr[-10000:].replace(token,"[task credential]"), "exit_code": result.exit_code})

    async def pa_read_file(path: str) -> ToolChunk:
        """Read UTF-8 text from the dedicated cloud workspace, with a bounded result."""
        state = owned_task(ident, uid)
        target = workspace_path(path)
        if _pa_files.editable(_pa_files.relative(target)):
            data = _pa_files.read_file(state["agent_id"], target)
            versions[target] = data["etag"]
            known.add(target)
            return response({"path": path, "content": data["content"], "truncated": data["truncated"]})
        content = await get_personal_sandbox_provider().get_file(state["box"], target, user_id=uid)
        if len(content) > 2*1024*1024: raise ValueError("Use Bash to inspect a large file")
        known.add(workspace_path(path))
        return response({"path": path, "content": content.decode("utf-8")[:50000]})

    async def pa_write_file(path: str, content: str) -> ToolChunk:
        """Write a task file in the dedicated workspace; does not write to MySpace."""
        state = owned_task(ident, uid)
        if len(content.encode()) > 2*1024*1024: raise ValueError("File too large")
        target = workspace_path(path)
        if _pa_files.editable(_pa_files.relative(target)):
            data = _pa_files.write_file(state["agent_id"], target, content, versions.get(target, ""))
            versions[target] = data["etag"]
        else:
            await get_personal_sandbox_provider().put_file(state["box"], target, content, user_id=uid)
        known.add(workspace_path(path))
        return response({"path": path, "saved": True})

    async def pa_edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolChunk:
        """Replace exact text in a workspace file, such as one entry of USER.md or MEMORY.md.
        Read the file with pa_read_file first. old_string must match exactly and be unique unless replace_all is true."""
        state = owned_task(ident, uid)
        target = workspace_path(path)
        if target not in known: raise ValueError("Read the file with pa_read_file before editing it")
        if not old_string: raise ValueError("old_string must not be empty")
        if old_string == new_string: raise ValueError("old_string and new_string are identical")
        provider = get_personal_sandbox_provider()
        memory_file = _pa_files.editable(_pa_files.relative(target))
        if memory_file:
            current = _pa_files.read_file(state["agent_id"], target)
            content = (current["content"] or "").encode()
        else:
            content = await provider.get_file(state["box"], target, user_id=uid)
        if len(content) > 2*1024*1024: raise ValueError("Use Bash to edit a large file")
        try: text = content.decode("utf-8")
        except UnicodeDecodeError: raise ValueError("Only UTF-8 text files can be edited") from None
        count = text.count(old_string)
        if count == 0: raise ValueError("old_string was not found; read the file again")
        if count > 1 and not replace_all: raise ValueError("old_string occurs " + str(count) + " times; add context or set replace_all")
        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        if len(updated.encode()) > 2*1024*1024: raise ValueError("File too large")
        if memory_file:
            data = _pa_files.write_file(state["agent_id"], target, updated, current["etag"])
            versions[target] = data["etag"]
        else:
            await provider.put_file(state["box"], target, updated, user_id=uid)
        return response({"path": path, "replacements": count if replace_all else 1})

    @off_loop
    def pa_history_search(query: str, limit: int = 20):
        """Retrieve earlier final replies from the owner's one lifelong conversation."""
        state = owned_task(ident, uid)
        with SessionLocal() as db:
            rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == state["chat"],
                ChatMessage.role.in_(["user", "assistant"]), ChatMessage.content.ilike("%" + query[:200] + "%"))
                .order_by(ChatMessage.chat_seq.desc()).limit(max(1, min(50, limit)))).all()
            return response([{"seq": m.chat_seq, "role": m.role, "content": m.content[:4000]} for m in rows])

    async def pa_skill_read(skill_id: str, relative_path: str = "SKILL.md"):
        """Read one explicitly enabled skill instruction or reference. Skills are not mounted."""
        state = owned_task(ident, uid)
        from personal_agent.identity.capabilities import capability_ids
        if loader is None or skill_id not in capability_ids(state["config"])[0]: raise PermissionError("Skill not enabled")
        from pathlib import Path
        base = loader.get_skill_dir(skill_id)
        if not base: raise LookupError("Skill not available")
        root = Path(base).resolve()
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root) or not path.is_file(): raise ValueError("Invalid skill reference")
        if path.stat().st_size > 200000: raise ValueError("Skill reference too large")
        return response({"skill_id": skill_id, "path": relative_path, "content": path.read_text()})

    @off_loop
    def pa_finish(answer: str) -> ToolChunk:
        """Complete the task with final text in the user's language, then stop.
        To show an image/file in the main conversation, call pa_deliver before this.
        Already delivered outputs remain visible; do not export or send them again."""
        state = owned_task(ident, uid, allow_pending=True)
        if not answer.strip() or len(answer) > 60000: raise ValueError("Answer must contain 1 to 60000 characters")
        from personal_agent.context import AgentContext
        with SessionLocal() as db:
            task = AgentContext(db, uid).tasks.get(ident, lock=True)
            task.checkpoint = {**(task.checkpoint or {}), "final_answer": {"run_id": state["run_id"], "text": answer.strip()}}
            db.commit()
        return response({"delivered": True, "next": "Stop now. Do not write more text."})

    @off_loop
    def pa_set_name(name: str) -> ToolChunk:
        """Change your own name when the user asks for a new one. Updates settings, IDENTITY.md and the name shown in the app."""
        owned_task(ident, uid)
        from personal_agent.context import AgentContext
        with SessionLocal() as db:
            view = AgentContext(db, uid).identity.configure({"name": name})
        return response({"name": view["config"]["name"]})

    @off_loop
    def pa_schedule_create(title: str, instruction: str, cron: str = "", run_at: str = "", timezone: str = "") -> ToolChunk:
        """Create a scheduled task that runs later as a new task. Use cron (five fields, local time) for repeats,
        for example "0 8 * * *" for 08:00 every day, or run_at (ISO local time) for one time. timezone defaults to the user's."""
        owned_task(ident, uid)
        from personal_agent.scheduling.schedules import create
        with SessionLocal() as db:
            return response(create(db, uid, title, instruction, cron, run_at, timezone))

    async def pa_schedule_list() -> ToolChunk:
        """List the user's scheduled tasks and their next run times."""
        state = owned_task(ident, uid)
        from personal_agent.scheduling.schedules import listing
        return response(listing(state["config"]))

    @off_loop
    def pa_schedule_update(schedule_id: str, title: str | None = None, instruction: str | None = None,
                                 cron: str | None = None, run_at: str | None = None,
                                 timezone: str | None = None, enabled: bool | None = None) -> ToolChunk:
        """Edit an existing schedule in place. List schedules first to identify it. Omit unchanged fields;
        use enabled=false to pause. Give cron for repeats or run_at for a one-time ISO local time."""
        from personal_agent.scheduling.schedules import update_schedule
        patch = {k: v for k, v in {"title": title, "instruction": instruction, "cron": cron,
                 "run_at": run_at, "timezone": timezone, "enabled": enabled}.items() if v is not None}
        owned_task(ident, uid)
        with SessionLocal() as db:
            return response(update_schedule(db, uid, schedule_id, patch))

    @off_loop
    def pa_schedule_delete(schedule_id: str) -> ToolChunk:
        """Delete one scheduled task when the user asks to stop it."""
        owned_task(ident, uid)
        from personal_agent.scheduling.schedules import delete
        with SessionLocal() as db:
            return response(delete(db, uid, schedule_id))

    from personal_agent.scheduling.schedules import is_unattended_run
    with SessionLocal() as db: scheduled = is_unattended_run(db.get(PersonalWorkItem, ident))
    # A scheduled run does its work now. Without these tools it cannot schedule itself again.
    managing = () if scheduled else (pa_schedule_create, pa_schedule_update, pa_schedule_delete)
    for fn in (pa_task_status, pa_task_checkpoint, pa_bash, pa_read_file, pa_write_file, pa_edit_file, pa_history_search, pa_skill_read,
               pa_set_name, pa_finish, pa_schedule_list, *managing):
        toolkit.append(function_tool(fn))
    from personal_agent.delivery.delivery import build as delivery_tool
    toolkit.append(function_tool(delivery_tool(ident, uid)))
    # The user's HugAgentOS sessions; writes wait for the user's approval.
    from personal_agent.sessions.session_tool import build as session_tool
    toolkit.append(function_tool(session_tool(uid, ident)))
    from personal_agent.tools.email import register_email_tools
    from personal_agent.computer.computer import register_computer_tools
    register_email_tools(toolkit, ident, uid)
    register_computer_tools(toolkit, ident, uid)
