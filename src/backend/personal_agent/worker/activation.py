"""One activation of a Worker: a core ChatRun on the task's own ChatSession (SUPERAGENT_DESIGN §6.1, §8.1).

It is admitted the way ``core.sandbox.commands.delivery.prepare`` admits a command
wake: ``ChatSequencer.accept_main_run`` commits the input and the pending run, then
``admission.start_run`` launches it with the Worker profile in its context. The
context travels in the run's recovery snapshot, so core's steer handoffs and command
completion wakes on this session run the Worker profile too.

The first activation writes a hidden brief (clock, task folder, recent conversation,
attachments) and the goal; later ones write only the new updates, or a hidden note.
"""
import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select

from core.db.models import ChatMessage, ChatRun, ChatSession
from personal_agent.models import PersonalAgent, PersonalAssignment, PersonalWorkItem
from personal_agent.worker import inputs

PROFILE = "personal_agent"
CONTINUE = "继续完成这个任务。不要重复已经完成的外部操作。"
RECOVERY = ("上一次执行被中断（服务重启）。先核对任务文件夹、已发送的邮件和已执行的电脑操作，"
            "再决定是否重复外部操作。")
RETRY = "上一次执行因临时故障中断（{reason}）。继续完成任务，不要重复已经完成的外部操作。"
KILLED = "这些后台命令超过 30 分钟，已被停止：\n{commands}"
# Task state the Worker reads again when it resumes: approval receipts and rejections.
STATE_KEYS = ("action_receipts", "rejected_actions", "approved_actions")


@dataclass
class Activation:
    assignment_id: str
    task_id: str
    user_id: str
    chat_id: str
    box: str
    first: bool
    content: str
    hidden: bool
    notes: list = field(default_factory=list)
    message_ids: list = field(default_factory=list)
    artifact_ids: list = field(default_factory=list)
    attachments: str = ""


def started(db, chat_id):
    """True once the Worker session has had a run: its history then carries the brief and goal."""
    return db.scalar(select(ChatRun.run_id).where(ChatRun.chat_id == chat_id).limit(1)) is not None


def recent_conversation(db, agent, task):
    from personal_agent.conversation.conversations import task_chat
    recent = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == task_chat(db, agent, task),
        ChatMessage.chat_seq < task.context_seq, ChatMessage.role.in_(["user", "assistant"]))
        .order_by(ChatMessage.chat_seq.desc()).limit(12)).all()
    from personal_agent.conversation.router.kinds import CONTEXT_ONLY, quoted

    def line(m):
        text = re.sub(r'/((?:api/)?files)/[A-Za-z0-9_-]+', '[previous attachment; export again for this task]', m.content[:1500])
        if (m.extra_data or {}).get(CONTEXT_ONLY):
            # Someone else in a group: quoted line by line, so it cannot pass for the owner.
            return "group context (not the owner):\n" + quoted(text)
        return f"{m.role}: {text}"
    return "\n".join(line(m) for m in reversed(recent))


def brief(db, agent, task, config):
    from personal_agent.tasks.runtime import clock
    from personal_agent.memory.sources import for_task
    return (clock(config) + "Task ID (<task_id> in the rules): " + task.id + "; task folder: /workspace/work/" + task.id + "/\n"
            + "本轮来源 id：" + for_task(task) + "\n"
            + "Recent conversation (reference data):\n" + recent_conversation(db, agent, task))


def plan(db, asg_id, owner, note=None):
    """What the next activation says, or None when the Assignment is no longer this owner's."""
    from personal_agent.identity.capabilities import runtime_config
    from personal_agent.tasks.task_threads import sync_thread
    asg = db.get(PersonalAssignment, asg_id)
    if asg is None or asg.status != "running" or asg.owner != owner:
        return None
    task = db.get(PersonalWorkItem, asg.work_item_id)
    if task.status != "running" or task.run_id != asg.id:
        return None
    agent = db.get(PersonalAgent, task.agent_id)
    session = sync_thread(db, task, agent.user_id)
    state = task.checkpoint or {}
    updates = inputs.unread(db, task)[:inputs.BATCH]
    first = not started(db, session.chat_id)
    notes = []
    if first:
        notes.append(brief(db, agent, task, runtime_config(db, agent.user_id, dict(agent.config or {}))))
        content = task.goal + ("\n\nTask updates:\n" + inputs.text(updates) if updates else "")
    else:
        if state.get("recovery_note"):
            notes.append(RECOVERY)
        if state.get("killed_commands"):
            notes.append(KILLED.format(commands="\n".join(state["killed_commands"])))
        if note:
            notes.append(note)
        kept = {key: state[key] for key in STATE_KEYS if state.get(key)}
        if kept:
            notes.append("Task state (reference data):\n" + json.dumps(kept, ensure_ascii=False)[:12000])
        content = inputs.text(updates)
    return Activation(assignment_id=asg.id, task_id=task.id, user_id=agent.user_id, chat_id=session.chat_id,
        box=agent.sandbox_session_id, first=first, content=content or CONTINUE, hidden=not content,
        notes=notes, message_ids=[ident for ident, _ in updates], artifact_ids=inputs.new_attachments(task))


