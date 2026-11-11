"""Conversation turns have their own loop. Model latency cannot block task execution.

Turns of different owners run in parallel. One owner has at most one running Turn; its
lease is on the Turn, not on the Triggers it claimed. A Turn takes the oldest pending
Trigger of an owner and the Triggers of the same conversation that may share it (see
kinds.TriggerKind.batchable). A heartbeat check waits behind the user's messages and is
dropped when it waited too long. A Turn whose lease expired is taken over as it is.

Nobody waits for a proactive check or for the result of its task. So they come after
everything else of their owner, take at most PROACTIVE_OWNERS of a worker's parallel
Turns, do not start while the agent is paused, and a result that waited too long is dropped.
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
# Of those, the most that proactive Turns may hold: the rest stay free for what users send.
PROACTIVE_OWNERS = 4
# The summary of a proactive task that waited this long (a paused agent, an account without
# permission for a while) is no longer sent.
PROACTIVE_RESULT_MAX_SEC = 6 * 3600
LEASE_SEC = 60
HEARTBEAT_SEC = 15
IDLE_SEC = 0.3
# Triggers one Turn claims at once, and their text. The first Trigger is always taken.
MAX_BATCH = 10
MAX_BATCH_CHARS = 40000


def claim(owner):
    """The ID of a Turn this worker now owns, or None."""
    return claim_next(owner)[0]


def claim_next(owner, user_only=False):
    """(the ID of a Turn this worker now owns or None, whether it is a proactive Turn).
    user_only: no proactive Turn starts; the worker has as many of them as it may run."""
    from personal_agent.scheduling.heartbeat import is_stale, record
    ended = []
    try:
        return _claim(owner, is_stale, ended, user_only)
    finally:
        for agent_id, outcome in ended: record(agent_id, outcome)


def expired(inbox, now=None):
    """True for a Trigger that has waited longer than a proactive task's summary stays worth sending."""
    from core.infra.time import utc_now
    created = inbox.created_at
    if created is None: return False
    if created.tzinfo is None:
        from datetime import timezone
        created = created.replace(tzinfo=timezone.utc)
    return ((now or utc_now()) - created).total_seconds() > PROACTIVE_RESULT_MAX_SEC


def drop(inbox, outcome):
    """End a Trigger that nobody waits for without a turn."""
    inbox.status = "done"
    inbox.decision = {**(inbox.decision or {}), "quiet": True, "outcome": outcome}


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


def owner_lock(agent_id):
    """The owner's row, locked so that one worker at a time claims for it. FOR NO KEY UPDATE, not FOR UPDATE:
    a running turn that adds a row referencing the agent (a task, an inbox row) takes a key-share lock on it
    while it holds its Turn row, and must not wait for a claim."""
    return select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update(key_share=True)


def takeover_lock(turn_id):
    """The Turn row to take over, or nothing while its own transaction holds it. A claim holds the owner's
    row here, so it never waits for a Turn row: the turn that holds it may be waiting for the owner's row."""
    return (select(PersonalFrontTurn).where(PersonalFrontTurn.id == turn_id, PersonalFrontTurn.status == "running")
            .with_for_update(skip_locked=True).execution_options(populate_existing=True))


def _claim(owner, is_stale, ended, user_only=False):
    with SessionLocal() as db:
        waiting = select(PersonalMessageInbox.agent_id).where(PersonalMessageInbox.status == "pending")
        running = select(PersonalFrontTurn.agent_id).where(PersonalFrontTurn.status == "running")
        for agent_id in db.scalars(select(PersonalAgent.id).where(or_(PersonalAgent.id.in_(waiting), PersonalAgent.id.in_(running)))
                                   .order_by(PersonalAgent.created_at)).all():
            agent = db.scalar(owner_lock(agent_id))
            if not access.allowed(db, agent.user_id): continue
            # One turn at a time: a live lease keeps the owner's other Triggers waiting. Read without a lock;
            # only a Turn whose lease ran out is locked, and only when nobody else holds it (see takeover_lock).
            turn = db.scalar(select(PersonalFrontTurn).where(PersonalFrontTurn.agent_id == agent_id,
                PersonalFrontTurn.status == "running").order_by(PersonalFrontTurn.created_at).limit(1))
            if turn is not None:
                if turn.lease_until and turn.lease_until > time.time(): continue
                turn = db.scalar(takeover_lock(turn.id))
                # Held by its own settlement, ended meanwhile, or renewed: the next cycle looks again.
                if turn is None or (turn.lease_until and turn.lease_until > time.time()): continue
                # Taken over after an interruption: the same Triggers, one more attempt.
                turn.attempts, turn.owner, turn.lease_until = turn.attempts + 1, owner, time.time() + LEASE_SEC
                proactive = all(kinds.proactive_row(db, row.kind, row.payload) for row in db.scalars(
                    select(PersonalMessageInbox).where(PersonalMessageInbox.turn_id == turn.id)).all())
                db.commit()
                return turn.id, proactive
            rows = pending_rows(db, agent_id)
            unasked = [row for row in rows if kinds.proactive_row(db, row.inbox.kind, row.inbox.payload)]
            paused, dropped = agent.status != "ready", []
            for row in unasked:
                # A check is out of date after minutes, and none is made for a paused agent; its window stays
                # open. The summary of its task is out of date after hours.
                check = row.inbox.kind == "heartbeat"
                if not ((is_stale(row.inbox) or paused) if check else expired(row.inbox)): continue
                drop(row.inbox, "stale")
                ended.append((agent_id, "stale" if check else "task_failed"))
                dropped.append(row)
            # A paused agent still answers what its user sent, but tells nothing on its own; and a worker that
            # already runs its share of proactive Turns leaves the rest for what users send.
            held = unasked if user_only or paused else dropped
            rows = [row for row in rows if row not in held]
            # A Trigger that does not wake is only taken along by a turn of its conversation.
            if not any(kinds.of(row.inbox.kind).wakes for row in rows):
                if dropped: db.commit()
                continue
            # What the user sent or asked for goes first, in its order; then a proactive task's result; then a check.
            head = min((row for row in rows if kinds.of(row.inbox.kind).wakes),
                       key=lambda row: 2 if row.inbox.kind == "heartbeat" else 1 if row in unasked else 0)
            batch = batch_of(rows, head)
            turn = PersonalFrontTurn(id="pa_turn_" + uuid.uuid4().hex, agent_id=agent_id, chat_id=head.message.chat_id,
                status="running", owner=owner, lease_until=time.time() + LEASE_SEC, attempts=1, reply_ids=[])
            db.add(turn)
            for row in batch:
                row.inbox.status, row.inbox.turn_id = "claimed", turn.id
                row.inbox.chat_id = row.inbox.chat_id or row.message.chat_id
            proactive = all(row in unasked for row in batch)
            db.commit()
            return turn.id, proactive
    return None, False


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
    # Running Turns, and whether each is a proactive one.
    active = {}
    try:
        while True:
            active = {task: proactive for task, proactive in active.items() if not task.done()}
            try:
                if len(active) < PARALLEL_OWNERS:
                    turn_id, proactive = await asyncio.to_thread(claim_next, owner, sum(active.values()) >= PROACTIVE_OWNERS)
                    if turn_id:
                        active[asyncio.create_task(run(turn_id, owner, execute))] = proactive
                        continue
            except asyncio.CancelledError: raise
            except Exception as exc: log.warning("Personal turn cycle failed (%s)", type(exc).__name__)
            await asyncio.sleep(IDLE_SEC)
    finally:
        for task in active: task.cancel()
