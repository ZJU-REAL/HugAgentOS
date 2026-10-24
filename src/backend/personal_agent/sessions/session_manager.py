"""Dispatch cloud sessions directly, and local sessions through the device queue."""

import time
from fastapi import HTTPException
from sqlalchemy import select
from hugagent_cli.contract import READS, validate
from personal_agent.models import PersonalSessionOperation as Operation
from personal_agent.sessions import session_queue

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
    row.status = "sent"
    db.commit()
    return True


def _complete(db, job_id, result):
    row = db.get(Operation, job_id)
    row.result = result
    row.status = "completed"
    db.commit()


async def dispatch(user, target, operation, args, request_key, grant=None):
    """Queue a session operation; cloud targets run here, local ones through the device.

    The queue rows are read and written on sessions of their own, so the
    operation itself (which may start a chat run) holds no connection.
    """
    from core.db.threads import run_db, run_in_session

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
            return result
    job = await run_in_session(
        session_queue.submit, user.user_id, target, operation, args, request_key, task_id, run_id, grant
    )
    if target == "cloud" and job["status"] == "queued":
        if await run_in_session(_claim_cloud, job["id"]):
            from personal_agent.sessions.session_cloud import execute

            try:
                result = await execute(user, operation, args, origin=task_id)
            except HTTPException as exc:
                await run_in_session(
                    _complete, job["id"], {"error": "request_refused", "http_status": exc.status_code}
                )
            except Exception:
                # Effect may have happened; never automatically replay.
                raise HTTPException(502, "Operation result unknown; query its receipt") from None
            else:
                await run_in_session(_complete, job["id"], result)
        job = await run_in_session(session_queue.view, user.user_id, job["id"], task_id)
    if isinstance(job.get("result"), dict) and job["result"].get("outcome_unknown"):
        job = {**job, "outcome_unknown": True}
    if digest:
        from personal_agent.tasks.approvals import receipt

        await run_db(receipt, task_id, user.user_id, digest, job)
    return job


def _approved_owner(db, task_id, uid):
    from core.db.models import UserShadow
    from core.auth.backend import UserContext
    from personal_agent.models import PersonalWorkItem

    task = db.get(PersonalWorkItem, task_id)
    user = db.get(UserShadow, uid)
    owner = UserContext(user_id=uid, user_center_id=user.user_center_id or "", username=user.username)
    return owner, task.owner


async def dispatch_approved(task_id, uid, action):
    from core.db.threads import run_db, run_in_session
    from personal_agent.tasks.guards import owned_task

    state = await run_db(owned_task, task_id, uid)
    user, task_owner = await run_in_session(_approved_owner, task_id, uid)
    job = await dispatch(
        user,
        action["target"],
        action["operation"],
        action["args"],
        action["request_key"],
        {
            "task": task_id,
            "run": state["run_id"],
            "owner": task_owner,
        },
    )

    if job.get("id") and job.get("status") in {"queued", "sent"}:
        import asyncio

        # The device answers through its queue; poll it without holding a session.
        while job["status"] in {"queued", "sent"} and time.time() < job["deadline"]:
            await asyncio.sleep(0.3)
            job = await run_in_session(session_queue.view, uid, job["id"], task_id)
        from personal_agent.tasks.approvals import receipt, fingerprint

        if job["status"] != "completed" or (
            isinstance(job.get("result"), dict) and job["result"].get("outcome_unknown")
        ):
            job = {**job, "outcome_unknown": True}
        await run_db(receipt, task_id, uid, fingerprint(action), job)
