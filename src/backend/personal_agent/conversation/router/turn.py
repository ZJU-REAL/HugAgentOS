"""The agent's own turn in a conversation.

Every message the user sends without an explicit task control, every finished background
task, every heartbeat check and a new agent's hatch is a Trigger. A Turn takes the pending
Triggers of one conversation (see scheduler.py), and the agent sees the conversation, its
persona and the task list, and either answers directly (streamed into the conversation) or
uses its task tools to delegate. There is no separate classifier.
"""
import asyncio
import logging
import re
import time
from contextvars import ContextVar

from sqlalchemy import select

from core.db.engine import SessionLocal
from core.db.models import ChatMessage, ChatSession
from core.db.threads import run_in_session
from personal_agent.conversation.router import context, kinds, triggers
from personal_agent.conversation.router.kinds import FAILED, Grants
from personal_agent.conversation.router.reply import ReplyStream
from personal_agent.conversation.router.steering import FrontSteering
from personal_agent.models import PersonalAgent, PersonalFrontTurn, PersonalMessageInbox, PersonalMessageTask, PersonalWorkItem
from personal_agent.tasks import task_ops

log = logging.getLogger(__name__)
PROFILE = "personal_agent_front"
MAX_ATTEMPTS = 2
# The Turn that runs in this asyncio task (see Turn).
current_turn = ContextVar("personal_turn", default=None)


class TurnLost(RuntimeError):
    """Another worker owns this turn now."""


def reply_id(ident):
    """The reply row of a turn whose first Trigger is ident. A retried or reprocessed batch writes it again."""
    return "pa_reply_" + ident.rsplit("_", 1)[-1][-32:]


class Turn:
    """One running turn: its batch of Triggers, what the batch may do, and the tools' state."""

    def __init__(self, row, batch, grants):
        self.id, self.owner, self.agent_id, self.chat_id = row.id, row.owner, row.agent_id, row.chat_id
        self.attempts, self.triggers, self.grants = row.attempts, batch, grants
        # Tasks created so far; numbers the tools' idempotency keys.
        self.count = 0
        self.quiet = False
        self.inspected_tasks = set()
        # Takes in what arrives in the conversation while the turn runs (see steering.py).
        self.steering = None
        # What memory written in this turn cites: the first user message, else the first Trigger (see source_of).
        self.source_id = None

    @property
    def head(self):
        """The first Trigger. Its message ID keys the reply and the tools' effects, so a retry repeats them."""
        return self.triggers[0].message_id

    @property
    def message_ids(self):
        return [trigger.message_id for trigger in self.triggers]

    @property
    def messages(self):
        """The user's messages of the batch, in order."""
        return [trigger for trigger in self.triggers if trigger.type.from_user]

    @property
    def only(self):
        """The kind of a single-kind batch, or None."""
        names = {trigger.kind for trigger in self.triggers}
        return kinds.of(names.pop()) if len(names) == 1 else None

    def sources(self, numbers=None):
        """The Triggers a task tool acts for: the user's messages numbered in the input ([#n]),
        by default all of them, or the batch's first Trigger when there is none."""
        if not numbers: return self.messages or self.triggers[:1]
        picked = []
        for n in dict.fromkeys(numbers):
            trigger = next((t for t in self.triggers if t.n == n), None)
            if trigger is None or not trigger.type.from_user:
                raise ValueError("from_messages must list numbers of the user's messages in this turn: " + ", ".join(str(t.n) for t in self.messages))
            picked.append(trigger)
        return sorted(picked, key=lambda trigger: trigger.n)

    def attachments(self, sources):
        return list(dict.fromkeys(item for trigger in sources if trigger.type.from_user
                                  for item in (trigger.payload.get("artifact_ids") or [])))

    def selected(self, sources):
        """Tasks the user selected for these messages."""
        return list(dict.fromkeys(item for trigger in sources for item in (trigger.payload.get("work_item_ids") or [])))


