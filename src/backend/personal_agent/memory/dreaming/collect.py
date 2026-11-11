"""Collect a run's sources: the queue of journal receipts, rendered and split, then a snapshot.

Normal runs take receipts not yet dreamed. While genesis is requested or running, the run
replays every written receipt since genesis["since"], oldest first, from a cursor kept in
the genesis state, so the first wiki of an existing user is built in batches of MAX_SOURCES.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select

from core.db.models import ChatMessage
from personal_agent.memory import snapshots, state as memory_state
from personal_agent.memory.dreaming import render, runs
from personal_agent.workspace import files

MAX_SOURCES = 40
SCAN = 100


@dataclass
class Batch:
    sources: list = field(default_factory=list)  # {"index", "sid", "path", "text", "part", "parts", "receipt", "changes", "owner_words"}
    receipts: list = field(default_factory=list)  # message ids whose every part is in this batch
    skipped: list = field(default_factory=list)   # {"message_id", "reason"}
    provider: str | None = None
    genesis: bool = False
    exhausted: bool = True                        # genesis: nothing older is left after this batch
    cursor: dict | None = None                    # genesis: the last receipt of this batch
    source_bytes: int = 0                         # all sources ever dreamed, this batch included


def _since(value):
    try: parsed = datetime.fromisoformat(str(value))
    except ValueError: return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def genesis_query(agent, since, cursor):
    receipt = ChatMessage.extra_data[runs.RECEIPT]
    query = select(ChatMessage).where(receipt["agent_id"].as_string() == agent.id,
                                      receipt["status"].as_string() == "written")
    if since is not None: query = query.where(ChatMessage.created_at >= since)
    if cursor:
        at = _since(cursor.get("at"))
        if at is not None:
            query = query.where(or_(ChatMessage.created_at > at,
                                    and_(ChatMessage.created_at == at, ChatMessage.message_id > cursor.get("id", ""))))
    return query.order_by(ChatMessage.created_at, ChatMessage.message_id)


def _skip(row, reason):
    receipt = dict(row.extra_data[runs.RECEIPT])
    receipt.update(dreamed=True, dream_skipped=reason)
    row.extra_data = {**row.extra_data, runs.RECEIPT: receipt}


def prepare(sessions, ident, run_id, owner, limit=MAX_SOURCES):
    """Build the batch under the lease; marks receipts of deleted conversations as skipped."""
    from personal_agent.conversation.conversations import require_session
    with sessions() as db:
        agent, _row, _data = runs.owned(db, ident, run_id, owner)
        batch = Batch(provider=runs.provider(agent.config))
        genesis = memory_state.genesis(agent)
        if genesis.get("status") == "requested":
            memory_state.set_genesis(agent, "running", cursor=None)
            genesis = memory_state.genesis(agent)
        batch.genesis = genesis.get("status") == "running"
        query = genesis_query(agent, _since(genesis.get("since")), genesis.get("cursor")) if batch.genesis else runs.pending_query(agent)
        rows = db.scalars(query.limit(SCAN + 1)).all()
        batch.exhausted = len(rows) <= SCAN
        for row in rows[:SCAN]:
            try: require_session(db, agent, row.chat_id)
            except LookupError:
                _skip(row, "conversation unavailable")
                batch.skipped.append({"message_id": row.message_id, "reason": "对话已删除"})
                batch.cursor = _cursor(row)
                continue
            rendered = render.render(db, agent, row)
            parts = render.split(rendered["text"])[:limit]   # one huge receipt cannot outgrow the batch
            if batch.sources and len(batch.sources) + len(parts) > limit:
                batch.exhausted = False
                break
            for number, text in enumerate(parts, 1):
                index = len(batch.sources) + 1
                batch.sources.append({"index": index, "sid": rendered["sid"], "path": "sources/" + str(index) + ".txt",
                                      "text": text, "part": number, "parts": len(parts), "receipt": row.message_id,
                                      "changes": rendered["changes"], "owner_words": rendered["owner_words"] if number == 1 else [],
                                      "day": rendered["day"]})
            batch.receipts.append(row.message_id)
            batch.cursor = _cursor(row)
        dreaming = (agent.config or {}).get("dreaming_state") or {}
        batch.source_bytes = dreaming.get("source_bytes", 0) + sum(len(s["text"].encode()) for s in batch.sources)
        db.commit()
        return batch


def _cursor(row):
    created = row.created_at
    if created is not None and created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)
    return {"at": created.isoformat() if created else "", "id": row.message_id}


def snapshot(sessions, ident, run_id, owner):
    """Copy memory/ before this attempt's first write and remember it on the run row."""
    with sessions() as db:
        _agent, row, data = runs.owned(db, ident, run_id, owner)
        snapshot_id = (run_id + "-a" + str(data.get("attempts", 1)))[-80:]
        with files.workspace_lock(ident):
            snapshots.take(ident, snapshot_id, note="Dreaming " + run_id)
        data["snapshot"] = snapshot_id
        runs.save(row, data)
        db.commit()
    return snapshot_id


def recover(sessions, ident, run_id, owner):
    """Restore the snapshot of an attempt that ended without abort(); see runs.claim."""
    with sessions() as db:
        agent, row, data = runs.owned(db, ident, run_id, owner)
        if not data.get("recover"): return False
        restored = False
        if data.get("snapshot"):
            with files.workspace_lock(ident):
                snapshots.restore(db, agent, data["snapshot"], run_id=run_id)
            restored = True
        data.update(recover=False, rolled_back=restored)
        runs.save(row, data)
        db.commit()
        return restored
