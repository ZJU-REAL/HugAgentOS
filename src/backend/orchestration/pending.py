"""Dispatch only never-started Plan/Loop runs, using their accepted arguments."""

import asyncio
from core.db import engine
from core.db.models import ChatRun
from core.runs import runtime


def _arguments(run_id):
    with engine.SessionLocal() as db:
        row = db.query(ChatRun.recovery_snapshot).filter(
            ChatRun.run_id == run_id, ChatRun.status == "pending",
        ).first()
        return (row[0] or {}).get("initial_args") if row else None


async def dispatch_pending(run):
    if run.status != "pending":
        return False
    kind = (run.request_payload or {}).get("kind")
    if kind == "plan_execute":
        from orchestration.plans.execute_run import _run_plan_execute_workflow as worker
    elif kind == "autonomous_loop":
        from orchestration.loops.run import _run_autonomous_loop_workflow as worker
    else:
        return False
    args = await asyncio.to_thread(_arguments, run.run_id)
    if args is None:
        return False
    runtime.register_run_task(
        run.run_id,
        worker(run_id=run.run_id, chat_id=run.chat_id, user_id=run.user_id,
               message_id=run.message_id, **args),
        name=f"{kind}:{run.run_id}",
    )
    return True
