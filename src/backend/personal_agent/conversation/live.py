"""Tell open pages when a person's Personal Agent changed, instead of having them poll.

The feed rides on the chat runs' event log (:mod:`core.runs.events.store`): the
same Redis Streams backend, blocking reads, connection recovery and cursor
grammar, or the same in-process log where no Redis is configured. One log per
person, keyed by user, carries only "something changed" signals; a page that
hears one reads the changed state through the ordinary endpoints, so a lost
signal costs nothing that the next one, or a reconnect, does not repair.

Signals come from the database rather than from each place that writes: a
commit that touched a Personal Agent row is announced after it lands. Writes
that do not change what a page shows are left out — a reply's text while it is
still streaming (the page shows a reply only once it is complete) and a
computer's heartbeat (only coming online or going offline is shown).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from typing import Any, AsyncIterator, Dict, Iterable, Optional

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from core.infra.logging import get_logger
from core.infra.redis import get_redis, redis_configured
from core.runs.events import store
from core.runs.events.projection import ProjectionCodec

logger = get_logger(__name__)

# The feed keeps only recent signals: a page that was away longer reloads everything on reconnect.
MAXLEN = 200
TTL_SECONDS = 24 * 3600
# Signals within this window are sent as one, so a burst of commits costs one read on the page.
COALESCE_SECONDS = 0.15
# A quiet connection still sends a comment this often, so proxies keep it open and a closed page is noticed.
KEEPALIVE_SECONDS = 15
CHAT_PREFIX = "pa_chat_"

# The feed carries no run state to fold, so its projection is empty.
_NO_PROJECTION = ProjectionCodec(
    new_projection=lambda _key: {},
    fold_projection=lambda state, _event: state,
    affected_tools=lambda _state, _event: set(),
    encode_checkpoint=lambda *_args, **_kwargs: {},
    decode_checkpoint=lambda *_args, **_kwargs: None,
)


class RedisFeed(store.RedisRunEventStream):
    """The run log's Redis backend under its own key, with a plain append and a TTL."""

    def _key(self, run_id: str) -> str:
        return "jx:pa:feed:" + run_id

    async def append(self, run_id: str, event: Dict[str, Any]) -> None:
        async with get_redis().pipeline(transaction=False) as pipe:
            pipe.xadd(self._key(run_id), {"data": json.dumps(event, ensure_ascii=False)}, maxlen=MAXLEN, approximate=True)
            pipe.expire(self._key(run_id), TTL_SECONDS)
            await pipe.execute()

    async def tail(self, run_id: str) -> str:
        entries = await get_redis().xrevrange(self._key(run_id), max="+", min="-", count=1)
        if not entries:
            return "0-0"
        cursor = entries[0][0]
        return cursor.decode() if isinstance(cursor, bytes) else str(cursor)


class LocalFeed(store.LocalRunEventStream):
    """The run log's in-process backend; old signals expire with the log."""

    async def append(self, run_id: str, event: Dict[str, Any]) -> None:
        await super().append(run_id, event)
        await self.expire(run_id, TTL_SECONDS)

    async def tail(self, run_id: str) -> str:
        found = await self.read(run_id)
        return found[-1][0] if found else store.START


_REDIS = RedisFeed(_NO_PROJECTION)
_LOCAL = LocalFeed(_NO_PROJECTION)


def feed():
    return _REDIS if redis_configured() else _LOCAL


async def follow(user_id: str) -> AsyncIterator[str]:
    """SSE frames for one person: ``ready`` at once, then one frame per signal.

    A signal names its topics: a conversation id when that conversation's messages changed,
    ``operation:<id>`` when a session operation's receipt changed, ``device:<id>`` and
    ``device:<id>:<chat>`` when a linked computer's own chat list or one of its chats changed,
    ``channels`` when one of the person's channel bots changed, ``computer`` when the agent changed
    what the cloud computer's screen shows. Tasks, computers and the agent's settings change without
    a topic of their own.
    """
    log = feed()
    try:
        cursor = await log.tail(user_id)
    except Exception as exc:  # noqa: BLE001 - a page that cannot follow still loads everything once
        logger.warning("pa_feed_tail_failed", error=type(exc).__name__)
        cursor = store.START
    # The page loads everything when it hears this, so changes made before the connection are not missed.
    yield "data: " + json.dumps({"type": "ready"}) + "\n\n"
    last = time.monotonic()
    while True:
        try:
            batch = await log.wait(user_id, after=cursor, limit=50, timeout_ms=KEEPALIVE_SECONDS * 1000)
        except Exception as exc:  # noqa: BLE001 - the Redis backend already dropped the bad connection
            if type(exc).__name__ != "TimeoutError":
                logger.warning("pa_feed_wait_failed", error=type(exc).__name__)
                await asyncio.sleep(1)
            batch = []
        if batch:
            cursor = batch[-1][0]
            topics = sorted({topic for _cursor, signal in batch for topic in signal.get("topics", ())})
            yield "data: " + json.dumps({"type": "changed", "topics": topics}) + "\n\n"
            last = time.monotonic()
        elif time.monotonic() - last >= KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            last = time.monotonic()


