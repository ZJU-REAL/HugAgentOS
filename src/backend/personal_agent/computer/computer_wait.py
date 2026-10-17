"""Durable desktop contention waits. No action has been dispatched at this point."""
from sqlalchemy import select, or_
from core.db.engine import SessionLocal
from core.infra.ephemeral import get_ephemeral_state
from personal_agent.models import PersonalWorkItem

BUSY_REASON = "该电脑正在被其他任务或用户使用"
STATUS = "computer_wait"

def mark(ident, uid, lease_key):
    from personal_agent.context import AgentContext
    from personal_agent.tasks.guards import owned_task
    with SessionLocal() as db:
        row = AgentContext(db, uid).tasks.get(ident, lock=True)
        owned_task(ident, uid)
        row.checkpoint = {**(row.checkpoint or {}), "computer_wait": lease_key}
        db.commit()

def clear(ident, uid):
    from personal_agent.context import AgentContext
    from personal_agent.tasks.guards import owned_task
    with SessionLocal() as db:
        row = AgentContext(db, uid).tasks.get(ident, lock=True)
        owned_task(ident, uid)
        row.pop_state("computer_wait")
        db.commit()

def park(row):
    """Called only once the model run has stopped, with its row locked."""
    state = row.checkpoint or {}
    if not state.get("computer_wait") or row.unconfirmed() or state.get("pending_approval") or state.get("task_wait_reason"):
        return False
    row.status, row.owner, row.lease_until, row.waiting_reason = STATUS, None, None, None
    return True

def candidates():
    with SessionLocal() as db:
        rows = db.scalars(select(PersonalWorkItem).where(or_(
            PersonalWorkItem.status == STATUS,
            (PersonalWorkItem.status == "waiting") & (PersonalWorkItem.waiting_reason == BUSY_REASON),
        ))).all()
        # Old waits lack the target. Re-admit once using the agent desktop;
        # the actual tool still acquires its exact target before dispatch.
        return [(row.id, (row.checkpoint or {}).get("computer_wait") or "pa:desktop:" + row.agent_id)
                for row in rows]

def resume(ident, lease_key):
    with SessionLocal() as db:
        row = db.scalar(select(PersonalWorkItem).where(PersonalWorkItem.id == ident).with_for_update())
        if row is None: return
        state = dict(row.checkpoint or {})
        legacy = row.status == "waiting" and row.waiting_reason == BUSY_REASON and state.get("task_wait_reason") == BUSY_REASON
        if row.status != STATUS and not legacy: return
        if row.unconfirmed() or state.get("pending_approval"):
            if row.status == STATUS: row.pause("操作结果或批准需要处理")
            db.commit()
            return
        if state.get("task_wait_reason") and not legacy: return
        if state.get("computer_wait") and state["computer_wait"] != lease_key: return
        state.pop("computer_wait", None)
        if legacy: state.pop("task_wait_reason", None)
        row.checkpoint = state
        row.requeue()
        db.commit()

async def resume_available():
    import asyncio
    state = get_ephemeral_state()
    for ident, key in await asyncio.to_thread(candidates):
        if await state.get(key) is None:
            # The tool acquires the lease again before execution; a race here
            # can only defer the task again, never bypass mutual exclusion.
            await asyncio.to_thread(resume, ident, key)
