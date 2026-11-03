"""The agent's reply in the conversation, written while it streams, in segments (ROUTER_DESIGN §5).

A turn that absorbs new input (see steering.py) keeps what it wrote so far where it was and
continues in a new row after that input, so the conversation reads A → reply → B → reply.
A tool call's result goes to the row that holds the call. Rows of segments that ended with
nothing in them are removed. Settlement (turn.finish) decides which segment answers which
Trigger; see ReplyStream.groups.
"""
from sqlalchemy import select

from core.db.engine import SessionLocal
from core.db.models import ChatMessage, ChatSession
from core.db.repository.chat import ChatMessageRepository
from personal_agent.conversation.router import kinds
from personal_agent.tasks.task_threads import StreamRecord

# Events that change what a segment holds.
CONTENT = {"model_step", "text_delta", "thinking_delta", "tool_call", "tool_result"}


class Segment(StreamRecord):
    """One row of a segmented record. joined: the inputs that arrived just before it."""

    def __init__(self, index, joined=()):
        super().__init__()
        self.index, self.joined = index, list(joined)
        self.id, self.dirty, self.closed = None, True, False
        # The user message this segment answers on IM.
        self.reply_to = None

    @property
    def empty(self):
        return not (self.segments or self.model_steps)

    @property
    def content(self):
        """The answer is the text after the last tool call; when the segment ended on a tool call
        (for example a sentence and then starting a task), it is the last text written."""
        texts = [segment["text"] for segment in self.segments if segment["type"] == "text" and segment["text"].strip()]
        return self.text.strip() or (texts[-1].strip() if texts else "")


class SegmentedRecord:
    """A stream record kept as one or more rows. boundary(inputs) arms a new segment; it opens at
    the next model call, which the stream orders after the input was injected, so output of the
    previous call still in flight stays in its row. Subclasses open, store and drop the rows."""

    def __init__(self):
        self.parts = [Segment(0)]
        # Tool call ID -> the segment that holds the call.
        self.calls = {}
        self.armed = None

    @property
    def current(self):
        return self.parts[-1]

    def boundary(self, joined):
        self.armed = [*(self.armed or []), *joined]

    def take(self, event, payload):
        """Add one stream event. True when the record should be written now."""
        if event == "model_call_start" and self.armed is not None:
            self.current.closed = self.current.dirty = True
            self.parts.append(Segment(len(self.parts), self.armed))
            self.armed = None
        part = self.current
        if event == "tool_result":
            part = self.calls.get(payload.get("id"), part)
        elif event == "model_step" and payload.get("kind") == "tool_result":
            ids = [block.get("id") for block in payload.get("blocks") or []]
            part = next((self.calls[ident] for ident in ids if ident in self.calls), part)
        elif event == "tool_call":
            self.calls[payload["id"]] = part
        elif event == "model_step" and payload.get("kind") == "assistant":
            for block in payload.get("blocks") or []:
                if block.get("type") == "tool_call": self.calls[block["id"]] = part
        if event in CONTENT: part.dirty = True
        return part.take(event, payload) or part is not self.current

    def write(self, db):
        """Write the changed segments, in order, so a new row follows the inputs before it."""
        for part in self.parts:
            if not part.dirty: continue
            part.dirty = False
            if part.closed and part.empty:
                if part.id: self.drop_row(db, part)
                part.id = None
                continue
            if part.id is None: self.open_row(db, part)
            self.store_row(db.get(ChatMessage, part.id), part)

    def open_row(self, db, part):
        raise NotImplementedError

    def store_row(self, row, part):
        part.store(row, part.content[:100000])

    def drop_row(self, db, part):
        raise NotImplementedError