# ── Collecting signals from commits ──────────────────────────────────────────

def _changed(obj, *names) -> bool:
    state = inspect(obj)
    return any(state.attrs[name].history.has_changes() for name in names)


def _streaming_text_only(message) -> bool:
    """A reply still being written, and still being written after this change: the page does not show it yet."""
    history = inspect(message).attrs.extra_data.history
    before = (history.deleted[0] if history.deleted else message.extra_data) or {}
    after = message.extra_data or {}
    return bool(before.get("streaming") and after.get("streaming"))


def _device_came_online(device) -> bool:
    from personal_agent.computer.devices import ONLINE_WINDOW
    if _changed(device, "name", "capabilities", "revoked", "control_enabled", "sessions_enabled", "epoch", "link_info"):
        return True
    history = inspect(device).attrs.last_seen.history
    if not history.has_changes():
        return False
    before = history.deleted[0] if history.deleted else None
    return not before or before <= time.time() - ONLINE_WINDOW


# Bookkeeping columns no page shows: renewing a lease is not a change.
_QUIET_COLUMNS = {"lease_until"}
# A bot hearing a message stamps these; its list entry looks the same.
_QUIET_CHANNEL_COLUMNS = _QUIET_COLUMNS | {"last_event_at", "updated_at"}
CHANNELS_TOPIC = "channels"
COMPUTER_TOPIC = "computer"


def _really_changed(obj, quiet=_QUIET_COLUMNS) -> bool:
    """A modified row that holds a different value afterwards, outside the bookkeeping columns.

    Writers often assign a value equal to the stored one (a turn's reply ids on every flush); the
    session still lists the row as modified, but nothing a page shows has changed.
    """
    return any(attr.key not in quiet and attr.history.has_changes() for attr in inspect(obj).attrs)


def _refs(session: Session) -> set:
    """("user"|"agent"|"chat"|"message", id, topic) for every Personal Agent row this flush wrote."""
    from core.db.models import ChatMessage, ChatSession
    from integrations.channels.models import ChannelConnection
    from personal_agent import models

    refs = set()
    dirty = [(o, "dirty") for o in session.dirty
             if _really_changed(o, _QUIET_CHANNEL_COLUMNS if isinstance(o, ChannelConnection) else _QUIET_COLUMNS)]
    for obj, kind in [(o, "new") for o in session.new] + dirty + [(o, "deleted") for o in session.deleted]:
        if isinstance(obj, ChatMessage):
            chat = obj.chat_id or ""
            if chat.startswith(CHAT_PREFIX) and not (kind == "dirty" and _streaming_text_only(obj)):
                refs.add(("chat", chat, chat))
        elif isinstance(obj, ChatSession):
            if (obj.chat_id or "").startswith(CHAT_PREFIX) and obj.user_id:
                refs.add(("user", obj.user_id, obj.chat_id))
        elif isinstance(obj, models.PersonalDevice):
            if kind != "dirty" or _device_came_online(obj):
                refs.add(("user", obj.user_id, None))
        elif isinstance(obj, models.PersonalAgent):
            refs.add(("user", obj.user_id, None))
        elif isinstance(obj, models.PersonalSessionOperation):
            refs.add(("user", obj.user_id, "operation:" + obj.id))
        elif isinstance(obj, (models.PersonalWorkItem, models.PersonalFrontTurn, models.PersonalAssignment)):
            refs.add(("agent", obj.agent_id, None))
        elif isinstance(obj, models.PersonalMessageTask):
            refs.add(("message", obj.message_id, None))
        elif isinstance(obj, ChannelConnection):
            # A bot connected, failed, was renamed, enabled or removed: the channel list shows it.
            if obj.owner_user_id:
                refs.add(("user", obj.owner_user_id, CHANNELS_TOPIC))
    return refs


# Who owns an agent or a conversation never changes, so a lookup is remembered (bounded).
_OWNERS: Dict[tuple, str] = {}
_OWNERS_MAX = 20000


def _owner(session: Session, kind: str, ident: Optional[str]) -> Optional[str]:
    """The person a ref belongs to, looked up on the committing session itself.

    Resolving here, not in the publisher, keeps every database read on the writer's own
    connection and thread; the publisher touches no database at all.
    """
    if kind == "user" or not ident:
        return ident
    found = _OWNERS.get((kind, ident))
    if found:
        return found
    from core.db.models import ChatMessage, ChatSession
    from personal_agent.models import PersonalAgent
    with session.no_autoflush:
        if kind == "message":
            row = session.get(ChatMessage, ident)
            return _owner(session, "chat", row.chat_id) if row else None
        row = session.get(PersonalAgent if kind == "agent" else ChatSession, ident)
    owner = row.user_id if row else None
    if owner:
        if len(_OWNERS) >= _OWNERS_MAX:
            _OWNERS.clear()
        _OWNERS[(kind, ident)] = owner
    return owner


