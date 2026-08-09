"""Inbound message orchestration: InboundMsg → reuse the chat pipeline as the owner → send the reply back to the channel.

Owner service-account model:
  - Always runs as the bot's ``owner_user_id`` (no group-member resolution, no team hookup).
  - p2p / group share one path; the only difference is session keying:
      p2p   → (channel_id, sender open_id)    one session per private-chat peer
      group → (channel_id, group chat_id)     the whole group shares one session
  - The resource_scope whitelist (if any) narrows the owner's full capabilities to the specified KBs / skills.
  - Speaker open_id / nickname are recorded for audit only, never mapped to a platform account.

Reuses ``chat_run_executor.start_run`` + ``follow_run`` with zero changes to the orchestration layer.
See internal design docs.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.channels.protocol import InboundMsg
from core.channels.registry import get_adapter
from core.chat.context import resolve_enabled_capabilities
from core.db.engine import SessionLocal
from core.db.models import Artifact, ChannelConnection, ChatSession
from core.db.repository.channel import ChannelConnectionRepository
from core.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Inbound file size cap (same as /v1/file/upload)
_MAX_INBOUND_FILE_BYTES = 50 * 1024 * 1024

# #1 Per-conversation serialization: one asyncio lock per (channel_id, conversation); messages
# arriving while a run is in progress queue up. handle_inbound always runs on the main event loop,
# so asyncio.Lock suffices. Different conversations have their own locks → still concurrent.
_conv_locks: "defaultdict[str, asyncio.Lock]" = defaultdict(asyncio.Lock)


class ChannelRunError(RuntimeError):
    """A user-facing terminal error emitted by the shared chat-run stream."""


def _conv_key(msg: "InboundMsg") -> str:
    return f"{msg.channel_id}:{msg.external_conversation_id}"

# In-process idempotent dedup: channels redeliver events (webhook retries / long-connection
# reconnect replays). Deduplicate by message_id with a bounded LRU (enough to cover short-window
# redelivery; strict cross-process exactly-once is not a goal).
_SEEN_MAX = 4096
_seen_message_ids: "OrderedDict[str, None]" = OrderedDict()


def _already_handled(message_id: str) -> bool:
    if not message_id:
        return False
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = None
    if len(_seen_message_ids) > _SEEN_MAX:
        _seen_message_ids.popitem(last=False)
    return False


# Channel-side "new conversation / clear context" commands. Only triggers when the whole
# message (after trim) exactly equals one of them, so normal sentences like "帮我清空购物车"
# ("clear my shopping cart") are never falsely matched.
_RESET_COMMANDS = frozenset({
    "/new", "/clear", "/reset", "/restart",
    "新对话", "新会话", "清空", "清空上下文", "清除上下文",
    "重置", "重置对话", "重新开始",
})


def _is_reset_command(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in _RESET_COMMANDS


def _reset_session(db, conn: ChannelConnection, msg: InboundMsg) -> None:
    """Soft-delete the current channel session. The next non-command message creates a fresh empty session via _find_or_create_session."""
    existing = (
        db.query(ChatSession)
        .filter(
            ChatSession.channel_id == conn.channel_id,
            ChatSession.external_conversation_id == msg.external_conversation_id,
            ChatSession.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        ChatService(db).delete_session_force(
            existing.chat_id, actor_user_id=conn.owner_user_id
        )


def _find_or_create_session(
    db, conn: ChannelConnection, msg: InboundMsg
) -> ChatSession:
    """Reuse or create a session keyed by (channel_id, external_conversation_id), owned by the owner."""
    existing = (
        db.query(ChatSession)
        .filter(
            ChatSession.channel_id == conn.channel_id,
            ChatSession.external_conversation_id == msg.external_conversation_id,
            # Exclude soft-deleted sessions: the channel-side /new clear works by soft-deleting
            # the current session — without this filter the next message would hit the old
            # session again, making the clear a no-op.
            ChatSession.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        # Backfill the p2p peer id: proactive delivery (automation etc.) needs it to locate
        # the recipient when sending back in a private chat; old sessions predate this field,
        # so we backfill on every inbound message.
        if msg.chat_type == "p2p" and msg.sender_id:
            meta = dict(existing.extra_data or {})
            if meta.get("channel_peer_id") != msg.sender_id:
                meta["channel_peer_id"] = msg.sender_id
                existing.extra_data = meta
                db.commit()
        return existing

    chat_service = ChatService(db)
    title = (msg.text or "渠道会话").strip()[:40] or "渠道会话"
    extra: Dict[str, Any] = {
        "source": f"channel:{conn.channel_type}",
        "channel_chat_type": msg.chat_type,
    }
    if msg.chat_type == "p2p" and msg.sender_id:
        extra["channel_peer_id"] = msg.sender_id
    session = chat_service.create_session(
        user_id=conn.owner_user_id,
        title=title,
        extra_data=extra,
    )
    session.channel_id = conn.channel_id
    session.external_conversation_id = msg.external_conversation_id
    db.commit()
    db.refresh(session)
    return session


def _load_history(db, chat_id: str, owner_id: str) -> List[Dict[str, Any]]:
    """Load history as session_messages — same source as the web path (checkpoint-aware + keeps tool calls/results).

    Goes through ``compaction_service.load_session_history`` (the same entry point as the web
    pipeline), not the old "``list_all_messages`` + keep only user/assistant plain text". The
    old approach **dropped wholesale** the assistant turns' ``tool_calls`` and tool results, as
    well as empty-text pure-tool turns — so in multi-turn channel sessions the model couldn't
    see which tools it called last turn or what results it got. With thinking disabled it would
    very easily redo the same work from scratch, degenerating into "I'll get right on generating…"
    style idling until hitting max_iters. With the same-source loader, the model gets real tool
    context across turns and reuses compaction checkpoints (large sessions no longer replay in full).

    When there is no access permission (theoretically impossible — the session was just created
    by _find_or_create_session and belongs to the owner), load_session_history returns None;
    we fall back to empty history here.
    """
    from core.services.compaction_service import load_session_history

    chat_service = ChatService(db)
    return load_session_history(chat_service, chat_id, owner_id) or []


def _resolve_enabled(
    db, conn: ChannelConnection, owner_id: str
) -> Dict[str, Optional[List[str]]]:
    """Resolve the owner's capabilities, then narrow them by the resource_scope whitelist."""
    skills, agents, mcps = resolve_enabled_capabilities(db, owner_id)
    scope = conn.resource_scope if isinstance(conn.resource_scope, dict) else {}
    scoped_skills = scope.get("skill_ids")
    scoped_kbs = scope.get("kb_ids")
    return {
        # Whitelist present → narrow to it (the owner must still own these; catalog gating applies as usual)
        "enabled_skills": scoped_skills if isinstance(scoped_skills, list) else skills,
        "enabled_agents": agents,
        "enabled_mcps": mcps,
        "enabled_kbs": scoped_kbs if isinstance(scoped_kbs, list) else None,
    }


