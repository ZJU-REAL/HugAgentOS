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

    Each item's shape matches chats.py uploaded_files: {file_id, name, content, mime_type, download_url}.
    content is the server-side parsed text (best-effort; left empty on failure, the agent can fall back to read_artifact).
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
            # parse_file involves blocking I/O (external parsing service / LibreOffice) → offload to the thread pool, don't block the event loop
            parsed = await asyncio.to_thread(_safe_parse_file, content, name)
            art = store_bytes_as_artifact(
                db, user_id=owner_id, content=content, filename=name, mime_type=mime,
                chat_id=chat_id, source="channel_upload", parsed_text=parsed,
                extra={"channel_id": conn.channel_id},
            )
            out.append({
                "file_id": art.artifact_id, "name": name, "content": parsed,
                "mime_type": mime, "download_url": f"/files/{art.artifact_id}",
            })
        except Exception:  # noqa: BLE001
            logger.exception("[channels] 入站附件处理失败 key=%s", att.get("key"))
    return out


def _safe_parse_file(content: bytes, name: str) -> str:
    """Synchronous parsing (run in the thread pool); returns an empty string on failure, the agent can fall back to read_artifact."""
    from core.content.file_parser import parse_file
    try:
        return parse_file(content, name) or ""
    except Exception:  # noqa: BLE001
        return ""


async def _collect_reply(run_id: str):
    """Follow the run's event stream, accumulating assistant text + capturing artifact files generated this turn (meta.artifacts)."""
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
    return full.strip(), artifacts


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


async def _replace_placeholder(adapter, conn, msg: InboundMsg, placeholder_id: str, text: str) -> None:
    """Replace the placeholder message with text: edit in place if editable; otherwise recall (if supported) + resend.

    Error notices / "no text reply" receipts go through here — the old logic only edited,
    and since DingTalk edits always fail, users were stuck on "processing" forever with
    no follow-up ever shown."""
    edit = getattr(adapter, "edit_message", None)
    if edit is not None:
        r = await edit(conn, placeholder_id, text)
        if r.success:
            return
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
        # without actually landing on tool calls, idling repeatedly across turns. Thinking events
        # are ignored by _collect_reply (which only accumulates content/meta), so they never leak
        # into the channel reply.
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

    # #4 Immediately reply with a placeholder message so users aren't left waiting while the
    # agent runs for 5–20s. Prefer the channel's send_placeholder if present (DingTalk: sent
    # via the bot API, recallable) — only the message_id obtained that way supports the later
    # "edit / recall-replace"; otherwise fall back to plain send_text.
    try:
        send_ph = getattr(adapter, "send_placeholder", None) or adapter.send_text
        ph = await send_ph(conn, msg, "🤔 正在处理，请稍候…")
        if ph.success:
            placeholder_id = ph.message_id
    except Exception:  # noqa: BLE001
        logger.debug("[channels] 占位消息发送失败", exc_info=True)

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
    except Exception:
        logger.exception("[channels] inbound run 失败 channel_id=%s", msg.channel_id)
        if placeholder_id:
            try:
                await _replace_placeholder(adapter, conn, msg, placeholder_id, "⚠️ 处理出错了，请稍后重试。")
            except Exception:  # noqa: BLE001
                pass
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
        elif placeholder_id:
            note = "已为你生成文件。" if gen_artifacts else "（本次无文本回复）"
            await _replace_placeholder(adapter, conn, msg, placeholder_id, note)
        if getattr(adapter, "push_file", None):
            for content, name, mime in _load_generated_files(gen_artifacts):
                fr = await adapter.push_file(conn, msg, content, name, mime)
                if not fr.success:
                    logger.warning("[channels] 文件回传失败 name=%s kind=%s", name, fr.error_kind)
    except Exception:
        logger.exception("[channels] inbound 回推阶段失败 channel_id=%s", msg.channel_id)
