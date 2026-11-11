"""The Worker's own tools: structured Reports and the guarded core ``Bash`` (SUPERAGENT_DESIGN §6.3, §8.2)."""
import json
import logging
from contextlib import suppress
import os
import re
import shlex
import time
from typing import Literal

from agentscope.message import TextBlock
from agentscope.tool._response import ToolChunk

from core.db.engine import SessionLocal
from core.db.threads import off_loop, run_db
from core.llm.factory.tools.definitions import function_tool
from personal_agent.tasks.guards import owned_task
from personal_agent.tools.responses import response

log = logging.getLogger(__name__)
# New input reaches the Worker within this long while a command runs (§6.3).
YIELD_MS = 5000
CREDENTIAL = re.compile(r"pa_cli_[A-Za-z0-9_-]{20,}")
# A command that only waits: each step is a sleep, a plain echo (no redirect or expansion) or a no-op.
_STEP = re.compile(r"\s*(?:;|&&|\n)\s*")
_SLEEP = re.compile(r"sleep\s+[0-9.]+[smhd]?")
_IDLE = re.compile(r"(?:echo(?:\s+[^;&|`$()<>\\]*)?|true|:)")


def waits_only(command):
    """True when ``command`` does nothing but sleep (and echo). Linear: no backtracking regex."""
    steps = [step for step in _STEP.split((command or "").strip()) if step]
    return (any(_SLEEP.fullmatch(step) for step in steps)
            and all(_SLEEP.fullmatch(step) or _IDLE.fullmatch(step) for step in steps))
STATUSES = ("progress", "waiting", "ready", "blocked", "failed", "dropped")


def redact(chunk):
    """Hide task CLI credentials in a tool result; the command itself may print them."""
    blocks = [TextBlock(type="text", text=CREDENTIAL.sub("[task credential]", block.text))
              if isinstance(block, TextBlock) else block for block in chunk.content]
    return ToolChunk(content=blocks, state=chunk.state, is_last=chunk.is_last, metadata=chunk.metadata)


def payload(chunk):
    try:
        return json.loads(chunk.content[0].text)
    except (AttributeError, IndexError, TypeError, ValueError):
        return {}


def live_commands(chat_id):
    """Commands of this Worker session that are still running."""
    from core.db.models.command import CommandTask
    from personal_agent.worker.settle import LIVE_COMMANDS
    with SessionLocal() as db:
        return [row.task_id for row in db.query(CommandTask.task_id).filter(
            CommandTask.chat_id == chat_id, CommandTask.status.in_(LIVE_COMMANDS))]


def register_report(toolkit, ident, uid):
    @off_loop
    def pa_report(status: Literal["progress", "waiting", "ready", "blocked", "failed", "dropped"], summary: str,
                  question: str = "") -> ToolChunk:
        """Report this task's state. The task is settled from your last report when you stop.
        status: "progress" saves where you are (the task goes on; call it before a long step);
        "waiting" when the task now waits for something outside you, such as a remote session;
        "ready" when the work is finished: summary is the complete, concise answer in the user's
        language with verified output links; "blocked" when you cannot go on without the user,
        with `question` set to one concrete question (the answer arrives as a new instruction);
        "failed" when the task cannot be done, with the reason; "dropped" when there turned out to
        be nothing for the user (already done, not reproducible, no request): the user is not messaged.
        After ready, blocked, failed or dropped, stop. Already delivered outputs remain visible."""
        state = owned_task(ident, uid, allow_pending=True)
        if status not in STATUSES: raise ValueError("Unknown report status")
        if not summary.strip() or len(summary) > 60000: raise ValueError("Summary must contain 1 to 60000 characters")
        if status == "progress" and len(summary) > 4000: raise ValueError("A progress report must contain at most 4000 characters")
        if status == "blocked" and not question.strip(): raise ValueError("A blocked task needs one concrete question for the user")
        if len(question) > 1000: raise ValueError("Question must contain at most 1000 characters")
        from personal_agent.context import AgentContext
        from personal_agent.channels.im_actions import new_question
        from personal_agent.scheduling.proactive_tasks import plain
        # A question and a reason are shown to the user as written; the marks of a summary do not belong there.
        question = plain(question)
        if status in ("blocked", "failed", "waiting"): summary = plain(summary) or summary
        with SessionLocal() as db:
            task = AgentContext(db, uid).tasks.get(ident, lock=True)
            owned_task(ident, uid, allow_pending=True)
            from personal_agent.scheduling.schedules import is_proactive
            if status == "blocked" and is_proactive(task):
                # A question would hold the summary until the user answers; nobody is there to answer.
                raise ValueError("A proactive check cannot ask the user. Put the open point in the report and use ready.")
            if status == "ready" and is_proactive(task):
                from personal_agent.scheduling import proactive_tasks
                summary, unknown = proactive_tasks.known_mail_only(task, summary)
                if unknown: log.warning("Proactive task %s reported mail it never read: %s", ident, ", ".join(unknown))
                # Nothing it reported could be checked: the same as nothing to report.
                if not summary: status, summary = "dropped", "报告的邮件事项在邮箱里查不到，已全部丢弃。"
            report = {"assignment": state["run_id"], "status": status, "summary": summary.strip(),
                      **({"question": question.strip()} if question.strip() else {}), "at": time.time()}
            checkpoint = {**(task.checkpoint or {}), "report": report}
            if status == "progress":
                checkpoint["progress"] = {"summary": summary.strip()}
            if status == "blocked":
                # The task waits on this reason and the question is posted once.
                asked = new_question(question)
                checkpoint.update(pending_question=asked, task_wait_reason=asked["text"][:300])
            task.checkpoint = checkpoint
            db.commit()
        if status == "progress":
            return response({"saved": True, "next": "Continue the task."})
        return response({"reported": status, "next": "Stop now. Do not write more text."})

    toolkit.append(function_tool(pa_report))


