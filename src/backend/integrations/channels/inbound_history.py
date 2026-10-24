"""Channel history responsibilities."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from integrations.channels.protocol import InboundMsg
from core.db.models import ChatSession
from core.db.threads import run_in_session
from integrations.channels.models import ChannelConnection
import logging
logger = logging.getLogger(__name__)
from integrations.channels import inbound_observation
from integrations.channels import inbound_sessions
from integrations.channels import inbound_state

def history_state(session: ChatSession) -> Dict[str, Any]:
    state = (session.extra_data or {}).get("history_pull")
    return dict(state) if isinstance(state, dict) else {}


def save_history_state(db, session: ChatSession, state: Dict[str, Any]) -> None:
    meta = dict(session.extra_data or {})
    meta["history_pull"] = state
    session.extra_data = meta
    db.commit()


def _pull_state(db, chat_id: str) -> Dict[str, Any]:
    return history_state(db.get(ChatSession, chat_id))


def _store_pull(
    db, chat_id: str, state: Dict[str, Any], records: List[Dict[str, Any]], file_index: Dict[str, Any]
) -> None:
    session = db.get(ChatSession, chat_id)
    if records:
        inbound_observation.record_observed_batch(db, session, records, file_index)
    save_history_state(db, session, state)


async def pull_history(adapter, conn: ChannelConnection, msg: InboundMsg, chat_id: str) -> int:
    """Deterministically pull this group's new messages and fold them into the buffer.

    Runs on every @ in a group, driven by code rather than by the model choosing to call a
    tool. Incremental by construction: the cursor stored on the session is the lower bound, so
    a pull only ever returns messages we have not already taken. The first pull in a
    conversation has no cursor and instead reaches back ``_HISTORY_COLDSTART_WINDOW_H`` hours
    — that is the cold start, and it is also what makes content from *before the bot joined*
    reachable at all.

    Deliberately independent of group listening: pushing and pulling sit behind different
    platform permissions, so a channel that will not push non-@ messages may still allow the
    pull. Returns the number of messages recorded.
    """
    fetch = getattr(adapter, "fetch_history", None)
    if fetch is None:
        return 0
    state = await run_in_session(_pull_state, chat_id)
    if int(state.get("failures") or 0) >= inbound_state.HISTORY_MAX_FAILURES:
        return 0

    cursor = int(state.get("cursor_ms") or 0)
    cold_start = not cursor
    if cold_start:
        # Timezone-aware on purpose: this value is compared against platform epoch-ms, and
        # the naive-utcnow habit elsewhere in this codebase is exactly what makes stored
        # timestamps read 8 hours off.
        cursor = int(
            (datetime.now(timezone.utc) - timedelta(hours=inbound_state.HISTORY_COLDSTART_WINDOW_H)).timestamp() * 1000
        )
    seen_ids = set(state.get("seen_ids") or [])

    try:
        items = await asyncio.wait_for(
            fetch(
                conn, msg.external_conversation_id,
                since_ms=cursor, limit=inbound_state.HISTORY_MAX_MESSAGES,
                # Cold start takes the newest end of the window: with a 7-day window and a
                # per-pull cap, taking the oldest would surface week-old chatter and leave
                # the recent messages — what the conversation is actually about — unread.
                newest_first=cold_start,
            ),
            timeout=inbound_state.HISTORY_TIMEOUT_S,
        )
        # Adapters may return either order; the buffer must read chronologically. Items
        # without a timestamp keep their relative position rather than sinking to the front.
        items = sorted(items or [], key=lambda it: int(getattr(it, "ts_ms", 0) or 0))
    except Exception:  # noqa: BLE001 — includes timeout; history is a bonus, never a blocker
        logger.warning(
            "[channels] 群历史拉取失败 channel_id=%s conv=%s",
            conn.channel_id, msg.external_conversation_id, exc_info=True,
        )
        state["failures"] = int(state.get("failures") or 0) + 1
        await run_in_session(_store_pull, chat_id, state, [], {})
        return 0

    records: List[Dict[str, Any]] = []
    file_index: Dict[str, Any] = {}
    budget = inbound_state.HISTORY_MAX_TOTAL_CHARS
    newest = cursor
    # Which end the budget cuts from differs by intent:
    #   cold start  — walk newest→oldest, so truncation drops the stale tail. There is no
    #                 continuity to preserve; this is a one-off seed.
    #   incremental — walk oldest→newest and stop at the budget, advancing the cursor only
    #                 over what was actually recorded, so the next pull resumes exactly there
    #                 and no message is skipped.
    for it in (reversed(items) if cold_start else items):
        mid = getattr(it, "message_id", "") or ""
        # Dedup on two fronts: the cursor boundary is inclusive on some platforms, and a
        # message may also have already arrived via push. Either way it must not be recorded
        # twice — that is what made repeated pulls wasteful.
        if mid and (mid in seen_ids or inbound_sessions.already_handled(f"hist:{mid}")):
            continue
        text = (getattr(it, "text", "") or "").strip()
        if len(text) > inbound_state.OBSERVE_TEXT_MAX:
            text = text[:inbound_state.OBSERVE_TEXT_MAX] + "…"
        refs = [
            {"kind": a.get("kind") or "file", "key": a.get("key") or "", "name": a.get("name") or ""}
            for a in (getattr(it, "attachments", None) or [])
            if a.get("key")
        ]
        if not text and not refs:
            continue
        if len(text) > budget:            # total budget exhausted → stop, keep the cursor honest
            break
        budget -= len(text)
        entry: Dict[str, Any] = {"n": getattr(it, "sender_name", "") or "用户", "t": text}
        if refs:
            entry["a"] = refs
            for ref in refs:
                file_index[ref["key"]] = {
                    "name": ref["name"], "kind": ref["kind"],
                    "message_id": mid, "raw": dict(getattr(it, "raw", None) or {}),
                }
        records.append(entry)
        if mid:
            seen_ids.add(mid)
        newest = max(newest, int(getattr(it, "ts_ms", 0) or 0))

    if cold_start:
        # Collected newest→oldest above; the buffer is read as a transcript, so flip it back.
        records.reverse()
    # Advance the cursor even when nothing was recorded: the window was still covered, and
    # not advancing would re-request it on every message forever.
    state.update({
        "cursor_ms": newest,
        "failures": 0,
        # Bounded id memory purely for boundary dedup; the cursor does the real work.
        "seen_ids": list(seen_ids)[-200:],
    })
    await run_in_session(_store_pull, chat_id, state, records, file_index)
    if records:
        logger.info(
            "[channels] 群历史拉取 %d 条 channel_id=%s conv=%s",
            len(records), conn.channel_id, msg.external_conversation_id,
        )
    return len(records)
