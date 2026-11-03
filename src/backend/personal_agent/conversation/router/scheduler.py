"""Conversation turns have their own loop. Model latency cannot block task execution.

Turns of different owners run in parallel. One owner has at most one running Turn; its
lease is on the Turn, not on the Triggers it claimed. A Turn takes the oldest pending
Trigger of an owner and the Triggers of the same conversation that may share it (see
kinds.TriggerKind.batchable). A heartbeat check waits behind the user's messages and is
dropped when it waited too long. A Turn whose lease expired is taken over as it is.
"""
import asyncio
import logging
import time
import uuid

from sqlalchemy import or_, select

from core.db.engine import SessionLocal
from core.db.models import ChatMessage
from personal_agent import access
from personal_agent.conversation.router import kinds
from personal_agent.models import PersonalAgent, PersonalFrontTurn, PersonalMessageInbox

log = logging.getLogger(__name__)
PARALLEL_OWNERS = 8
LEASE_SEC = 60
HEARTBEAT_SEC = 15
IDLE_SEC = 0.3
# Triggers one Turn claims at once, and their text. The first Trigger is always taken.
MAX_BATCH = 10
MAX_BATCH_CHARS = 40000


def claim(owner):
    """The ID of a Turn this worker now owns, or None."""
    from personal_agent.scheduling.heartbeat import is_stale, record
    stale = []
    try:
        return _claim(owner, is_stale, stale)
    finally:
        for agent_id in stale: record(agent_id, "stale")


def batch_of(rows, head):
    """head and the pending Triggers after it in its conversation that may share its turn, in order."""
    if not kinds.of(head.inbox.kind).batchable: return [head]
    return [head] + joining(rows[rows.index(head) + 1:], lane(head), 1, len(head.message.content or ""))


def joining(rows, chat_id, count=0, size=0):
    """The pending rows of conversation chat_id that may join a turn already holding count Triggers of size
    characters, in order. A heartbeat check never joins and never holds them back; another Trigger that
    cannot share stops them, and so do the limits. A running turn absorbs by the same rules (see steering.py)."""
    batch = []
    for row in rows:
        if lane(row) != chat_id or row.inbox.kind == "heartbeat": continue
        if not kinds.of(row.inbox.kind).batchable or count + len(batch) >= MAX_BATCH: break
        size += len(row.message.content or "")
        if size > MAX_BATCH_CHARS: break
        batch.append(row)
    return batch


def lane(row):
    return row.inbox.chat_id or row.message.chat_id


def pending_rows(db, agent_id):
    """The owner's pending Triggers with their conversation rows, oldest first, locked."""
    return [Row(inbox, message) for inbox, message in db.execute(select(PersonalMessageInbox, ChatMessage)
        .join(ChatMessage, ChatMessage.message_id == PersonalMessageInbox.message_id)
        .where(PersonalMessageInbox.agent_id == agent_id, PersonalMessageInbox.status == "pending")
        .order_by(PersonalMessageInbox.created_at, PersonalMessageInbox.message_id).with_for_update(of=PersonalMessageInbox)).all()]


def _claim(owner, is_stale, stale):
    with SessionLocal() as db:
        waiting = select(PersonalMessageInbox.agent_id).where(PersonalMessageInbox.status == "pending")
        running = select(PersonalFrontTurn.agent_id).where(PersonalFrontTurn.status == "running")
        for agent_id in db.scalars(select(PersonalAgent.id).where(or_(PersonalAgent.id.in_(waiting), PersonalAgent.id.in_(running)))
                                   .order_by(PersonalAgent.created_at)).all():
            agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update())
            if not access.allowed(db, agent.user_id): continue
            # One turn at a time: a live lease keeps the owner's other Triggers waiting.
            turn = db.scalar(select(PersonalFrontTurn).where(PersonalFrontTurn.agent_id == agent_id,
                PersonalFrontTurn.status == "running").order_by(PersonalFrontTurn.created_at).limit(1).with_for_update())
            if turn is not None:
                if turn.lease_until and turn.lease_until > time.time(): continue
                # Taken over after an interruption: the same Triggers, one more attempt.
                turn.attempts, turn.owner, turn.lease_until = turn.attempts + 1, owner, time.time() + LEASE_SEC
                db.commit()
                return turn.id
            rows = pending_rows(db, agent_id)
            # A Trigger that does not wake is only taken along by a turn of its conversation.
            if not any(kinds.of(row.inbox.kind).wakes for row in rows): continue
            # The user's messages go before a heartbeat check, in their order.
            head = min((row for row in rows if kinds.of(row.inbox.kind).wakes), key=lambda row: row.inbox.kind == "heartbeat")
            if head.inbox.kind == "heartbeat" and is_stale(head.inbox):
                head.inbox.status = "done"
                head.inbox.decision = {**(head.inbox.decision or {}), "quiet": True, "outcome": "stale"}
                db.commit()
                stale.append(agent_id)
                continue
            batch = batch_of(rows, head)
            turn = PersonalFrontTurn(id="pa_turn_" + uuid.uuid4().hex, agent_id=agent_id, chat_id=head.message.chat_id,
                status="running", owner=owner, lease_until=time.time() + LEASE_SEC, attempts=1, reply_ids=[])
            db.add(turn)
            for row in batch:
                row.inbox.status, row.inbox.turn_id = "claimed", turn.id
                row.inbox.chat_id = row.inbox.chat_id or row.message.chat_id
            db.commit()
            return turn.id
    return None


class Row:
    """A pending Trigger with its conversation row, while claiming."""
    __slots__ = ("inbox", "message")

    def __init__(self, inbox, message):
        self.inbox, self.message = inbox, message


def renew(turn_id, owner):
    with SessionLocal() as db:
        row = db.scalar(select(PersonalFrontTurn).where(PersonalFrontTurn.id == turn_id).with_for_update())
        if row is None or row.status != "running" or row.owner != owner: return False
        row.lease_until = time.time() + LEASE_SEC
        db.commit()
        return True


async def run(turn_id, owner, execute=None):
    from personal_agent.conversation.router.turn import run_turn
    task = asyncio.current_task()

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            if not await asyncio.to_thread(renew, turn_id, owner):
                task.cancel()
                return
    beat = asyncio.create_task(heartbeat())
    try:
        await run_turn(turn_id, owner, execute)
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
                    turn_id = await asyncio.to_thread(claim, owner)
                    if turn_id:
                        active.add(asyncio.create_task(run(turn_id, owner, execute)))
                        continue
            except asyncio.CancelledError: raise
            except Exception as exc: log.warning("Personal turn cycle failed (%s)", type(exc).__name__)
            await asyncio.sleep(IDLE_SEC)
    finally:
        for task in active: task.cancel()
