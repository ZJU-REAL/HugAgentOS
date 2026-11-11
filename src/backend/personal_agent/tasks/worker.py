"""Task states shared by the executor, approvals and task controls.

The execution itself is ``personal_agent.worker``: each activation is a core ChatRun.
"""
import logging
from personal_agent.computer import computer_wait
from personal_agent.models import PersonalAgent
from personal_agent.context import AgentContext

log = logging.getLogger(__name__)
MAX_AUTO_RESUMES = 2
UNCONFIRMED = "操作结果尚未确认，请核对后继续。"
AWAITING_APPROVAL = "等待你批准操作"

def resumable(row):
    """An interrupted run can restart only when no external effect is unconfirmed."""
    state = row.checkpoint or {}
    return not (row.unconfirmed() or state.get("pending_approval") or state.get("approved_action_payloads")
        or state.get("task_wait_reason")) and state.get("auto_resumes", 0) < MAX_AUTO_RESUMES

def waits_only_on_its_question(state):
    """True when the task waits on the question its run asked and no approval is left undecided.

    A decided approval answers such a question: the run asked while its action waited for the user
    (often "please approve"), so once the action is decided the task runs again and says what
    happened, asking again only if the question still stands.
    """
    question = state.get("pending_question") or {}
    return bool(question) and not state.get("pending_approval") and \
        state.get("task_wait_reason") == (question.get("text") or "")[:300]

def blocked(row):
    """Why the task must wait for the user before (or instead of) finishing, or None."""
    state = row.checkpoint or {}
    if row.unconfirmed(): return UNCONFIRMED
    if state.get("task_wait_reason"): return state["task_wait_reason"]
    if state.get("pending_approval"): return AWAITING_APPROVAL
    return None

def pause(db, row, reason):
    row.pause(reason)
    notify(db, row, row.label + "已暂停：" + reason)

def wait_for_user(db, row, reason):
    from personal_agent.channels.im_actions import approval_notice, question_notice
    row.pause(reason)
    # Both notices are posted once per approval or question, however often the task waits.
    if reason == AWAITING_APPROVAL: approval_notice(db, row)
    elif (row.checkpoint or {}).get("pending_question"): question_notice(db, row)

def interrupt(db, row, reason):
    """Requeue a safe interrupted task. Otherwise ask the user to check it."""
    if computer_wait.park(row): return
    if resumable(row):
        row.checkpoint = {**(row.checkpoint or {}), "auto_resumes": (row.checkpoint or {}).get("auto_resumes", 0) + 1}
        row.requeue()
        return
    pause(db, row, reason)

def notify(db, row, text):
    """Post a short status notice in the main conversation. It is not a task result."""
    agent = db.get(PersonalAgent, row.agent_id)
    try:
        AgentContext(db, agent.user_id).conversation.append(agent, "assistant", text, row.id, relation="status")
    except Exception as exc:
        log.warning("Personal notice failed (%s)", type(exc).__name__)