def owned(db, turn, lock=False):
    """The Turn row while this worker still holds its lease, or TurnLost."""
    query = select(PersonalFrontTurn).where(PersonalFrontTurn.id == turn.id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None or row.status != "running" or row.owner != turn.owner or not row.lease_until or row.lease_until < time.time():
        raise TurnLost("Turn is no longer owned")
    return row


def source_of(agent, bound):
    """The source id of a turn (memory.sources): its first user message; for a turn of events
    only, the task an event reports on (task-<id>, never the owner's trust) or else its row."""
    from personal_agent.memory.sources import for_message
    first = next((trigger for trigger in bound if trigger.type.from_user and trigger.message), None)
    if first is not None: return for_message(agent, first.message)
    event = next((trigger for trigger in bound if trigger.message), None)
    if event is None: return None
    task_id = (event.message.extra_data or {}).get("work_item_id")
    return "task-" + task_id if task_id else for_message(agent, event.message)


def with_source(extra, source_id):
    """The turn context with the line that names this turn's source id, inside its block."""
    if not source_id: return extra
    line = "本轮来源 id：" + source_id
    if extra.endswith(context.CONTEXT_CLOSE):
        return extra[: -len(context.CONTEXT_CLOSE)] + "\n\n" + line + context.CONTEXT_CLOSE
    return extra + "\n\n" + line


# ---------------------------------------------------------------- running a turn

def prepare(db, turn_id, owner):
    """Load what the turn needs and open its reply. None when the turn ended elsewhere."""
    row = db.scalar(select(PersonalFrontTurn).where(PersonalFrontTurn.id == turn_id).with_for_update())
    if row is None or row.status != "running" or row.owner != owner: return None
    batch = triggers.claimed(db, turn_id)
    if not batch: return None
    agent = db.get(PersonalAgent, row.agent_id)
    bound = [trigger.bound(db) for trigger in batch]
    turn = Turn(row, batch, Grants.combine(trigger.type.grants(db, trigger) for trigger in bound))
    turn.source_id = source_of(agent, bound)
    reply = ReplyStream(turn)
    reply.open(db)
    turn.steering = FrontSteering(turn, reply)
    row.reply_ids = [reply.id]
    query = "\n\n".join(trigger.message.content for trigger in bound if trigger.type.from_user) or bound[0].message.content
    db.commit()
    return {"turn": turn, "reply": reply, "uid": agent.user_id, "chat_id": turn.chat_id, "box": agent.sandbox_session_id,
        "config": dict(agent.config or {}), "query": query[:4000], "history": context.history(db, turn.message_ids, [reply.id]),
        "attempts": turn.attempts}


async def converse(plan, binding):
    """Stream the agent's turn into its reply."""
    from core.llm.factory import create_agent_executor
    from core.llm.runtime.streaming import StreamingAgent
    from personal_agent.identity.capabilities import front_mcp_ids, runtime_config
    def _agent_config(db):
        config = runtime_config(db, plan["uid"], plan["config"])
        return config
    config = await run_in_session(_agent_config)
    reply, turn = plan["reply"], plan["turn"]
    effort = config.get("thinking_effort", "fast")
    instance, clients = await create_agent_executor(user_query=plan["query"], current_user_id=plan["uid"],
        chat_id=binding.chat_id, run_id=binding.run_id, journal_owner=binding.owner, sandbox_session_id=plan["box"],
        enabled_skill_ids=[], enabled_mcp_ids=front_mcp_ids(config), enabled_kb_ids=[], visible_subagents=[],
        top_level_chat=False, model_provider_id=config.get("model_provider_id"), chat_mode=effort,
        profile=PROFILE, profile_ref=turn.id)
    stream = StreamingAgent(instance, clients)
    # After the agent is built: building it settles a finished first run (see persona.settle_bootstrap).
    extra = with_source(await asyncio.to_thread(context.turn_context, turn.message_ids), turn.source_id)
    try:
        async for event, payload in stream.stream(session_messages=context.with_context(plan["history"], extra), context={"user_id": plan["uid"],
                "chat_id": binding.chat_id, "run_id": binding.run_id, "journal_owner": binding.owner, "model_provider_id": config.get("model_provider_id"), "chat_mode": effort, "enable_thinking": effort != "fast"}):
            if reply.take(event, payload): await asyncio.to_thread(reply.flush)
            if event == "error": raise RuntimeError("Model execution failed")
    finally:
        await stream.shutdown()


def fallback(db, turn, ids):
    """What the user sees when the agent wrote nothing: the events' own text, the touched tasks, or FAILED."""
    if not turn.messages:
        texts = [text for trigger in turn.triggers if (text := trigger.type.fallback(db, trigger)) is not None]
        if texts: return "\n\n".join(texts)
    tasks = [task for ident in ids if (task := db.get(PersonalWorkItem, ident))]
    return "\n".join(task_ops.status_line(task) for task in tasks) or FAILED


def linked(text):
    """A summary in which every news item has its link. A news item is the paragraph after a (news:<url>)
    tag line. One that shows no link gets the URL of its tag; when the tag has none, the item is left
    out with its tag, so it is not remembered as told either."""
    from personal_agent.scheduling import heartbeat
    text = text or ""
    marks = list(heartbeat.TAG_LINE.finditer(text))
    # From the end, so that the positions before a change stay valid.
    for mark, following in reversed(list(zip(marks, [*marks[1:], None]))):
        ref = (heartbeat.take_refs(mark.group(0))[1] or [""])[0]
        if not ref.startswith("news:"): continue
        body = text[mark.end():following.start() if following else len(text)]
        item = body.strip().split("\n\n")[0]
        if not item or re.search(r"https?://", item): continue
        end = mark.end() + body.index(item) + len(item)
        url = ref[len("news:"):]
        if re.match(r"https?://", url): text = text[:end] + "\n" + url + text[end:]
        else: text = text[:mark.start()] + text[end:]
    return text


def unlinked(text):
    """A summary of a check that does not look for news, without what reads as news: a paragraph that gives
    a web address and does not belong to a matter the platform or the task named by its tag. A heading
    left with nothing under it goes too."""
    from personal_agent.scheduling import heartbeat
    kept = []
    for block in re.split(r"\n[ \t]*\n", text or ""):
        mark = heartbeat.TAG_LINE.match(block)
        ref = (heartbeat.take_refs(mark.group(0))[1] or [""])[0] if mark else ""
        if re.search(r"https?://", block) and (not ref or ref.startswith("news:")):
            if kept and "\n" not in kept[-1].strip() and kept[-1].rstrip().endswith((":", "：")): kept.pop()
            continue
        kept.append(block)
    return "\n\n".join(kept).strip()


def untagged(db, reply, text, told=(), news=True):
    """(text, refs): a proactive summary without its tag lines, and the matters they name. News items
    are checked for their link first (see linked), and one whose addresses the user already got
    (told: heartbeat.told_news) is left out and not counted as told again. news=False: the check
    does not look for news, and what reads as news is left out (see unlinked).
    The tags also leave the display segments and the rows of earlier segments (a turn that took in
    new input wrote more than one), so no view of the reply shows them."""
    from personal_agent.scheduling import heartbeat
    def clean(text):
        text, refs = heartbeat.take_refs(linked(text) if news else unlinked(text))
        text, gone = heartbeat.without_told_news(text, told)
        return text, [ref for ref in refs if not (ref.startswith("news:") and heartbeat.same_page(ref[5:]) in gone)]
    text, refs = clean(text)
    for part in reply.parts:
        # New objects, not changed ones: a stored row is only written again when its value differs.
        part.segments[:] = [{**segment, "text": clean(segment["text"])[0]} if segment.get("type") == "text" else segment
                            for segment in part.segments]
        row = db.get(ChatMessage, part.id) if part.id and part is not reply.current else None
        if row is None: continue
        row.content, more = clean(row.content)
        row.extra_data = {**(row.extra_data or {}), "segments": part.segments[-300:]}
        refs = refs + more
    return text, list(dict.fromkeys(refs))


def tagged(text):
    """[{"ref", "text"}]: each tag of a summary with the start of the paragraph after it. Kept on the
    message, so that the user can later say which matter not to raise again (pa_proactive_dismiss)."""
    from personal_agent.scheduling import heartbeat
    marks = list(heartbeat.TAG_LINE.finditer(text or ""))
    out = []
    for mark, following in zip(marks, [*marks[1:], None]):
        said = text[mark.end():following.start() if following else len(text)].strip()
        # The ref as take_refs reads it: the pattern's groups are its own business.
        ref = heartbeat.take_refs(mark.group(0))[1]
        if said and ref: out.append({"ref": ref[0], "text": said[:120]})
    return out[:20]


def with_note(reply, message, text, note):
    """Add a sentence of the platform's own to the stored reply (the first summary's explanation, a restore). The new text."""
    segments = reply.current.segments
    # A new object, not a changed one: a stored row is only written again when its value differs.
    if segments and segments[-1].get("type") == "text": segments[-1] = {**segments[-1], "text": segments[-1]["text"] + "\n\n" + note}
    else: segments.append({"type": "text", "text": note})
    text = (text + "\n\n" + note)[:100000]
    reply.store(message, text)
    return text


def finish(turn, reply, failed=False):
    """Close the reply, settle every Trigger and end the Turn in one transaction. False when the turn was lost."""
    from personal_agent.scheduling import heartbeat
    from personal_agent.scheduling.proactive_guard import NOTE as RESTORE_NOTE
    outcome, refs, told_text, summary_of = None, [], None, None
    with SessionLocal() as db:
        try:
            row = owned(db, turn, lock=True)
        except TurnLost:
            return False
        release(db, turn)
        batch = {trigger.message_id: trigger.bound(db) for trigger in turn.triggers}
        message = reply.final(db)
        links = {}
        for ident, task_id in db.execute(select(PersonalMessageTask.message_id, PersonalMessageTask.work_item_id)
                .where(PersonalMessageTask.message_id.in_(turn.message_ids))):
            links.setdefault(ident, []).append(task_id)
        links = {ident: list(dict.fromkeys(items)) for ident, items in links.items()}
        ids = list(dict.fromkeys(task_id for ident in turn.message_ids for task_id in links.get(ident, [])))
        only = turn.only
        # A proactive summary: written by the check itself, or reporting the task it started.
        reported = any(kinds.proactive_result(db, trigger) for trigger in batch.values())
        summary = reported or only is kinds.KINDS["heartbeat"]
        if only is kinds.KINDS["heartbeat"]:
            # A check never reports a failure or "nothing found" to the user; it records the outcome.
            outcome = only.outcome(db, turn, reply, ids, failed)
            turn.quiet, failed = outcome != "alert", False
        elif reported and turn.grants.unattended and (failed or (not turn.quiet and heartbeat.is_quiet_text(heartbeat.take_refs(reply.content)[0]))):
            # Nobody asked for this summary: one that failed or was not written leaves nothing. The task's
            # report is never sent in its place; the matters stay untold, so the next check raises them.
            outcome, turn.quiet, failed = "task_failed", True, False
        elif only is kinds.KINDS["bootstrap"] and failed:
            # Nobody asked for the greeting: a failed one leaves nothing. The user's first message follows BOOTSTRAP.md.
            turn.quiet, failed = True, False
        # News the user already got is not sent again (the task that searches does not see what was told).
        agent = db.get(PersonalAgent, turn.agent_id)
        told = heartbeat.told_news((agent.config or {}).get("heartbeat") or {}) if summary and not failed else set()
        # A check whose admission said it does not look for news sends none (see unlinked).
        news = not summary or kinds.links(agent) is not False
        if (told or not news) and not failed and not turn.quiet and turn.grants.quiet:
            left = heartbeat.without_told_news(heartbeat.take_refs(linked(reply.content) if news else unlinked(reply.content))[0], told)[0]
            if not left.strip() or heartbeat.is_quiet_text(left):
                # Nothing else was written: the turn ends as one that had nothing to tell.
                turn.quiet = True
                if only is kinds.KINDS["heartbeat"]: outcome = "delegated" if ids else "quiet"
        # A task that tried to change the agent's settings was put back; the user hears of it from the
        # platform, also when the summary left it out or there is nothing else to tell.
        restored = reported and any(kinds.restored(db, trigger) for trigger in batch.values())
        alone = restored and turn.quiet and not failed
        if alone: turn.quiet, outcome = False, outcome or "nothing"
        if turn.quiet and not failed:
            from personal_agent.channels.im_actions import mark_placeholder_recall
            for trigger in turn.messages: mark_placeholder_recall(db, trigger.message_id)
            reply.discard(db)
            row.reply_ids = []
            if reported and not outcome: outcome = "nothing"
        else:
            text = reply.content
            if failed and not turn.messages: text = fallback(db, turn, ids)
            elif failed: text = text + "\n\n" + FAILED if text else fallback(db, turn, ids)
            elif not text: text = fallback(db, turn, ids)
            if alone:
                # Nothing of what the turn wrote is sent, only the platform's sentence.
                text, reply.current.segments[:] = RESTORE_NOTE, [{"type": "text", "text": RESTORE_NOTE}]
            elif summary:
                text = linked(text) if news else unlinked(text)
                said = tagged(text)
                # Before the text is stored and queued: a tag left in it would reach the user's IM.
                text, refs = untagged(db, reply, text, told, news)
                if reported:
                    # Only tags were written, in a turn that also answers someone: what stands in has no tags either.
                    if not text: text, refs = untagged(db, reply, fallback(db, turn, ids), told, news)
                    outcome = "reported"
                told_text = text
                if said: message.extra_data = {**(message.extra_data or {}), "proactive_refs": said}
                if not failed: summary_of = text
            reply.store(message, text[:100000])
            if restored and RESTORE_NOTE not in text:
                text = with_note(reply, message, text, RESTORE_NOTE)
                if summary_of is not None: summary_of = text
            from core.infra.time import utc_now
            db.get(ChatSession, message.chat_id).last_message_at = utc_now()
            message.extra_data = {**message.extra_data, "streaming": False, "activity": None}
            # Each delivered segment answers the inputs before it (see ReplyStream.groups).
            for part, answered in reply.groups(failed):
                settle(db, turn, [batch[trigger.message_id] for trigger in answered], db.get(ChatMessage, part.id), links, failed)
            row.reply_ids = reply.ids
        for trigger in batch.values():
            inbox = db.get(PersonalMessageInbox, trigger.message_id)
            inbox.status = "failed" if failed else "done"
            inbox.decision = {**(inbox.decision or {}), "work_item_ids": links.get(trigger.message_id, []), "quiet": bool(turn.quiet),
                              **({"outcome": outcome} if outcome else {})}
        from core.infra.time import utc_now
        row.status, row.owner, row.lease_until, row.finished_at = ("failed" if failed else "done"), None, None, utc_now()
        if summary_of is not None:
            # The last step before the commit: the note is given once, so nothing that can still fail
            # in this settlement may come after it.
            note = heartbeat.first_summary_note(turn.agent_id)
            if note: with_note(reply, message, summary_of, note)
        db.commit()
    if outcome:
        heartbeat.record(turn.agent_id, outcome)
        # The text goes along: a matter the summary names by its title was told, also without a tag.
        if refs or told_text: heartbeat.remember_told(turn.agent_id, refs, text=told_text)
    return True


def release(db, turn):
    """Triggers claimed for this turn that it never took in go back for the next turn, so none is lost."""
    for inbox in db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.turn_id == turn.id,
            PersonalMessageInbox.status == "claimed", PersonalMessageInbox.message_id.not_in(turn.message_ids)).with_for_update()):
        inbox.status, inbox.turn_id = "pending", None