async def _ingest_attachments(
    db, adapter, conn: ChannelConnection, owner_id: str, chat_id: str, msg: InboundMsg
) -> List[Dict[str, Any]]:
    """Download inbound attachments → store as Artifacts (mirrors /v1/file/upload) → return uploaded_files items.

    Each item contains metadata only. Parsing is deferred to the shared
    file-context/read_artifact path and cached on first use.
    """
    download = getattr(adapter, "download_resource", None)
    if download is None or not msg.attachments:
        return []
    from core.services.artifact_service import store_bytes_as_artifact

    out: List[Dict[str, Any]] = []
    for att in msg.attachments:
        try:
            content = await download(conn, msg, att)
            if not content or len(content) > _MAX_INBOUND_FILE_BYTES:
                continue
            name = att.get("name") or "file.bin"
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            art = store_bytes_as_artifact(
                db, user_id=owner_id, content=content, filename=name, mime_type=mime,
                chat_id=chat_id, source="channel_upload",
                extra={"channel_id": conn.channel_id},
            )
            out.append({
                "file_id": art.artifact_id,
                "name": name,
                "mime_type": mime,
            })
        except Exception:  # noqa: BLE001
            logger.exception("[channels] 入站附件处理失败 key=%s", att.get("key"))
    return out


async def _collect_reply(run_id: str):
    """Follow the run's event stream, accumulating assistant text + capturing artifact files generated this turn (meta.artifacts).

    Structured-reasoning models emit thinking as separate ``thinking`` events (ignored
    here), but inline-thinking models embed ``<think>…</think>`` in the content deltas
    themselves — strip those before handing the text to the channel, or the raw chain
    of thought goes out as message text (the web UI parses the tags; IM cannot).
    """
    from core.channels.markdown import strip_inline_thinking
    from orchestration import chat_run_executor

    full = ""
    artifacts: List[Dict[str, Any]] = []
    async for event in chat_run_executor.follow_run(run_id):
        et = event.get("type")
        if et == "content":
            full += event.get("delta", "") or ""
        elif et == "meta":
            arts = event.get("artifacts")
            if isinstance(arts, list):
                artifacts = arts
        elif et == "error":
            detail = str(event.get("error") or event.get("delta") or "").strip()
            raise ChannelRunError(detail or "请求处理失败，请稍后重试")
    return strip_inline_thinking(full).strip(), artifacts


