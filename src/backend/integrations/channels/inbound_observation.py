"""Channel observation responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List
from integrations.channels.protocol import InboundMsg
from core.db.models import ChatSession
from integrations.channels.models import ChannelConnection
import logging
logger = logging.getLogger(__name__)
from integrations.channels import inbound_sessions
from integrations.channels import inbound_state

def speaker_label(msg: InboundMsg) -> str:
    """Label the speaker in group scenarios (no directory lookup; falls back to existing fields)."""
    return (msg.sender_name or (msg.sender_id or "")[-6:] or "用户")


async def resolve_addressed(adapter, conn: ChannelConnection, msg: InboundMsg) -> bool:
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


def observe_text(msg: InboundMsg) -> str:
    """The bystander message's own text, truncated. Attachments are handled separately."""
    text = (msg.text or "").strip()
    if len(text) > inbound_state.OBSERVE_TEXT_MAX:
        text = text[:inbound_state.OBSERVE_TEXT_MAX] + "…"
    return text


def observe_attachment_refs(msg: InboundMsg) -> List[Dict[str, Any]]:
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


def observe_message(db, conn: ChannelConnection, msg: InboundMsg) -> None:
    """Record a bystander group message into the session's observation buffer. No agent run, no reply.

    The buffer lives in ``ChatSession.extra_data`` rather than being written as real session
    messages, for three reasons: it stays bounded (a quiet group can't inflate history), it
    avoids long runs of consecutive user-role turns in the model's message list, and draining
    it into the triggering turn keeps the group log in one coherent block instead of scattered
    fragments.
    """
    text = observe_text(msg)
    refs = observe_attachment_refs(msg)
    if not text and not refs:
        return
    session = inbound_sessions.find_or_create_session(db, conn, msg)
    meta = dict(session.extra_data or {})

    entry: Dict[str, Any] = {"n": speaker_label(msg), "t": text}
    if refs:
        entry["a"] = refs
    buf = list(meta.get("observed") or [])
    buf.append(entry)
    meta["observed"] = buf[-inbound_state.OBSERVE_MAX:]

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
        while len(index) > inbound_state.OBSERVE_FILE_INDEX_MAX:
            index.pop(next(iter(index)))
        meta["observed_files"] = index

    session.extra_data = meta  # reassign: in-place JSON mutation is not change-tracked
    db.commit()


def record_observed_batch(
    db, session: ChatSession, items: List[Dict[str, Any]], file_index: Dict[str, Any]
) -> None:
    """Append a pulled batch to the observation buffer + file index, respecting the caps."""
    meta = dict(session.extra_data or {})
    buf = list(meta.get("observed") or [])
    buf.extend(items)
    meta["observed"] = buf[-inbound_state.OBSERVE_MAX:]
    if file_index:
        index = dict(meta.get("observed_files") or {})
        for key, entry in file_index.items():
            index.pop(key, None)
            index[key] = entry
        while len(index) > inbound_state.OBSERVE_FILE_INDEX_MAX:
            index.pop(next(iter(index)))
        meta["observed_files"] = index
    session.extra_data = meta
    db.commit()


def drain_observed(db, session: ChatSession) -> List[Dict[str, Any]]:
    """Take and clear the observation buffer (it is folded into the turn being started)."""
    meta = dict(session.extra_data or {})
    buf = list(meta.get("observed") or [])
    if not buf:
        return []
    meta["observed"] = []
    session.extra_data = meta
    db.commit()
    return buf


def render_observed_line(item: Dict[str, Any]) -> str:
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


def render_observed_block(items: List[Dict[str, Any]]) -> str:
    """Render the buffer as a context preamble for the triggering message.

    Explicitly labelled as background and marked do-not-reply, otherwise the model treats the
    group log as a list of pending requests and tries to answer every line.
    """
    lines = "\n".join(render_observed_line(it) for it in items)
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
