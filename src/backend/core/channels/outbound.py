"""Active outbound: does not depend on an inbound trigger, delivers content directly to a channel conversation.

For use by scheduled tasks (automation scheduler) / agent self-scheduling tools — "send a daily report to this group at 9 AM every day".
Reuses the adapter's `push` (text, auto-chunked) / `push_file` (files).

The synthetic message's ``chat_type`` / peer id are recovered from the bound ChatSession metadata
(``channel_chat_type`` / ``channel_peer_id``, written on inbound) — for channels like DingTalk,
group chat and one-on-one go through different bot interfaces, and missing them would pick the wrong channel.

See internal design docs.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Tuple

from core.channels.protocol import InboundMsg
from core.channels.registry import get_adapter
from core.db.engine import SessionLocal
from core.db.repository.channel import ChannelConnectionRepository

logger = logging.getLogger(__name__)


def _synthetic_msg(
    channel_id: str,
    channel_type: str,
    conversation_id: str,
    chat_type: str = "group",
    peer_id: str = "",
) -> InboundMsg:
    """Construct a "target conversation" placeholder message, used only to let the adapter locate the conversation.

    ``external_conversation_id`` is the conversation-locating fallback for each adapter's send
    logic (e.g. lark `_chat_id`); ``chat_type`` / ``sender_id`` are used for routing by channels
    that need to distinguish group vs one-on-one interfaces (e.g. the DingTalk bot API).
    """
    return InboundMsg(
        channel_id=channel_id,
        channel_type=channel_type,
        text="",
        chat_type=chat_type,
        external_conversation_id=conversation_id,
        sender_id=peer_id,
    )


async def deliver_to_conversation(
    channel_id: str,
    conversation_id: str,
    text: Optional[str] = None,
    files: Optional[Iterable[Tuple[bytes, str, str]]] = None,
) -> bool:
    """Actively deliver text/files to the specified channel conversation. Returns whether all succeeded.

    files: an iterable of (content_bytes, filename, mime_type).
    Any step's failure is logged as a warning — delivery failures must be observable, silent drops are not allowed.
    """
    chat_type, peer_id = "group", ""
    with SessionLocal() as db:
        conn = ChannelConnectionRepository(db).get_by_id(channel_id)
        if conn is None or not conn.enabled:
            logger.warning("[channels] 投递目标连接不存在/未启用 channel_id=%s", channel_id)
            return False
        _ = conn.config  # Force-load the credentials column so it stays usable after detach
        db.expunge(conn)
        # Recover the conversation profile (group/one-on-one + p2p peer id)
        try:
            from core.db.models import ChatSession

            sess = (
                db.query(ChatSession)
                .filter(
                    ChatSession.channel_id == channel_id,
                    ChatSession.external_conversation_id == conversation_id,
                    ChatSession.deleted_at.is_(None),
                )
                .first()
            )
            if sess is not None:
                meta = sess.extra_data or {}
                chat_type = meta.get("channel_chat_type") or "group"
                peer_id = meta.get("channel_peer_id") or ""
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 会话画像恢复失败 conv=%s", conversation_id, exc_info=True)

    adapter = get_adapter(conn.channel_type)
    msg = _synthetic_msg(channel_id, conn.channel_type, conversation_id, chat_type, peer_id)
    ok = True
    try:
        if text:
            # [ref:tool-N] citation markers only render on the web frontend, so strip them before
            # outbound; markdown rendering/downgrade is handled inside adapter.push per each
            # channel's capability (e.g. DingTalk sends a markdown message).
            from core.channels.markdown import strip_citation_markers

            r = await adapter.push(conn, msg, strip_citation_markers(text))
            if not r.success:
                logger.warning(
                    "[channels] 主动文本投递失败 channel=%s conv=%s kind=%s detail=%s",
                    channel_id, conversation_id, r.error_kind, r.error_detail,
                )
            ok = ok and r.success
        files_list = list(files or [])
        if files_list and not callable(getattr(adapter, "push_file", None)):
            logger.warning(
                "[channels] 渠道 %s 不支持文件投递，跳过 %d 个文件 channel=%s",
                conn.channel_type, len(files_list), channel_id,
            )
            ok = False
        else:
            for content, name, mime in files_list:
                fr = await adapter.push_file(conn, msg, content, name, mime)
                if not fr.success:
                    logger.warning(
                        "[channels] 主动文件投递失败 channel=%s conv=%s name=%s kind=%s detail=%s",
                        channel_id, conversation_id, name, fr.error_kind, fr.error_detail,
                    )
                ok = ok and fr.success
    except Exception:  # noqa: BLE001
        logger.exception("[channels] 主动投递失败 channel_id=%s conv=%s", channel_id, conversation_id)
        return False
    return ok