class ReplyStream(SegmentedRecord):
    """The Router's reply: the first segment's row is opened when the turn starts; canonical steps
    serve model history, display columns serve the page."""

    def __init__(self, turn):
        super().__init__()
        self.turn, self.activity = turn, None

    @property
    def base(self):
        from personal_agent.conversation.router.turn import reply_id
        return reply_id(self.turn.head)

    def ident(self, index):
        return self.base if index == 0 else self.base + "_" + str(index)

    @property
    def id(self):
        """The final segment's row."""
        return self.current.id

    @property
    def ids(self):
        return [part.id for part in self.parts if part.id]

    @property
    def content(self):
        return self.current.content

    def store(self, row, content):
        self.current.store(row, content)

    def open(self, db):
        """Open the first segment. A retried or reprocessed turn writes its reply again: the segments an
        earlier attempt wrote are removed, and its first row is reused while it still follows the input."""
        turn = self.turn
        for row in db.scalars(select(ChatMessage).where(ChatMessage.chat_id == turn.chat_id,
                ChatMessage.message_id.startswith(self.base + "_", autoescape=True))).all():
            self._delete(db, row)
        part = self.parts[0]
        part.joined = list(turn.triggers)
        row = db.get(ChatMessage, self.base)
        if row is not None and row.chat_seq < max(trigger.seq for trigger in turn.triggers):
            # It was written before inputs the earlier attempt absorbed.
            self._delete(db, row)
            db.flush()
            row = None
        if row is None:
            self.open_row(db, part)
        else:
            row.model_steps = []
            row.content, row.tool_calls, row.thinking = "", [], []
            row.extra_data = {**(row.extra_data or {}), **self._extra(part), "segments": []}
            part.id = self.base
        part.dirty = False

    def _extra(self, part):
        turn = self.turn
        users = [trigger for trigger in part.joined if trigger.type.from_user]
        # A later segment answers the messages that arrived before it; otherwise it continues the earlier answer.
        reply_to = users[-1].message_id if users else next((p.reply_to for p in reversed(self.parts[:part.index]) if p.reply_to), None)
        part.reply_to = reply_to
        extra = {"reply_to": reply_to, "streaming": True, "turn": turn.head}
        # Only a segment that reports a task result (and answers no message) belongs to that task.
        task_id = next((t.payload.get("task_id") for t in part.joined if t.payload.get("task_id")), None)
        if task_id and not users: extra["work_item_id"] = task_id
        # A heartbeat reply stays hidden until it is known to be an alert (see service.messages).
        if turn.only is kinds.KINDS["heartbeat"]: extra["heartbeat_turn"] = True
        return extra

    def open_row(self, db, part):
        ident = self.ident(part.index)
        ChatMessageRepository(db).create({"message_id": ident, "chat_id": self.turn.chat_id, "role": "assistant",
            "content": "", "extra_data": self._extra(part)}, commit=False)
        db.get(ChatSession, self.turn.chat_id).message_count += 1
        part.id = ident

    def store_row(self, row, part):
        super().store_row(row, part)
        if part.closed:
            # An earlier segment: done streaming; the final one is closed by turn.finish.
            row.extra_data = {**row.extra_data, "streaming": False, "activity": None, "front_intermediate": True}
        else:
            row.extra_data = {**row.extra_data, "activity": self.activity}

    def drop_row(self, db, part):
        self._delete(db, db.get(ChatMessage, part.id))

    def _delete(self, db, row):
        if row is None: return
        db.delete(row)
        session = db.get(ChatSession, row.chat_id)
        session.message_count = max(0, session.message_count - 1)

    def take(self, event, payload):
        # The tool name of the step in progress; the page shows its own label for it.
        if event == "tool_call": self.activity = str(payload.get("name") or "")
        elif event == "tool_result": self.activity = None
        return super().take(event, payload)

    def flush(self):
        from personal_agent.conversation.router.turn import owned
        with SessionLocal() as db:
            row = owned(db, self.turn)
            self.write(db)
            row.reply_ids = self.ids
            db.commit()

    def final(self, db):
        """Write everything and make sure the final segment has a row, for its text or the fallback."""
        self.write(db)
        if self.current.id is None: self.open_row(db, self.current)
        return db.get(ChatMessage, self.current.id)

    def discard(self, db):
        """A quiet turn leaves no reply."""
        for part in self.parts:
            if part.id: self._delete(db, db.get(ChatMessage, part.id))
            part.id = None

    def groups(self, failed):
        """(segment, Triggers it answers) for each segment that is delivered, in order.

        A segment with text answers the inputs that arrived since the previous delivered one; a
        segment without text (only tool calls) passes them on. The final segment answers the rest,
        including input absorbed after the last model call began. A failed turn delivers only the final one."""
        out, waiting = [], []
        for part in self.parts:
            waiting += part.joined
            if part is self.current:
                out.append((part, waiting + list(self.armed or [])))
            elif part.id and not failed and part.content and waiting:
                out.append((part, waiting))
                waiting = []
        return out