def run_context(db, task, agent, chat_id, asg_id):
    """The run context: the Worker profile, the owner's capabilities and the agent's own computer."""
    from core.chat.context import build_runtime_context
    from personal_agent.identity.capabilities import runtime_config, task_mcp_ids
    from personal_agent.tools.plugins import loaded_plugins
    config = runtime_config(db, agent.user_id, dict(agent.config or {}))
    effort = config.get("thinking_effort", "fast")
    # Skills are workspace files the task reads itself; plugins it loaded in an earlier run stay connected.
    context = build_runtime_context(model_name=None, user_id=agent.user_id, chat_id=chat_id,
        enable_thinking=effort != "fast", enabled_skills=[],
        enabled_mcps=task_mcp_ids(config, loaded_plugins(agent.id, config, chat_id)),
        enabled_kbs=[], enabled_agents=[])
    context.update({
        "profile": PROFILE, "profile_ref": task.id, "personal_assignment_id": asg_id,
        "chat_mode": effort, "model_provider_id": config.get("model_provider_id"),
        "sandbox_session_id": agent.sandbox_session_id,
        # Unattended: nobody is in the task's chat to answer a core confirmation.
        # Personal approvals are the task's own (tasks/approvals.py).
        "automation_run": True,
        # The Worker hook takes every waiting update at once; at run end core hands them all on together.
        "steer_batch_handoff": True,
    })
    return context


@dataclass
class Accepted:
    run: object
    chat_id: str
    user_id: str
    content: str
    history: list
    context: dict
    payload: dict


def accept(db, act, owner):
    """Commit the activation's rows and its pending ChatRun; None when the Assignment moved on.

    Raises ``ChatBusyError`` when a run is already live on the session."""
    from core.chat.operations.session_context import load_session_messages
    from core.chat.sequencer import ChatSequencer
    from core.chat.service import ChatService
    from personal_agent.tasks.task_threads import append
    asg = db.get(PersonalAssignment, act.assignment_id)
    task = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == act.task_id).with_for_update())
    if asg is None or asg.status != "running" or asg.owner != owner or task.status != "running" or task.run_id != asg.id:
        db.rollback()
        return None
    agent = db.get(PersonalAgent, task.agent_id)
    context = run_context(db, task, agent, act.chat_id, asg.id)
    session = db.get(ChatSession, act.chat_id)
    notes = [*act.notes, *([act.attachments.strip()] if act.attachments.strip() else [])]
    if notes:
        text = "\n\n".join(notes)
        append(db, session, "user", text, {"hidden_in_chat": True, "context_only": True,
            "personal_input": "brief" if act.first else "note", "work_item_id": task.id,
            **inputs.context_metadata(text, "personal_agent:" + ("brief" if act.first else "note"))})
    payload = {"kind": "chat", "wake_reason": "personal_task", "chat_id": act.chat_id,
               "chat_mode": context["chat_mode"], "enable_thinking": context["enable_thinking"]}

    def admitted(db, run):
        inputs.consume(task, act.message_ids)
        inputs.staged(task, act.artifact_ids)
        for key in ("recovery_note", "killed_commands"):
            task.pop_state(key)
        asg.run_id, asg.idle_since, asg.retry_at = run.run_id, None, None

    extra = {"personal_input": "activation", "work_item_id": task.id}
    if act.hidden:
        extra["hidden_in_chat"] = True
    accepted = ChatSequencer(db).accept_main_run(chat_id=act.chat_id, user_id=act.user_id,
        user_content=act.content, user_extra_data=extra, request_payload=payload, on_accept=admitted)
    history = load_session_messages(ChatService(db), act.chat_id, act.user_id)
    db.refresh(accepted.run)
    db.expunge(accepted.run)
    return Accepted(accepted.run, act.chat_id, act.user_id, act.content, history, context, payload)


async def start(accepted):
    """Launch an accepted Worker run on the core chat runtime."""
    import core.chat.runtime.admission as admission
    from core.chat.sequencer import ChatSequencer
    from core.db.threads import run_in_session
    try:
        await admission.start_run(accepted_run=accepted.run, chat_id=accepted.chat_id, user_id=accepted.user_id,
            session_messages=accepted.history, effective_user_message=accepted.content,
            raw_user_message=accepted.content, context=accepted.context, request_payload=accepted.payload)
    except BaseException as exc:
        await run_in_session(lambda db: ChatSequencer(db).abandon_pending_run(
            accepted.run.run_id, reason=str(exc) or type(exc).__name__))
        raise