def _load_generated_files(artifacts: List[Dict[str, Any]]):
    """Read the bytes of artifact files generated this turn; returns a list of (content, name, mime) for pushing back (best-effort)."""
    if not artifacts:
        return []
    from core.storage import get_storage

    files = []
    with SessionLocal() as db:
        storage = get_storage()
        for art in artifacts:
            fid = art.get("file_id")
            if not fid:
                continue
            row = db.query(Artifact).filter(Artifact.artifact_id == fid).first()
            if row is None or not row.storage_key:
                continue
            try:
                content = storage.download_bytes(row.storage_key)
            except Exception:  # noqa: BLE001
                logger.warning("[channels] 产物下载失败 %s", fid, exc_info=True)
                continue
            files.append((content, row.filename or art.get("name") or fid,
                          row.mime_type or "application/octet-stream"))
    return files


def _speaker_label(msg: InboundMsg) -> str:
    """Label the speaker in group scenarios (no directory lookup; falls back to existing fields)."""
    return (msg.sender_name or (msg.sender_id or "")[-6:] or "用户")


# ── Group listening (observe_all): bystander messages as background context ──────────────
# Cap on the buffered bystander messages carried into the next @bot turn. A busy group can
# produce hundreds of messages between two @s; without a cap the buffer would blow up both the
# JSON column and the prompt. Oldest entries are evicted first — recent context is what matters.
_OBSERVE_MAX = 60
# Per-message truncation. Long pasted blocks are background material, not the task.
_OBSERVE_TEXT_MAX = 500
# Cap on the lazy-fetch file index. Larger than _OBSERVE_MAX because a file stays worth
# opening long after the chatter around it scrolled out of the message buffer.
_OBSERVE_FILE_INDEX_MAX = 100

# ── Deterministic history pull budget ────────────────────────────────────────────────────
# Group history is pulled by code on every @, never by the model deciding to call a tool:
# a capability that only works when the model remembers to use it is not a capability.
# Every pull is incremental — bounded below by the stored cursor — so the same messages are
# not fetched twice; the window below only applies to the very first pull in a conversation,
# where there is no cursor yet.
_HISTORY_COLDSTART_WINDOW_H = 168   # how far back the first pull reaches (7 days)
_HISTORY_MAX_MESSAGES = 80          # hard cap per pull
_HISTORY_MAX_TOTAL_CHARS = 12000    # total text budget per pull, so one chatty group can't flood the prompt
_HISTORY_TIMEOUT_S = 10             # a slow platform must never hold up the reply
# Consecutive failures after which we stop retrying for a conversation. Without this, a
# permanently unavailable API would add its timeout to every single @ forever.
_HISTORY_MAX_FAILURES = 3


async def _resolve_addressed(adapter, conn: ChannelConnection, msg: InboundMsg) -> bool:
    """Is this message aimed at the bot? Adapters that cannot decide synchronously defer here.

    Fails open (True) on any resolver error or when the adapter has no resolver at all, so
    channels that never learned about group listening (WeCom / WeChat) keep behaving exactly
    as before.
    """
    if msg.addressed_to_bot is not None:
        return bool(msg.addressed_to_bot)
    resolver = getattr(adapter, "resolve_addressed", None)
    if resolver is None:
        return True
    try:
        return bool(await resolver(conn, msg))
    except Exception:  # noqa: BLE001
        logger.debug("[channels] @ 判定失败，按「已被 @」处理 channel_id=%s", conn.channel_id, exc_info=True)
        return True


def _observe_text(msg: InboundMsg) -> str:
    """The bystander message's own text, truncated. Attachments are handled separately."""
    text = (msg.text or "").strip()
    if len(text) > _OBSERVE_TEXT_MAX:
        text = text[:_OBSERVE_TEXT_MAX] + "…"
    return text


def _observe_attachment_refs(msg: InboundMsg) -> List[Dict[str, Any]]:
    """Attachment handles for a bystander message — recorded, never downloaded.

    Eagerly downloading everything a group shares would pull the whole group's file traffic
    into our storage: large I/O and a far wider privacy footprint than reading text. But
    *discarding the key* (the earlier behavior) left the agent able to see that a file existed
    while being unable to ever open it. Keeping the key costs nothing and turns this into
    lazy loading: the model asks for a file by key via ``channel_read_attachment`` only when
    it decides the file matters.
    """
    return [
        {"kind": a.get("kind") or "file", "key": a.get("key") or "", "name": a.get("name") or ""}
        for a in (msg.attachments or [])
        if a.get("key")
    ]


