"""A running turn takes in what arrives in its conversation meanwhile (ROUTER_DESIGN §4.2).

Before each model call the steering hook asks take() for the conversation's new pending
Triggers, by the scheduler's batching rules; they are injected as one input. Once the
input is in the model's context, absorb() attaches them to the Turn: they are claimed,
the turn's Grants are recomputed and the reply starts a new segment after them (see
reply.py). Until then they stay pending, so a turn that ends or dies before the model saw
them leaves them for the next turn. Issued tool calls are never interrupted.
"""
import asyncio
import json

from sqlalchemy import select

from core.db.engine import SessionLocal
from personal_agent.conversation.router import context, scheduler
from personal_agent.conversation.router.kinds import Grants
from personal_agent.conversation.router.triggers import Trigger
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem

# A turn absorbs at most this often and this many Triggers; the rest wait for the next turn, so a turn ends.
MAX_ABSORBS = 3
MAX_ABSORBED = 20


def render(messages, *, interrupted):
    """The injected text for the inputs one boundary took (see core.chat.steer_hook.render_steer)."""
    return ("[回复前，同一会话又收到了新的输入（按到达顺序编号，编号接着本轮之前的输入）]\n" + "\n\n".join(messages)
        + "\n请把这些输入和本轮之前的输入一起考虑：已经发出的工具调用照常完成；接下来写的内容会作为新的一段回复显示在这些输入之后。")


class FrontSteering:
    """The absorbing side of one running Turn."""

    def __init__(self, turn, reply):
        self.turn, self.reply = turn, reply
        self.absorbs = 0
        self.absorbed = 0

    def hook(self):
        from core.chat.steer_hook import SteeringHook
        return SteeringHook(self.take, self.acknowledge, interrupt_tools=False, take_batch=True, render=render)

    async def take(self, run_id=None):
        return await asyncio.to_thread(self._take)

    async def acknowledge(self, delivery):
        idents = [item["message_id"] for item in (delivery or {}).get("batch") or [delivery or {}] if item.get("message_id")]
        if idents: await asyncio.to_thread(self.absorb, idents)

    def _take(self):
        with SessionLocal() as db: return self.take_in(db)

    def take_in(self, db):
        """The new Triggers this turn may absorb now, rendered: [{"message", "message_id"}]. Nothing is claimed yet."""
        turn = self.turn
        if self.absorbs >= MAX_ABSORBS or self.absorbed >= MAX_ABSORBED: return []
        # A heartbeat check or a hatch takes nothing in, as it shares no batch.
        if not all(trigger.type.batchable for trigger in turn.triggers): return []
        rows = scheduler.joining(scheduler.pending_rows(db, turn.agent_id), turn.chat_id)[:MAX_ABSORBED - self.absorbed]
        if not rows: return []
        from personal_agent.scheduling.schedules import user_zone, zone
        agent = db.get(PersonalAgent, turn.agent_id)
        tz = zone(user_zone(agent.config))
        seen = {block for trigger in turn.triggers if (block := trigger.type.context(db, agent, trigger.bound(db)))}
        items = []
        for n, row in enumerate(rows, len(turn.triggers) + 1):
            trigger = Trigger(row.inbox.message_id, row.inbox.kind, dict(row.inbox.payload or {}), row.message.chat_seq, n, row.message)
            rendered = trigger.type.render(db, row.message, current=True)
            text = context.label(trigger, tz) + " " + (rendered[1] if rendered else row.message.content)
            # Rules a kind adds to the turn context (for example how to report a result), once.
            block = trigger.type.context(db, agent, trigger)
            if block and block not in seen:
                seen.add(block)
                text += "\n\n" + block
            # The task this message selected or quoted: turn_context only covered the turn's first inputs.
            focus = list(dict.fromkeys(trigger.type.focus(db, trigger)))
            if focus:
                selected = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == turn.agent_id,
                    PersonalWorkItem.id.in_(focus))).all()
                text += ("\n\nTasks selected or referenced by this message (platform data; a reference does not by "
                         "itself request a change):\n" + json.dumps([context.task_brief(t) for t in selected], ensure_ascii=False))
            items.append({"message": text, "message_id": trigger.message_id})
        db.rollback()
        return items

    def absorb(self, idents):
        """Attach Triggers the model has now read to the turn. A Trigger no longer pending is skipped."""
        with SessionLocal() as db:
            added, grants = self.claim_in(db, idents)
            db.commit()
        if added: self.attach(added, grants)
        return added

    def claim_in(self, db, idents):
        from core.db.models import ChatMessage
        from personal_agent.conversation.router.turn import owned
        turn = self.turn
        owned(db, turn, lock=True)
        rows = {row.message_id: row for row in db.scalars(select(PersonalMessageInbox).where(
            PersonalMessageInbox.message_id.in_(idents), PersonalMessageInbox.agent_id == turn.agent_id,
            PersonalMessageInbox.status == "pending").with_for_update())}
        added = []
        for ident in idents:
            row = rows.get(ident)
            if row is None: continue
            message = db.get(ChatMessage, ident)
            row.status, row.turn_id, row.chat_id = "claimed", turn.id, row.chat_id or message.chat_id
            added.append(Trigger(ident, row.kind, dict(row.payload or {}), message.chat_seq, len(turn.triggers) + len(added) + 1))
        grants = Grants.combine(trigger.type.grants(db, trigger.bound(db)) for trigger in [*turn.triggers, *added])
        return added, grants

    def attach(self, added, grants):
        turn = self.turn
        turn.triggers.extend(added)
        turn.grants = grants
        # A quiet ending was chosen before the model read this input: it no longer holds.
        turn.quiet = False
        self.absorbs += 1
        self.absorbed += len(added)
        self.reply.boundary(added)