def settle(db, turn, batch, message, links, failed):
    """Link a delivered reply segment to each Trigger it answers and queue it for delivery once.

    The segment answers the last user message from IM and takes over its IM placeholder; the
    other messages' placeholders are recalled. When no user message is delivered (none, or only
    web ones), the reply goes where the first event that has an IM conversation came from."""
    from personal_agent.channels.im_actions import mark_placeholder_recall
    for trigger in batch: trigger.type.settle(db, trigger, message, links.get(trigger.message_id, []))
    users = [trigger for trigger in batch if trigger.type.from_user]
    # The answer goes to the last message that came from IM (a web message needs no delivery);
    # without one, to the first event that has an IM conversation.
    im = [trigger for trigger in users if trigger.message and (trigger.message.extra_data or {}).get("channel_origin")]
    target = (im or users)[-1:]
    for trigger in users:
        if trigger not in target: mark_placeholder_recall(db, trigger.message_id)
    if not any(trigger.type.deliver(db, trigger, message) for trigger in target):
        for trigger in batch:
            if not trigger.type.from_user and trigger.type.deliver(db, trigger, message): break
    if users:
        from personal_agent.workspace.file_memory import admit as admit_memory
        sources = [trigger.message.content for trigger in users if trigger.message]
        admit_memory(message, db.get(PersonalAgent, turn.agent_id), "conversation_failed" if failed else "conversation",
                     input_text="\n\n".join(sources))


