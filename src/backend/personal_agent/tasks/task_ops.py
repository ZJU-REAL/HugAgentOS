"""Task effects shared by the agent's task tools and the explicit task controls.

Every effect is idempotent per message: a turn that is retried after a crash
creates, links and changes the same tasks only once.
"""
import uuid
from sqlalchemy import select, update
from personal_agent.models import PersonalWorkItem, PersonalMessageTask, PersonalDeviceCommand, TITLE_LENGTH

STATUS_NAMES = {"queued": "排队中", "computer_wait": "排队中", "running": "执行中", "waiting": "等待处理", "completed": "已完成", "failed": "失败", "cancelled": "已停止"}
CONTROLS = ("pause", "resume", "cancel")
MAX_GOAL = 20000
MAX_UPDATE = 4000
MAX_INPUTS = 10

def link(db, message_id, task_id, relation, instruction=""):
    if db.get(PersonalMessageTask, (message_id, task_id, relation)) is None:
        db.add(PersonalMessageTask(message_id=message_id, work_item_id=task_id, relation=relation, instruction=instruction))
        db.flush()

def _sync(svc, ids):
    """Bring the tasks' private conversations up to date with the new links."""
    from personal_agent.tasks.task_threads import sync_thread
    svc.db.flush()
    for task_id in dict.fromkeys(ids): sync_thread(svc.db, svc.tasks.get(task_id), svc.user_id)

def create(svc, message, goal, key, *, attachments=(), related=(), title=""):
    goal, title = (goal or "").strip(), (title or "").strip()[:TITLE_LENGTH]
    if not goal or len(goal) > MAX_GOAL: raise ValueError("Task goal must contain 1 to 20000 characters")
    agent = svc.identity.agent()
    if agent.status != "ready": raise ValueError("Personal Agent is paused")
    related = list(dict.fromkeys(related or []))
    if len(related) > 8: raise ValueError("At most eight related tasks")
    for ident in related: svc.tasks.get(ident)
    old = svc.db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id, PersonalWorkItem.request_key == key))
    if old: return old
    attachments = list(attachments or [])
    state = {"source_chat_id": message.chat_id, "initial_artifact_ids": attachments, "input_artifact_ids": attachments, **({"title": title} if title else {})}
    task = PersonalWorkItem(id="pa_task_" + uuid.uuid4().hex, agent_id=agent.id, request_key=key, goal=goal,
        context_seq=message.chat_seq, checkpoint=state)
    svc.db.add(task); svc.db.flush()
    link(svc.db, message.message_id, task.id, "goal", goal)
    for ident in related: link(svc.db, message.message_id, ident, "related")
    _sync(svc, [task.id, *related])
    return task

def update_task(svc, message, task_id, instruction, *, attachments=()):
    from personal_agent.identity.config import TERMINAL
    instruction = (instruction or "").strip()
    if not instruction or len(instruction) > MAX_UPDATE: raise ValueError("Task update must contain 1 to 4000 characters")
    task = svc.tasks.get(task_id, lock=True)
    if svc.db.get(PersonalMessageTask, (message.message_id, task.id, "update")): return task
    reopening = task.status in TERMINAL
    state = dict(task.checkpoint or {})
    inputs = list(dict.fromkeys([*state.get("input_artifact_ids", []), *(attachments or [])]))
    if len(inputs) > MAX_INPUTS: raise ValueError("Task has more than ten input attachments")
    state["input_artifact_ids"] = inputs
    state["continuation_seq"] = message.chat_seq
    # New instructions void approvals that were given for the old ones.
    state["approved_action_payloads"], state["approved_actions"] = {}, []
    state.pop("pending_approval", None)
    if reopening:
        # Keep the transcript, files and receipts; a new turn must not reuse old approvals.
        _sync(svc, [task.id])
        for key in ("final_answer", "progress", "task_wait_reason", "computer_wait", "approved_scopes", "decided_during_run", "auto_resumes"):
            state.pop(key, None)
        task.requeue()
        if task.unconfirmed():
            task.pause("请先核对未知的操作结果，再继续此任务。")
    if task.status == "waiting" and not task.unconfirmed():
        state.pop("task_wait_reason", None)
        task.checkpoint = state
        task.requeue()
    task.checkpoint = state
    link(svc.db, message.message_id, task.id, "update", instruction)
    _sync(svc, [task.id])
    return task

def control(svc, message, task_id, kind):
    from personal_agent.identity.config import TERMINAL
    if kind not in CONTROLS: raise ValueError("Unknown task action")
    task = svc.tasks.get(task_id, lock=True)
    if svc.db.get(PersonalMessageTask, (message.message_id, task.id, kind)): return task
    if kind in ("pause", "resume") and task.status in TERMINAL: raise ValueError("任务已经结束。")
    if kind == "resume":
        # "Continue" is never an approval: a pending action is approved only in the approval card.
        if (task.checkpoint or {}).get("pending_approval") or task.unconfirmed():
            raise ValueError("请先在卡片中批准待处理操作，或核对未知的操作结果。")
        if task.status == "waiting":
            state = dict(task.checkpoint or {}); state.pop("task_wait_reason", None); task.checkpoint = state
            task.requeue()
    elif task.status not in TERMINAL:
        task.status = "waiting" if kind == "pause" else "cancelled"
        task.waiting_reason = "用户暂停了任务" if kind == "pause" else "用户停止了任务"
        if task.status == "cancelled":
            from personal_agent.workspace.file_memory import record_task
            record_task(svc.db, task, svc.identity.agent())
        svc.db.execute(update(PersonalDeviceCommand).where(PersonalDeviceCommand.work_item_id == task.id,
            PersonalDeviceCommand.status == "queued").values(status="revoked"))
    link(svc.db, message.message_id, task.id, kind)
    _sync(svc, [task.id])
    return task

def status_line(task):
    return task.label_text + "：" + STATUS_NAMES.get(task.status, task.status) + ("。" + task.waiting_reason if task.waiting_reason else "")
