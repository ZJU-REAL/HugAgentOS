"""What an ended Worker run means for its task and Assignment (SUPERAGENT_DESIGN §6.1, §6.5).

``finished`` is ``on_run_finished``: the Worker has stopped (no live run, no unread input,
no background work it is waiting for) and the task is settled from its last Report.
``failed`` is ``on_run_failed`` after transient failures have used up their retries.
Every function runs with the task row locked and ends the Assignment in the same transaction.
"""
import re
import time

from sqlalchemy import update

from core.db.models import ChatSteerQueueItem
from core.db.models.command import CommandTask
from core.infra.time import utc_now
from personal_agent.computer import computer_wait
from personal_agent.models import PersonalAgent
from personal_agent.tasks.worker import AWAITING_APPROVAL, blocked, interrupt, notify, pause, wait_for_user, waits_only_on_its_question

# 408 / 429 / 5xx and refused, reset or timed-out connections (§6.5).
TRANSIENT = re.compile(r"\b(?:408|429|5\d\d)\b|rate.?limit|overloaded|timed? ?out|timeout|"
                       r"connection (?:refused|reset|aborted|error)|temporarily unavailable|server disconnected", re.I)
RETRY_DELAYS = (15, 30, 60)
# Report statuses that hand the task to the user: the slot is released even while
# processes the Worker left for the user keep running.
HANDOVER = {"ready", "dropped", "blocked", "failed"}
LIVE_COMMANDS = ("starting", "running")
BACKGROUND_STOPPED = "后台命令运行超过 30 分钟，已停止。"


def transient(reason):
    return bool(TRANSIENT.search(reason or ""))


def end(asg, reason):
    asg.status, asg.ended_reason, asg.ended_at, asg.lease_until = "ended", reason, utc_now(), None


def report_of(task, asg):
    """The Worker's latest Report in this Assignment, or {}."""
    report = (task.checkpoint or {}).get("report") or {}
    return report if report.get("assignment") == asg.id else {}


def hands_over(task, asg):
    """True when the task now waits for the user, whatever the Worker still has running."""
    state = task.checkpoint or {}
    return bool(report_of(task, asg).get("status") in HANDOVER or state.get("task_wait_reason")
                or state.get("pending_approval") or task.unconfirmed())


def detach(db, chat_id):
    """Processes left running for the user no longer wake the Worker when they end."""
    db.execute(update(CommandTask).where(CommandTask.chat_id == chat_id, CommandTask.status.in_(LIVE_COMMANDS))
               .values(background=False))


def undelivered_steers(db, run_id):
    """Input steered into a run that ended before taking it: the message ids to deliver again."""
    rows = db.query(ChatSteerQueueItem).filter(ChatSteerQueueItem.target_run_id == run_id,
        ChatSteerQueueItem.status.in_(("accepted", "claimed"))).all()
    return [row.steer_id.split(":", 1)[0] for row in rows]


def schedule_retry(asg):
    """Back off before the next attempt; False once the retries are used up."""
    if asg.retries >= len(RETRY_DELAYS):
        return False
    asg.retry_at = time.time() + RETRY_DELAYS[asg.retries]
    asg.retries += 1
    return True


def finished(db, asg, task, uid, owner, fallback=""):
    """Settle the task from its last Report. ``fallback`` is the cleaned last reply, used without one."""
    from personal_agent.context import AgentContext
    from personal_agent.tasks.runtime import with_exports
    state = task.checkpoint or {}
    report = report_of(task, asg)
    end(asg, "finished")
    detach(db, asg.chat_id)
    if computer_wait.park(task):
        return "parked"
    if state.get("decided_during_run") and not task.unconfirmed() and waits_only_on_its_question(state):
        # The user decided the run's approval while it was ending: that answers the question the run
        # asked alongside it. Run again to act on it; waiting for a reply would void the approval.
        for key in ("task_wait_reason", "pending_question", "decided_during_run"): task.pop_state(key)
        task.requeue()
        return "requeued"
    if task.unconfirmed() or state.get("task_wait_reason"):
        wait_for_user(db, task, blocked(task))
        return "waiting"
    if task.pop_state("decided_during_run"):
        # The user decided an approval while this run was ending. Run again to act on it.
        task.requeue()
        return "requeued"
    if state.get("pending_approval"):
        wait_for_user(db, task, AWAITING_APPROVAL)
        return "waiting"
    status = report.get("status")
    if status == "waiting":
        task.pause((report.get("summary") or "等待中")[:300])
        return "waiting"
    if status == "failed":
        fail(db, task, report.get("summary") or "", reported=True)
        return "failed"
    result = report.get("summary") if status in ("ready", "dropped") else fallback
    if not (result or "").strip():
        fail(db, task, "")
        return "failed"
    result = with_exports(result.strip(), state.get("exports") or [], task.run_id)
    AgentContext(db, uid).tasks.complete(task.id, result, dict(task.checkpoint or {}), owner=owner)
    return "completed"


def fail(db, task, reason, *, unknown=False, reported=False):
    """The run failed for good: the task waits for the user to check it, or fails."""
    if not unknown and not reported and computer_wait.park(task):
        return
    uncertain = unknown or blocked(task) is not None
    task.status = "waiting" if uncertain else "failed"
    if uncertain:
        task.waiting_reason = "操作结果未知，需核对后继续"
    elif reported and reason:
        task.waiting_reason = reason[:300]
    else:
        task.waiting_reason = "任务执行失败。请检查模型、连接器或电脑状态后重试。"
    notify(db, task, task.label + ("需要你核对操作结果。" if uncertain else "执行失败。可以在任务卡片中重试。"))
    if task.status == "failed":
        from personal_agent.workspace.file_memory import record_task
        record_task(db, task, db.get(PersonalAgent, task.agent_id))


def failed(db, asg, task, reason, *, unknown=False):
    end(asg, "failed")
    # Commands it left running no longer wake a Worker for a task that is over.
    detach(db, asg.chat_id)
    fail(db, task, reason, unknown=unknown)


def cancelled(db, asg, task):
    """The run was cancelled from its chat: requeue a safe task, otherwise ask the user to check it."""
    end(asg, "cancelled")
    detach(db, asg.chat_id)
    interrupt(db, task, "执行已中断。请检查操作结果后继续。")


def restarted(db, asg, task):
    """The process that ran the Assignment is gone (§6.5): a new one starts with a recovery note."""
    end(asg, "restarted")
    detach(db, asg.chat_id)
    task.checkpoint = {**(task.checkpoint or {}), "recovery_note": True}
    interrupt(db, task, "执行进程中断。请检查已执行的操作后继续。")


def over_budget(db, asg, task, commands):
    """Background commands outlived BACKGROUND_LIMIT and were stopped; the task waits."""
    end(asg, "background_limit")
    db.execute(update(CommandTask).where(CommandTask.task_id.in_([c["task_id"] for c in commands]),
        CommandTask.notice_status.in_(("pending", "none"))).values(notice_status="consumed"))
    killed = [c["command"][:200] for c in commands]
    task.checkpoint = {**(task.checkpoint or {}), "killed_commands": killed}
    pause(db, task, BACKGROUND_STOPPED)


def stopped(asg):
    """The task left ``running`` (cancelled, paused or parked by someone else)."""
    end(asg, "stopped")