def _observe_message(db, conn: ChannelConnection, msg: InboundMsg) -> None:
    """Record a bystander group message into the session's observation buffer. No agent run, no reply.

    The buffer lives in ``ChatSession.extra_data`` rather than being written as real session
    messages, for three reasons: it stays bounded (a quiet group can't inflate history), it
    avoids long runs of consecutive user-role turns in the model's message list, and draining
    it into the triggering turn keeps the group log in one coherent block instead of scattered
    fragments.
    """
    text = _observe_text(msg)
    refs = _observe_attachment_refs(msg)
    if not text and not refs:
        return
    session = _find_or_create_session(db, conn, msg)
    meta = dict(session.extra_data or {})

    entry: Dict[str, Any] = {"n": _speaker_label(msg), "t": text}
    if refs:
        entry["a"] = refs
    buf = list(meta.get("observed") or [])
    buf.append(entry)
    meta["observed"] = buf[-_OBSERVE_MAX:]

    if refs:
        # Separate, longer-lived index than the message buffer: the buffer is drained into a
        # prompt on the next @, but the model may only decide to open a file several turns
        # later. Keyed by attachment key, it stores what the adapter needs to fetch the bytes
        # (Lark resolves resources by message_id, DingTalk by the robotCode in raw) so none of
        # that plumbing has to appear in the prompt.
        index = dict(meta.get("observed_files") or {})
        for ref in refs:
            index.pop(ref["key"], None)  # re-insert so recency ordering stays correct
            index[ref["key"]] = {
                "name": ref["name"], "kind": ref["kind"],
                "message_id": msg.message_id, "raw": dict(msg.raw or {}),
            }
        while len(index) > _OBSERVE_FILE_INDEX_MAX:
            index.pop(next(iter(index)))
        meta["observed_files"] = index

    session.extra_data = meta  # reassign: in-place JSON mutation is not change-tracked
    db.commit()


def _record_observed_batch(
    db, session: ChatSession, items: List[Dict[str, Any]], file_index: Dict[str, Any]
) -> None:
    """Append a pulled batch to the observation buffer + file index, respecting the caps."""
    meta = dict(session.extra_data or {})
    buf = list(meta.get("observed") or [])
    buf.extend(items)
    meta["observed"] = buf[-_OBSERVE_MAX:]
    if file_index:
        index = dict(meta.get("observed_files") or {})
        for key, entry in file_index.items():
            index.pop(key, None)
            index[key] = entry
        while len(index) > _OBSERVE_FILE_INDEX_MAX:
            index.pop(next(iter(index)))
        meta["observed_files"] = index
    session.extra_data = meta
    db.commit()


def _history_state(session: ChatSession) -> Dict[str, Any]:
    state = (session.extra_data or {}).get("history_pull")
    return dict(state) if isinstance(state, dict) else {}


def _save_history_state(db, session: ChatSession, state: Dict[str, Any]) -> None:
    meta = dict(session.extra_data or {})
    meta["history_pull"] = state
    session.extra_data = meta
    db.commit()


