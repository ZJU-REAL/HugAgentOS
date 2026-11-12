"""Dispatch cloud sessions directly, and local sessions through the device queue."""

import asyncio
import logging
import time
from fastapi import HTTPException
from sqlalchemy import select
from hugagent_cli.contract import READS, STATUS_OUTCOME_UNKNOWN, failure, validate
from personal_agent.models import PersonalSessionOperation as Operation
from personal_agent.sessions import session_queue

logger = logging.getLogger(__name__)
POLL_SEC = 0.3

def _claim_cloud(db, job_id):
    """Mark a queued cloud operation sent; False when another dispatcher took it."""
    row = db.scalar(
        select(Operation)
        .where(Operation.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.status != "queued":
        return False
    if not session_queue.active_task(db, row):
        raise HTTPException(409, "Task expired")
    row.status, row.claimed_at = "sent", time.time()
    db.commit()
    return True


def _complete(db, job_id, result):
    row = db.get(Operation, job_id)
    row.result = result
    row.status = "completed"
    db.commit()


def normalize_target(target):
    """The user's web HugAgentOS is stored as "cloud"; the model calls it "web", so it is not taken
    for the agent's own cloud computer. Both names mean the same place."""
    return "cloud" if target in (None, "", "web", "cloud") else target


def _current(db, uid, receipt, task_id):
    """An earlier receipt that names an operation, as that operation stands now; None when it names none."""
    if not (isinstance(receipt, dict) and receipt.get("id")):
        return None
    try:
        return session_queue.view(db, uid, receipt["id"], task_id)
    except LookupError:
        return None


def settled(job):
    """What the approved action's receipt keeps: only a write that may have taken effect is flagged
    for the user to check. Expired, withdrawn, refused and failed operations certainly did not run."""
    if session_queue.outcome(job) == STATUS_OUTCOME_UNKNOWN:
        return {**job, "outcome_unknown": True}
    return {key: value for key, value in job.items() if key != "outcome_unknown"}


async def _wait(uid, job, task_id):
    """Until the device answers or the row's own deadline (plus the receipt grace) passes, when
    public() reports it expired or outcome_unknown. Polled without holding a session."""
    from core.db.threads import run_in_session

    end = job["deadline"] + session_queue.RECEIPT_GRACE + 2
    while job.get("status") in {"queued", "sent"} and time.time() < end:
        await asyncio.sleep(POLL_SEC)
        job = await run_in_session(session_queue.view, uid, job["id"], task_id)
    return job


async def _finish(uid, job, task_id, digest, wait):
    from core.db.threads import run_db

    if wait and job.get("id"):
        job = await _wait(uid, job, task_id)
    job = settled(job)
    if digest:
        from personal_agent.tasks.approvals import receipt

        await run_db(receipt, task_id, uid, digest, job)
    return job


async def _run_cloud(user, job, operation, args, task_id):
    """Run a queued web operation here and write its receipt. Every failure becomes a receipt that says
    what it was (contract.failure): nothing escapes as an unexplained error."""
    from core.db.threads import run_in_session
    from personal_agent.sessions.session_cloud import execute

    if not await run_in_session(_claim_cloud, job["id"]):
        return
    try:
        result = await execute(user, operation, args, origin=task_id)
    except HTTPException as exc:
        result = failure(operation, "http_" + str(exc.status_code), sent=True,
                         http_status=exc.status_code, detail=exc.detail)
    except Exception as exc:
        # A write may have taken effect; failure() marks it uncertain, so it is never replayed blindly.
        logger.exception("Session operation %s failed on the web", operation)
        result = failure(operation, "local_failure", sent=True, detail=str(exc), exception=type(exc).__name__)
    await run_in_session(_complete, job["id"], result)


async def dispatch(user, target, operation, args, request_key, grant=None, wait=False):
    """Queue a session operation; web (cloud) targets run here, local ones through the device.

    wait: also wait for the device's answer, or for the deadline that ends it. The queue rows are
    read and written on sessions of their own, so the operation itself (which may start a chat run)
    holds no connection. With a task grant, the approved action's receipt follows the row.
    """
    from core.db.threads import run_db, run_in_session

    target = normalize_target(target)
    validate(operation, args)
    task_id = grant["task"] if grant else None
    run_id = grant["run"] if grant else None
    digest = None
    if grant and operation not in READS:
        from personal_agent.tasks.approvals import authorize

        action = {
            "kind": "hugagent.session",
            "target": target,
            "operation": operation,
            "args": args,
            "request_key": request_key,
        }
        digest, result = await run_db(authorize, task_id, user.user_id, action)
        if result:
            current = await run_in_session(_current, user.user_id, result, task_id)
            if current is None:
                return result  # waiting for approval, rejected, or checked by the user
            if not session_queue.may_retry(current):
                return await _finish(user.user_id, current, task_id, digest, wait)
            # It certainly did not take effect: the user's approval still covers running it once.
    job = await run_in_session(
        session_queue.submit, user.user_id, target, operation, args, request_key, task_id, run_id, grant
    )
    if digest:
        from personal_agent.tasks.approvals import receipt

        # Before anything runs, the receipt names the row, so a crash leaves something to look up.
        await run_db(receipt, task_id, user.user_id, digest, settled(job))
    if target == "cloud" and job["status"] == "queued":
        await _run_cloud(user, job, operation, args, task_id)
        job = await run_in_session(session_queue.view, user.user_id, job["id"], task_id)
    return await _finish(user.user_id, job, task_id, digest, wait)


def _approved_owner(db, task_id, uid):
    from core.db.models import UserShadow
    from core.auth.backend import UserContext
    from personal_agent.models import PersonalWorkItem

    task = db.get(PersonalWorkItem, task_id)
    user = db.get(UserShadow, uid)
    owner = UserContext(user_id=uid, user_center_id=user.user_center_id or "", username=user.username)
    return owner, task.owner


async def dispatch_approved(task_id, uid, action):
    """Run an action the user approved, before the model runs again, and wait for its answer:
    the receipt the next run reads says what really happened."""
    from core.db.threads import run_db, run_in_session
    from personal_agent.tasks.guards import owned_task

    state = await run_db(owned_task, task_id, uid)
    user, task_owner = await run_in_session(_approved_owner, task_id, uid)
    return await dispatch(
        user,
        action["target"],
        action["operation"],
        action["args"],
        action["request_key"],
        {"task": task_id, "run": state["run_id"], "owner": task_owner},
        wait=True,
    )