async def run_turn(turn_id, owner, execute=None):
    """One complete turn. execute(plan, binding) replaces the model in tests."""
    from core.runs.journal import durable_run_binding
    plan = await asyncio.to_thread(_prepare, turn_id, owner)
    if plan is None: return
    turn, reply = plan["turn"], plan["reply"]
    # A heartbeat check gets one attempt; the next check comes soon.
    if plan["attempts"] > (1 if turn.only is kinds.KINDS["heartbeat"] else MAX_ATTEMPTS):
        # The turn was interrupted repeatedly: do not run it again, tell the user.
        await asyncio.to_thread(finish, turn, reply, True)
        return
    token = current_turn.set(turn)
    try:
        async with durable_run_binding(user_id=plan["uid"], chat_id=plan["chat_id"], kind="personal_agent_turn", external_id=turn.id) as binding:
            await (execute or converse)(plan, binding)
        await asyncio.to_thread(finish, turn, reply)
    except TurnLost:
        log.info("Personal turn %s moved to another worker", turn.id)
    except asyncio.CancelledError:
        # Shutdown: the lease expires and another worker runs the turn again.
        raise
    except Exception as exc:
        log.warning("Personal turn failed: %s (%s)", turn.id, type(exc).__name__)
        await asyncio.to_thread(finish, turn, reply, True)
    finally:
        current_turn.reset(token)


def _prepare(turn_id, owner):
    with SessionLocal() as db: return prepare(db, turn_id, owner)
