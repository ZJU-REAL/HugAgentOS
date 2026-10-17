"""Dispatch cloud sessions directly, and local sessions through the device queue."""

import time
from fastapi import HTTPException
from sqlalchemy import select
from hugagent_cli.contract import READS, validate
from personal_agent.models import PersonalSessionOperation as Operation
from personal_agent.sessions import session_queue

async def dispatch(db, user, target, operation, args, request_key, grant=None):
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
        digest, result = authorize(task_id, user.user_id, action)
        if result:
            return result
    job = session_queue.submit(
        db, user.user_id, target, operation, args, request_key, task_id, run_id, grant
    )
    if target == "cloud" and job["status"] == "queued":
        row = db.scalar(
            select(Operation)
            .where(Operation.id == job["id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.status == "queued":
            if not session_queue.active_task(db, row):
                raise HTTPException(409, "Task expired")
            row.status = "sent"
            db.commit()
            from personal_agent.sessions.session_cloud import execute

            try:
                result = await execute(db, user, operation, args, origin=task_id)
            except HTTPException as exc:
                db.rollback()
                row = db.get(Operation, job["id"])
                row.status = "completed"
                row.result = {"error": "request_refused", "http_status": exc.status_code}
                db.commit()
            except Exception:
                db.rollback()  # Effect may have happened; never automatically replay.
                raise HTTPException(502, "Operation result unknown; query its receipt") from None
            else:
                row = db.get(Operation, job["id"])
                row.result = result
                row.status = "completed"
                db.commit()
        job = session_queue.view(db, user.user_id, job["id"], task_id)
    if isinstance(job.get("result"), dict) and job["result"].get("outcome_unknown"):
        job = {**job, "outcome_unknown": True}
    if digest:
        from personal_agent.tasks.approvals import receipt

        receipt(task_id, user.user_id, digest, job)
    return job

async def dispatch_approved(task_id, uid, action):
    from core.db.engine import SessionLocal
    from core.db.models import UserShadow
    from core.auth.backend import UserContext
    from personal_agent.tasks.guards import owned_task

    state = owned_task(task_id, uid)
    with SessionLocal() as db:
        from personal_agent.models import PersonalWorkItem

        task = db.get(PersonalWorkItem, task_id)
        user = db.get(UserShadow, uid)
        job = await dispatch(
            db,
            UserContext(
                user_id=uid, user_center_id=user.user_center_id or "", username=user.username
            ),
            action["target"],
            action["operation"],
            action["args"],
            action["request_key"],
            {
                "task": task_id,
                "run": state["run_id"],
                "owner": task.owner,
                "revision": task.revision,
            },
        )

        if job.get("id") and job.get("status") in {"queued", "sent"}:
            import asyncio

            while job["status"] in {"queued", "sent"} and time.time() < job["deadline"]:
                await asyncio.sleep(0.3)
                db.expire_all()
                job = session_queue.view(db, uid, job["id"], task_id)
            from personal_agent.tasks.approvals import receipt, fingerprint

            if job["status"] != "completed" or (
                isinstance(job.get("result"), dict) and job["result"].get("outcome_unknown")
            ):
                job = {**job, "outcome_unknown": True}
            receipt(task_id, uid, fingerprint(action), job)
