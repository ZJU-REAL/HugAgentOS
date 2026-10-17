"""Conversation turns have their own loop. Model latency cannot block task execution.

Turns of different owners run in parallel. One owner's turns run one at a time in the
order of their messages: a lease on the oldest open row keeps the next one waiting.
A heartbeat check waits behind the user's messages and is dropped when it waited too long.
"""
import asyncio
import logging
import time
from sqlalchemy import select
from core.db.engine import SessionLocal
from personal_agent import access
from personal_agent.models import PersonalAgent, PersonalMessageInbox
from personal_agent.conversation.message_routing import OPEN

log = logging.getLogger(__name__)
PARALLEL_OWNERS = 8
LEASE_SEC = 60
HEARTBEAT_SEC = 15
IDLE_SEC = 0.3

def claim(owner):
    from personal_agent.scheduling.heartbeat import is_beat_key, is_stale, record
    stale = []
    try:
        return _claim(owner, is_beat_key, is_stale, stale)
    finally:
        for agent_id in stale: record(agent_id, "stale")

def _claim(owner, is_beat_key, is_stale, stale):
    with SessionLocal() as db:
        waiting = select(PersonalMessageInbox.agent_id).where(PersonalMessageInbox.status.in_(OPEN)).distinct()
        for agent_id in db.scalars(select(PersonalAgent.id).where(PersonalAgent.id.in_(waiting)).order_by(PersonalAgent.created_at)).all():
            agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update())
            if not access.allowed(db, agent.user_id): continue
            rows = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.agent_id == agent_id,
                PersonalMessageInbox.status.in_(OPEN))
                .order_by(PersonalMessageInbox.created_at, PersonalMessageInbox.message_id).with_for_update()).all()
            # One turn at a time: a live lease on any open row keeps the others waiting.
            if not rows or any(r.status != "pending" and r.lease_until and r.lease_until > time.time() for r in rows): continue
            # The user's messages go before a heartbeat check, in their order.
            row = min(rows, key=lambda r: is_beat_key(r.request_key))
            if row.status == "pending" and is_beat_key(row.request_key) and is_stale(row):
                row.status, row.decision = "done", {**(row.decision or {}), "quiet": True, "outcome": "stale"}
                db.commit()
                stale.append(agent_id)
                continue
            # A row claimed before counts as an interrupted attempt.
            attempts = (row.decision or {}).get("attempts", 0) + 1
            row.status, row.owner, row.lease_until = "responding", owner, time.time() + LEASE_SEC
            row.decision = {**(row.decision or {}), "attempts": attempts}
            db.commit()
            return row.message_id
    return None

def renew(ident, owner):
    with SessionLocal() as db:
        row = db.scalar(select(PersonalMessageInbox).where(PersonalMessageInbox.message_id == ident).with_for_update())
        if row is None or row.status != "responding" or row.owner != owner: return False
        row.lease_until = time.time() + LEASE_SEC
        db.commit()
        return True

async def run(ident, owner, execute=None):
    from personal_agent.conversation.front import run_turn
    turn = asyncio.current_task()

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            if not await asyncio.to_thread(renew, ident, owner):
                turn.cancel()
                return
    beat = asyncio.create_task(heartbeat())
    try:
        await run_turn(ident, owner, execute)
    except asyncio.CancelledError:
        pass
    finally:
        beat.cancel()

async def loop(owner, execute=None):
    active = set()
    try:
        while True:
            active = {task for task in active if not task.done()}
            try:
                if len(active) < PARALLEL_OWNERS:
                    ident = await asyncio.to_thread(claim, owner)
                    if ident:
                        active.add(asyncio.create_task(run(ident, owner, execute)))
                        continue
            except asyncio.CancelledError: raise
            except Exception as exc: log.warning("Personal turn cycle failed (%s)", type(exc).__name__)
            await asyncio.sleep(IDLE_SEC)
    finally:
        for task in active: task.cancel()