def _keep(session: Session, refs: Iterable[tuple]) -> None:
    """Note (person, topic) for each ref, announced if and when the transaction commits."""
    signals = {(user, topic) for kind, ident, topic in refs if (user := _owner(session, kind, ident))}
    if signals:
        session.info.setdefault("pa_feed", set()).update(signals)


@event.listens_for(Session, "after_flush")
def _collect(session, _context):
    try:
        _keep(session, _refs(session))
    except Exception as exc:  # noqa: BLE001 - a missed signal must never fail the write
        logger.warning("pa_feed_collect_failed", error=type(exc).__name__)


def _bulk_owners():
    """For bulk UPDATE/DELETE statements on watched tables: the columns naming each row's owner, and its ref."""
    from core.db.models import ChatMessage
    from integrations.channels.models import ChannelConnection
    from personal_agent import models

    agent = ((lambda m: (m.agent_id,)), lambda row: ("agent", row[0], None))
    return {
        ChatMessage: ((lambda m: (m.chat_id,)),
                      lambda row: ("chat", row[0], row[0]) if (row[0] or "").startswith(CHAT_PREFIX) else None),
        models.PersonalWorkItem: agent, models.PersonalFrontTurn: agent, models.PersonalAssignment: agent,
        models.PersonalAgent: ((lambda m: (m.user_id,)), lambda row: ("user", row[0], None)),
        models.PersonalDevice: ((lambda m: (m.user_id,)), lambda row: ("user", row[0], None)),
        models.PersonalSessionOperation: ((lambda m: (m.user_id, m.id)), lambda row: ("user", row[0], "operation:" + row[1])),
        ChannelConnection: ((lambda m: (m.owner_user_id,)), lambda row: ("user", row[0], CHANNELS_TOPIC)),
    }


_BULK = None


@event.listens_for(Session, "do_orm_execute")
def _bulk(state):
    """Bulk statements bypass the session's lists of changed rows: find the rows they will touch first."""
    global _BULK
    if not (state.is_update or state.is_delete) or state.bind_mapper is None:
        return
    if _BULK is None:
        _BULK = _bulk_owners()
    found = _BULK.get(state.bind_mapper.class_)
    where = getattr(state.statement, "whereclause", None)
    if found is None or where is None:
        return
    from sqlalchemy import select
    columns, ref = found
    try:
        rows = state.session.execute(select(*columns(state.bind_mapper.class_)).where(where)).all()
        _keep(state.session, [made for made in map(ref, rows) if made])
    except Exception as exc:  # noqa: BLE001 - a missed signal must never fail the write
        logger.warning("pa_feed_bulk_failed", error=type(exc).__name__)


@event.listens_for(Session, "after_commit")
def _committed(session):
    signals = session.info.pop("pa_feed", None)
    if signals:
        _publisher().submit(signals)


@event.listens_for(Session, "after_rollback")
def _rolled_back(session):
    session.info.pop("pa_feed", None)


# ── Publishing ───────────────────────────────────────────────────────────────

class _Publisher:
    """One daemon thread per process turns committed (person, topic) pairs into one signal per person.

    A commit made on any thread — a request, a worker, a thread pool — only drops its
    pairs in a queue, never waiting for Redis. The thread blocks on that queue itself,
    not through an executor, so it never holds up the process's exit, and it reads no
    database: owners were resolved by the writer.
    """

    def __init__(self):
        self.queue: "queue.SimpleQueue[set]" = queue.SimpleQueue()
        threading.Thread(target=self._run, name="pa-feed", daemon=True).start()

    def submit(self, signals: Iterable[tuple]) -> None:
        self.queue.put(set(signals))

    def _run(self):
        loop = asyncio.new_event_loop()
        while True:
            signals = self.queue.get()
            time.sleep(COALESCE_SECONDS)
            while True:
                try:
                    signals |= self.queue.get_nowait()
                except queue.Empty:
                    break
            try:
                loop.run_until_complete(self.publish(signals))
            except Exception as exc:  # noqa: BLE001 - keep serving later commits
                logger.warning("pa_feed_publish_failed", error=type(exc).__name__)

    async def publish(self, signals: set) -> None:
        topics: Dict[str, set] = {}
        for user, topic in signals:
            found = topics.setdefault(user, set())
            if topic:
                found.add(topic)
        for user, found in topics.items():
            await feed().append(user, {"topics": sorted(found)})


_PUBLISHER: Optional[_Publisher] = None
_LOCK = threading.Lock()


def _publisher() -> _Publisher:
    global _PUBLISHER
    if _PUBLISHER is None:
        with _LOCK:
            if _PUBLISHER is None:
                _PUBLISHER = _Publisher()
    return _PUBLISHER


def announce(user_ids: Iterable[str], topics: Iterable[str] = ()) -> None:
    """Signal changes that no row write here shows: a computer going offline, a linked computer's own chats."""
    users = {user for user in user_ids if user}
    topics = list(topics) or [None]
    if users:
        _publisher().submit({(user, topic) for user in users for topic in topics})