def register_bash(toolkit, request, loader, ident, uid):
    """Core ``Bash``, ``write_stdin`` and ``list_processes`` on the agent's own computer.

    A command runs in the task folder with the task's HugAgentOS CLI credentials; it moves
    to the background after ``yield_time_ms`` and its completion wakes this Worker."""
    from core.llm.tools import register_bash as core_bash
    if not getattr(request, "run_id", None):
        # Managed commands belong to a core run: their completion wakes its session.
        return
    collected = []
    core_bash(collected, loader=loader, source_run_id=request.run_id, chat_id=request.chat_id,
              sandbox_session_id=request.sandbox_session_id, user_id=uid)
    tools = {tool.name: tool for tool in collected}
    if "Bash" not in tools:
        return
    bash, stdin, listing = tools["Bash"]._func, tools["write_stdin"]._func, tools["list_processes"]._func
    folder = "/workspace/work/" + ident

    async def Bash(command: str, timeout: int | None = None, yield_time_ms: int = YIELD_MS,
                   run_in_background: bool = False) -> ToolChunk:
        state = await run_db(owned_task, ident, uid)
        if waits_only(command):
            running = await run_db(live_commands, request.chat_id)
            if running:
                # Waiting would hold every new instruction until the sleep ends; completion wakes the Worker anyway.
                return response({"error": "Not run: this command only waits, and " + ", ".join(running)
                                 + " is still running.", "next": "End your reply now without a final report. "
                                 "The command's completion wakes you with its result; then continue the task."})
        from personal_agent.computer.sandbox import get_personal_sandbox_provider
        from personal_agent.sessions.session_cli_install import ensure_cli
        from personal_agent.sessions.session_auth import mint, key
        from personal_agent.worker.executor import BACKGROUND_LIMIT
        from core.infra.ephemeral import get_ephemeral_state
        from hugagent_cli.transport import enabled
        provider = get_personal_sandbox_provider()
        await ensure_cli(provider, state["box"], uid)
        # Valid as long as a background command may run; dropped as soon as a command ends in the foreground.
        token = await mint(ident, uid, (timeout or BACKGROUND_LIMIT) + 60)
        server = os.environ.get("PERSONAL_AGENT_CLI_SERVER", "http://backend:3001")
        # Most deployments are private networks without TLS, so plain HTTP is allowed unless disabled.
        allow_http = "1" if enabled(os.environ.get("PERSONAL_AGENT_CLI_ALLOW_HTTP", "1")) else "0"
        # The command text is stored with the command record, so the credential travels in a file the shell deletes.
        secret = await provider.put_secret(state["box"], "export HUGAGENT_SERVER=" + shlex.quote(server)
                                           + " HUGAGENT_API_KEY=" + shlex.quote(token)
                                           + " HUGAGENT_ALLOW_HTTP=" + allow_http + "\n", user_id=uid)
        script = ". " + secret + "; rm -f " + secret + "\nmkdir -p " + folder + " && cd " + folder + "\n" + command
        # The cap is what lets new input reach the Worker within YIELD_MS (§6.3), whatever the model asks for.
        try:
            result = await bash(script, timeout, max(250, min(YIELD_MS, yield_time_ms)), run_in_background)
        except BaseException:
            # The command never started: neither its credential nor the file carrying it may outlive the call.
            with suppress(Exception):
                await get_ephemeral_state().drop(key(token))
            with suppress(Exception):
                await provider.remove_secret(state["box"], secret, user_id=uid)
            raise
        if payload(result).get("status") != "running":
            await get_ephemeral_state().drop(key(token))
        return redact(result)

    polled = set()

    async def write_stdin(session_id: str, chars: str = "", yield_time_ms: int = 0) -> ToolChunk:
        await run_db(owned_task, ident, uid)
        if not chars and session_id in polled and session_id in await run_db(live_commands, request.chat_id):
            # One look at a running command's output is useful; repeating it is polling.
            return response({"error": "Not checked again: " + session_id + " is still running.",
                             "next": "End your reply now without a final report. The command's completion "
                                     "wakes you with its result; then continue the task."})
        if not chars:
            polled.add(session_id)
        return redact(await stdin(session_id, chars, min(YIELD_MS, yield_time_ms)))

    async def list_processes() -> ToolChunk:
        """List this task's commands: running ones first, then the latest finished ones."""
        await run_db(owned_task, ident, uid)
        return redact(await listing())

    Bash.__doc__ = (tools["Bash"]._func.__doc__ or "").replace("默认 60000", "默认 " + str(YIELD_MS)) + (
        "\nRuns on your own cloud computer in the task folder " + folder + "/ (never on the user's device). "
        "HUGAGENT_* variables let the hugagent CLI act for this task. A command still running after "
        "yield_time_ms continues in the background: do independent work or end your reply; its completion "
        "wakes you. Never sleep, poll or wait for it.\n")
    write_stdin.__doc__ = tools["write_stdin"]._func.__doc__
    for fn in (Bash, write_stdin, list_processes):
        toolkit.append(function_tool(fn))