async def _pull_history(
    db, adapter, conn: ChannelConnection, msg: InboundMsg, session: ChatSession
) -> int:
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
    state = _history_state(session)
    if int(state.get("failures") or 0) >= _HISTORY_MAX_FAILURES:
        return 0

    cursor = int(state.get("cursor_ms") or 0)
    cold_start = not cursor
    if cold_start:
        # Timezone-aware on purpose: this value is compared against platform epoch-ms, and
        # the naive-utcnow habit elsewhere in this codebase is exactly what makes stored
        # timestamps read 8 hours off.
        cursor = int(
            (datetime.now(timezone.utc) - timedelta(hours=_HISTORY_COLDSTART_WINDOW_H)).timestamp() * 1000
        )
    seen_ids = set(state.get("seen_ids") or [])

    try:
        items = await asyncio.wait_for(
            fetch(
                conn, msg.external_conversation_id,
                since_ms=cursor, limit=_HISTORY_MAX_MESSAGES,
                # Cold start takes the newest end of the window: with a 7-day window and a
                # per-pull cap, taking the oldest would surface week-old chatter and leave
                # the recent messages — what the conversation is actually about — unread.
                newest_first=cold_start,
            ),
            timeout=_HISTORY_TIMEOUT_S,
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
        _save_history_state(db, session, state)
        return 0

    records: List[Dict[str, Any]] = []
    file_index: Dict[str, Any] = {}
    budget = _HISTORY_MAX_TOTAL_CHARS
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
        if mid and (mid in seen_ids or _already_handled(f"hist:{mid}")):
            continue
        text = (getattr(it, "text", "") or "").strip()
        if len(text) > _OBSERVE_TEXT_MAX:
            text = text[:_OBSERVE_TEXT_MAX] + "…"
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
    if records:
        _record_observed_batch(db, session, records, file_index)
    # Advance the cursor even when nothing was recorded: the window was still covered, and
    # not advancing would re-request it on every message forever.
    state.update({
        "cursor_ms": newest,
        "failures": 0,
        # Bounded id memory purely for boundary dedup; the cursor does the real work.
        "seen_ids": list(seen_ids)[-200:],
    })
    _save_history_state(db, session, state)
    if records:
        logger.info(
            "[channels] 群历史拉取 %d 条 channel_id=%s conv=%s",
            len(records), conn.channel_id, msg.external_conversation_id,
        )
    return len(records)


def _drain_observed(db, session: ChatSession) -> List[Dict[str, Any]]:
    """Take and clear the observation buffer (it is folded into the turn being started)."""
    meta = dict(session.extra_data or {})
    buf = list(meta.get("observed") or [])
    if not buf:
        return []
    meta["observed"] = []
    session.extra_data = meta
    db.commit()
    return buf


def _render_observed_line(item: Dict[str, Any]) -> str:
    """One group-log line: ``speaker：text [文件：name｜key=…]``.

    Attachment keys are rendered inline so the model can pass one straight to
    ``channel_read_attachment``. The files themselves were never downloaded — the key is the
    only handle that exists, which is exactly why it has to reach the prompt.
    """
    parts = [item.get("t") or ""]
    for ref in item.get("a") or []:
        label = "图片" if ref.get("kind") == "image" else "文件"
        parts.append(f"[{label}：{ref.get('name') or '未命名'}｜key={ref.get('key')}]")
    return f"{item.get('n', '用户')}：{' '.join(p for p in parts if p)}"


def _render_observed_block(items: List[Dict[str, Any]]) -> str:
    """Render the buffer as a context preamble for the triggering message.

    Explicitly labelled as background and marked do-not-reply, otherwise the model treats the
    group log as a list of pending requests and tries to answer every line.
    """
    lines = "\n".join(_render_observed_line(it) for it in items)
    hint = (
        "\n（其中带 key= 的文件并未下载，只有句柄。确有需要时用 "
        "channel_read_attachment(file_key=\"…\") 取回再读，不要凭文件名臆测内容。）"
        if any(it.get("a") for it in items) else ""
    )
    return (
        "【群聊上下文】以下是你被 @ 之前群里的近期对话，仅供你理解背景，"
        "不要逐条回复，也不要在回复中复述：\n"
        f"{lines}{hint}\n"
        "【以下才是本次需要你处理的消息】\n"
    )


async def _recall_placeholder(adapter, conn, msg: InboundMsg, placeholder_id: str) -> None:
    """Recall the placeholder message (best-effort): channels that can't edit but can recall (DingTalk) use "recall + resend" as an equivalent replacement."""
    recall = getattr(adapter, "recall_message", None)
    if recall is None:
        return
    try:
        r = await recall(conn, msg, placeholder_id)
        if not r.success:
            logger.debug("[channels] 占位撤回失败 kind=%s detail=%s", r.error_kind, r.error_detail)
    except Exception:  # noqa: BLE001
        logger.debug("[channels] 占位撤回异常", exc_info=True)


async def _replace_placeholder(
    adapter, conn, msg: InboundMsg, placeholder_id: Optional[str], text: str
) -> None:
    """Finish a placeholder flow with text, always sending a terminal message.

    Edit in place when the channel returned a placeholder ID; otherwise recall
    (if supported) and resend. WeChat's send API does not return a message ID, so
    it must take the direct-send path instead of silently leaving the placeholder
    as the last visible message.

    Error notices / "no text reply" receipts go through here — the old logic only edited,
    and since DingTalk edits always fail, users were stuck on "processing" forever with
    no follow-up ever shown."""
    edit = getattr(adapter, "edit_message", None)
    if placeholder_id and edit is not None:
        r = await edit(conn, placeholder_id, text)
        if r.success:
            return
    if placeholder_id:
        await _recall_placeholder(adapter, conn, msg, placeholder_id)
    fr = await adapter.send_text(conn, msg, text)
    if not fr.success:
        logger.warning("[channels] 占位替换消息发送失败 kind=%s detail=%s", fr.error_kind, fr.error_detail)


async def _deliver_reply(adapter, conn, msg: InboundMsg, reply: str, placeholder_id: Optional[str]) -> None:
    """#2+#4: chunk long replies; if there is a placeholder message, edit the first chunk into it and send the rest as follow-up messages.
    Channel supports recall but not edit (DingTalk) → recall the placeholder then send, a visually equivalent replacement.

    When the channel supports markdown (caps.supports_markdown + send_markdown), send with native rendering;
    run prepare_markdown on the whole text before chunking (tables etc. can no longer be recognized and converted once cut by chunk boundaries).
    [ref:tool-N] citation markers only render on the web client, so they are always stripped before going out (all channels)."""
    from core.channels.markdown import strip_citation_markers
    from core.channels.protocol import chunk_text

    reply = strip_citation_markers(reply)
    send_md = (
        getattr(adapter, "send_markdown", None)
        if getattr(adapter.caps, "supports_markdown", False)
        else None
    )
    if send_md is not None:
        prepare = getattr(adapter, "prepare_markdown", None)
        if callable(prepare):
            reply = prepare(reply)
    chunks = chunk_text(reply, getattr(adapter.caps, "max_message_len", 0)) or [reply]
    edit = getattr(adapter, "edit_message", None)
    rest = chunks
    if placeholder_id and edit is not None:
        r = await edit(conn, placeholder_id, chunks[0])
        if r.success:
            rest = chunks[1:]
        else:
            # edit failed/unsupported → recall the placeholder (if supported), send all chunks as new messages
            await _recall_placeholder(adapter, conn, msg, placeholder_id)
    for c in rest:
        fr = await (send_md or adapter.send_text)(conn, msg, c)
        if not fr.success:
            logger.warning("[channels] 回复分段推送失败 kind=%s detail=%s", fr.error_kind, fr.error_detail)


async def handle_inbound(msg: InboundMsg) -> None:
    """Main entry point for inbound messages. Scheduled on the main event loop (long-connection threads submit via run_coroutine_threadsafe).

    All exceptions are swallowed and logged — one message's failure must not take down the long connection / process.
    """
    if _already_handled(msg.message_id):
        return
    if not (msg.text or "").strip() and not msg.attachments:
        return
    # #1 Per-conversation serialization: until an in-progress run finishes, later messages in the same conversation queue up, avoiding history races/corruption.
    async with _conv_locks[_conv_key(msg)]:
        await _process_inbound(msg)


async def _process_inbound(msg: InboundMsg) -> None:
    from core.chat.context import build_runtime_context, collect_historical_attachments
    from orchestration import chat_run_executor

    placeholder_id: Optional[str] = None
    db = SessionLocal()
    try:
        repo = ChannelConnectionRepository(db)
        conn = repo.get_by_id(msg.channel_id)
        if conn is None or not conn.enabled:
            logger.warning("[channels] inbound 无对应启用连接 channel_id=%s", msg.channel_id)
            db.close()
            return

        owner_id = conn.owner_user_id
        adapter = get_adapter(conn.channel_type)
        _ = conn.config  # force-load the credential column so it remains usable after detaching

        # Group listening gate. Only reached at all when the platform actually delivers non-@
        # group messages (needs the channel app's "read all group messages" permission);
        # otherwise every inbound group message is an @ and this is a no-op.
        if msg.chat_type == "group" and not await _resolve_addressed(adapter, conn, msg):
            if conn.group_listen_mode == "observe_all":
                _observe_message(db, conn, msg)
                repo.touch_event(conn.channel_id)  # bystander traffic still proves the connection is alive
                logger.info(
                    "[channels] 旁听群消息 channel_id=%s type=%s conv=%s",
                    conn.channel_id, conn.channel_type, msg.external_conversation_id,
                )
            else:
                # mention_only → drop entirely, exactly as before this feature existed.
                # Logged because reaching this line at all is the *only* evidence that the
                # platform is delivering non-@ group messages (i.e. that the "read all group
                # messages" permission is actually granted). Without it, "the bot ignores the
                # group" and "the platform never sends the group" look identical from outside.
                # DEBUG, not INFO: an active group would otherwise flood the log.
                logger.debug(
                    "[channels] 丢弃未 @ 的群消息（旁听未开启）channel_id=%s type=%s",
                    conn.channel_id, conn.channel_type,
                )
            # Either way: no reset-command handling, no run, no placeholder, no reply.
            db.close()
            return

        # Channel-side "new conversation / clear context": soft-deleting the current session is
        # enough (the next message automatically creates an empty session); no agent run. This is
        # where /new and clear-context actually take effect in Feishu and other clients —
        # otherwise the command would be fed to the agent as plain text and the history would keep
        # being reused, impossible to clear.
        if _is_reset_command(msg.text):
            _reset_session(db, conn, msg)
            db.refresh(conn)
            db.expunge(conn)
            db.close()
            try:
                await adapter.send_text(conn, msg, "✅ 已开启新对话，之前的上下文已清除。")
            except Exception:  # noqa: BLE001
                logger.debug("[channels] 清空回执发送失败", exc_info=True)
            return

        session = _find_or_create_session(db, conn, msg)
        chat_id = session.chat_id

        # #4 Placeholder first. Everything below this line does real network work —
        # downloading attachments, pulling group history — and the placeholder exists
        # precisely so the user is not staring at nothing while that happens. It used to be
        # sent after the whole prep stage, which put ~1s of history pull ahead of any visible
        # feedback and read as the bot being slow to react.
        # Prefer the channel's send_placeholder (DingTalk: robot API, recallable) — only that
        # path yields a message_id supporting the later edit/recall-replace; otherwise plain
        # send_text.
        try:
            send_ph = getattr(adapter, "send_placeholder", None) or adapter.send_text
            ph = await send_ph(conn, msg, "🤔 正在处理，请稍候…")
            if ph.success:
                placeholder_id = ph.message_id
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 占位消息发送失败", exc_info=True)

        # Inbound attachments: download → store as Artifact → uploaded_files
        uploaded_files = await _ingest_attachments(db, adapter, conn, owner_id, chat_id, msg)
        # File-only message with no text → synthesize a one-line user prompt
        if (msg.text or "").strip():
            user_text = msg.text
        elif uploaded_files:
            user_text = "[收到文件] " + "、".join(f.get("name", "") for f in uploaded_files)
        else:
            db.close()
            return
        # #5 Label the speaker in group scenarios so the agent can tell multi-person conversations apart
        if msg.chat_type == "group":
            user_text = f"{_speaker_label(msg)}：{user_text}"
            # Deterministic incremental pull, before draining: whatever the platform pushed
            # (possibly nothing) plus whatever we can pull ends up in the same buffer, and the
            # drain below sees both. Failure is absorbed inside — it must not block the reply.
            await _pull_history(db, adapter, conn, msg, session)
            # observe_all: prepend everything the group said since the last @ as background.
            # Prepending into user_text (rather than a separate context field) means it is
            # persisted with the turn, so later turns still see the group log through normal
            # history loading and compaction — no second retention mechanism to maintain.
            observed = _drain_observed(db, session)
            if observed:
                user_text = _render_observed_block(observed) + user_text

        session_messages = _load_history(db, chat_id, owner_id)
        session_messages.append({"role": "user", "content": user_text})
        ChatService(db).add_message(
            chat_id=chat_id, role="user", content=user_text,
            extra_data={
                "channel_sender_open_id": msg.sender_id,
                "channel_sender_name": msg.sender_name,
                "channel_message_id": msg.message_id,
                "channel_attachments": [f.get("file_id") for f in uploaded_files],
                # Same shape as the web client: lets the cross-turn historical-file scanner
                # (collect_historical_attachments → _extract_message_file_ids only recognizes
                # "attachments"/"artifacts") re-inject these files' real file_ids to the model in
                # later turns; otherwise the model fabricates ids when reading files across turns.
                "attachments": [
                    {"file_id": f.get("file_id"), "name": f.get("name")}
                    for f in uploaded_files
                ],
            },
        )
        enabled = _resolve_enabled(db, conn, owner_id)
        # Enable thinking: same as the web client's default in non-fast mode. With thinking off,
        # models (Qwen family especially) tend to emit shallow filler like "I'll get right on X"
        # without actually landing on tool calls, idling repeatedly across turns. Thinking never
        # leaks into the channel reply: structured-reasoning models emit separate thinking events
        # (ignored by _collect_reply, which only accumulates content/meta), and inline-thinking
        # models' <think>…</think> spans inside content are stripped by _collect_reply.
        context = build_runtime_context(
            model_name=None, user_id=owner_id, chat_id=chat_id, enable_thinking=True,
            uploaded_files=uploaded_files,
            enabled_skills=enabled["enabled_skills"], enabled_agents=enabled["enabled_agents"],
            enabled_mcps=enabled["enabled_mcps"], enabled_kbs=enabled["enabled_kbs"],
        )
        from core.services.ontology_service import build_user_ontology_runtime

        ontology_enabled, ontology_runtime = build_user_ontology_runtime(
            user_id=owner_id,
            task=user_text,
            db=db,
        )
        context["ontology_enabled"] = ontology_enabled
        context["ontology_runtime"] = ontology_runtime
        # Cross-turn file readability: inject files uploaded/generated earlier in this session
        # (including last turn's Feishu attachments) as a summary block so the model gets real
        # file_ids to call read_artifact with. Exclude this turn's attachments (already injected
        # in full via uploaded_files) to avoid duplication. build_runtime_context doesn't include
        # this field, so we add it here (aligned with the web client's historical_files injection
        # in chats.py).
        current_file_ids = {f.get("file_id") for f in uploaded_files if f.get("file_id")}
        context["historical_files"] = collect_historical_attachments(
            chat_id, owner_id, exclude_file_ids=current_file_ids,
        )
        # Bind to a specific sub-agent: pin the whole run to that sub-agent (running with its own
        # prompt/tools/model/knowledge bases), via the workflow's direct sub-agent mode. NULL →
        # unset, run with the owner's default capabilities (main agent).
        # Note: the sub-agent carries its own capability bindings, which override the whitelist
        # narrowing from _resolve_enabled above — this is intended behavior.
        if conn.agent_id:
            context["agent_id"] = conn.agent_id
        # #7 Trigger A: let the agent self-create scheduled delivery tasks within this conversation
        context["channel_origin"] = {
            "channel_id": conn.channel_id,
            "conversation_id": msg.external_conversation_id,
            "chat_type": msg.chat_type,
            # channel_type lets the agent be told *which* platform it is in, so it can reach
            # for that platform's CLI to pull group history the bot never received (anything
            # from before it joined). Delivery targets never needed it; the prompt does.
            "channel_type": conn.channel_type,
        }
        repo.touch_event(conn.channel_id)
        # The commits in create_session / add_message / touch_event expire conn's column
        # attributes (expire_on_commit defaults to True). If we expunged as-is, reading app_id/
        # config during push after detaching would trigger a refresh and raise
        # DetachedInstanceError ("is not bound to a Session").
        # Refresh first to reload all columns, then expunge — afterwards attribute reads are
        # pure in-memory and no session is needed.
        db.refresh(conn)
        db.expunge(conn)  # app_id/config remain readable after leaving the session, for push
    except Exception:
        logger.exception("[channels] inbound 准备阶段失败 channel_id=%s", msg.channel_id)
        db.close()
        return
    finally:
        db.close()

    # (The placeholder is sent inside the prep stage above, before attachment download and
    # history pull — those are the slow parts it needs to cover.)

    # §13 Channel adaptation: IM clients like Feishu have no approval UI for MySpace write
    # operations. Without pre-authorization, the gate would suspend on a MySpace write waiting
    # for out-of-band user confirmation (_collect_reply only recognizes content/meta;
    # file_confirm is dropped) → stuck until the 2h timeout with the placeholder never updated.
    # The bot runs as the owner, and in private chat the message sender is the owner themselves,
    # so pre-mark this session as allowed — equivalent to the owner approving their own write.
    # (Sub-agents remain non-interactive; their /myspace writes are still rejected.)
    try:
        from core.llm.tools import _myspace_confirm as _mc
        _mc.allow_session(chat_id)
    except Exception:  # noqa: BLE001 — a pre-authorization failure must not take down the whole run
        logger.debug("[channels] myspace 写预授权失败 chat_id=%s", chat_id, exc_info=True)

    try:
        run = await chat_run_executor.start_run(
            chat_id=chat_id, user_id=owner_id, session_messages=session_messages,
            effective_user_message=user_text, raw_user_message=user_text, context=context,
            request_payload={"channel_id": msg.channel_id, "source": "channel"}, model_name=None,
        )
        reply, gen_artifacts = await _collect_reply(run.run_id)
    except Exception as exc:
        logger.exception("[channels] inbound run 失败 channel_id=%s", msg.channel_id)
        notice = (
            str(exc).strip()
            if isinstance(exc, ChannelRunError) and str(exc).strip()
            else "处理出错了，请稍后重试。"
        )
        try:
            await _replace_placeholder(adapter, conn, msg, placeholder_id, f"⚠️ {notice}")
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 失败消息回推异常", exc_info=True)
        return

    # Note: the assistant reply is **not persisted here**. start_run's background executor
    # (chat_run_executor) already persists the assistant message together with
    # usage/model/tool_calls/artifacts when the stream ends. Saving another one here would create
    # a **duplicate assistant message**, and one without usage (tokens recorded as 0) — exactly
    # the root cause of "input/output tokens are both 0" in channel sessions. We only use the
    # reply text to push back to the channel, without persisting again.

    # Push back: text (placeholder edit + follow-up chunks) + generated files
    try:
        if reply:
            await _deliver_reply(adapter, conn, msg, reply, placeholder_id)
        else:
            note = "已为你生成文件。" if gen_artifacts else "（本次无文本回复）"
            await _replace_placeholder(adapter, conn, msg, placeholder_id, note)
        if getattr(adapter, "push_file", None):
            for content, name, mime in _load_generated_files(gen_artifacts):
                fr = await adapter.push_file(conn, msg, content, name, mime)
                if not fr.success:
                    logger.warning("[channels] 文件回传失败 name=%s kind=%s", name, fr.error_kind)
    except Exception:
        logger.exception("[channels] inbound 回推阶段失败 channel_id=%s", msg.channel_id)
