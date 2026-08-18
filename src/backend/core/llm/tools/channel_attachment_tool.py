"""channel_read_attachment tool — lazily fetch a file seen in group listening.

Group listening (``core/channels/inbound.py``) records bystander messages as background
context but deliberately does **not** download their attachments: eagerly pulling every file
a group shares would mean large I/O and a much wider privacy footprint than reading text.
What it does keep is the attachment **key** plus whatever the adapter needs to resolve it,
in ``ChatSession.extra_data['observed_files']``.

This tool closes that loop: the model sees ``[文件：Q3财报.xlsx｜key=…]`` in the group log and
calls here only when it decides the file actually matters. The bytes are fetched through the
same channel adapter used for @-message attachments, stored as a normal Artifact, and the
returned ``file_id`` is then read with ``read_artifact`` — so downstream file handling has
exactly one code path.

Registered only for channel runs (see agent_factory's ``_is_channel_run``).
"""

import json
import logging
import mimetypes
import os
from typing import Any, Dict, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

logger = logging.getLogger(__name__)

# Same ceiling as inbound attachment ingestion and /v1/file/upload.
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _err(msg: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(
        type="text", text=json.dumps({"error": msg}, ensure_ascii=False),
    )])


def _lookup(db, chat_id: str, user_id: Optional[str], file_key: str):
    """Resolve (session, channel connection, index entry) or return an error string."""
    from core.db.models import ChannelConnection, ChatSession

    session = (
        db.query(ChatSession)
        .filter(ChatSession.chat_id == chat_id, ChatSession.deleted_at.is_(None))
        .first()
    )
    if session is None:
        return None, None, None, "当前会话不存在"
    # The channel bot runs as its owner, so the session must belong to the calling identity.
    # Without this check a leaked chat_id would let one user pull another's group files.
    if user_id and str(session.user_id) != str(user_id):
        return None, None, None, "无权访问该会话"
    entry = ((session.extra_data or {}).get("observed_files") or {}).get(file_key)
    if not entry:
        return None, None, None, (
            f"未找到 file_key={file_key} 对应的群聊附件；"
            "只有旁听记录里带 key= 的文件可以取回，且过旧的记录会被淘汰"
        )
    if not session.channel_id:
        return None, None, None, "当前会话不是渠道会话"
    conn = (
        db.query(ChannelConnection)
        .filter(ChannelConnection.channel_id == session.channel_id)
        .first()
    )
    if conn is None or not conn.enabled:
        return None, None, None, "渠道连接不存在或已停用"
    return session, conn, entry, None


def _rebuild_inbound(conn, session, entry: Dict[str, Any]):
    """Minimal InboundMsg carrying just what ``download_resource`` reads.

    Lark resolves message resources by ``message_id``; DingTalk needs the ``robotCode`` kept
    in ``raw``. Both were captured at observation time, so no channel-specific branching is
    needed here.
    """
    from core.channels.protocol import InboundMsg

    return InboundMsg(
        channel_id=conn.channel_id,
        channel_type=conn.channel_type,
        text="",
        chat_type="group",
        external_conversation_id=session.external_conversation_id or "",
        message_id=entry.get("message_id") or "",
        raw=dict(entry.get("raw") or {}),
    )


def register_channel_attachment(
    toolkit: Toolkit, user_id: Optional[str] = None, chat_id: Optional[str] = None
) -> None:
    """Register ``channel_read_attachment`` for channel (IM bot) runs."""

    async def channel_read_attachment(file_key: str) -> ToolResponse:
        """按 key 取回群聊里出现过但尚未下载的文件（图片 / 文档 / 表格等）。

        群聊旁听只记录了文件的**句柄**，没有下载内容。当你判断某个文件与当前问题
        相关时，用本工具按 key 取回；取回后会得到 `file_id`，再用 `read_artifact`
        读取正文。

        **不要**仅凭文件名猜测内容——名字和内容经常对不上。
        **不要**把群聊里出现的每个文件都取一遍，只取与当前问题确实相关的。

        Args:
            file_key (`str`):
                文件句柄，取自群聊上下文里 `[文件：xxx｜key=yyy]` 中的 `yyy`。

        Returns:
            JSON: 成功返回 {file_id, filename, mime_type, size, next: 使用提示}；
                  失败返回 {error: 原因}。下载码可能过期，过旧的文件可能取不回。
        """
        from core.channels.protocol import ChannelResourceError
        from core.channels.registry import get_adapter
        from core.db.engine import SessionLocal
        from core.services.artifact_service import store_bytes_as_artifact

        key = (file_key or "").strip()
        if not key:
            return _err("file_key 不能为空")
        if not chat_id:
            return _err("当前不是渠道会话，无法取回群聊附件")

        with SessionLocal() as db:
            session, conn, entry, err = _lookup(db, chat_id, user_id, key)
            if err:
                return _err(err)

            try:
                adapter = get_adapter(conn.channel_type)
            except Exception:  # noqa: BLE001
                return _err(f"渠道 {conn.channel_type} 不可用")
            download = getattr(adapter, "download_resource", None)
            if download is None:
                return _err(f"渠道 {conn.channel_type} 不支持下载消息附件")

            name = entry.get("name") or "file.bin"
            try:
                content = await download(
                    conn, _rebuild_inbound(conn, session, entry),
                    {"kind": entry.get("kind") or "file", "key": key, "name": name},
                )
            except ChannelResourceError as exc:
                # 平台给了明确理由（如跨组织访问被拒），原样透出——把它说成“已过期”
                # 会让用户往完全错误的方向排查。
                return _err(str(exc) or "渠道拒绝了这次读取")
            except Exception:  # noqa: BLE001
                logger.exception("[channels] 按需取回附件失败 key=%s", key[:16])
                return _err("附件下载失败，可能下载码已过期")
            if not content:
                return _err("附件下载失败或已过期（渠道返回空内容）")
            if len(content) > _MAX_ATTACHMENT_BYTES:
                return _err(f"文件过大（{len(content)} 字节），超出 50MB 上限")

            # An online document is fetched as markdown *content*, and its name is a title
            # with no extension. read_artifact dispatches its parser purely on the filename
            # extension, so leaving it bare would mean the file downloads fine and then reads
            # back as "unsupported format" — fetched but unusable. Give it the extension that
            # matches what the bytes actually are.
            if entry.get("kind") == "doc" and not os.path.splitext(name)[1]:
                name = f"{name}.md"
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            art = store_bytes_as_artifact(
                db, user_id=str(session.user_id), content=content, filename=name,
                mime_type=mime, chat_id=chat_id, source="channel_observed",
                extra={"channel_id": conn.channel_id, "observed_file_key": key},
            )
            return ToolResponse(content=[TextBlock(type="text", text=json.dumps({
                "file_id": art.artifact_id,
                "filename": name,
                "mime_type": mime,
                "size": len(content),
                # Images can't be parsed into text by read_artifact — point at the
                # vision bridge instead, otherwise a group-chat screenshot fetches
                # fine and then reads back as "unsupported format".
                "next": (
                    f"用 view_image(file_id='{art.artifact_id}') 看这张图"
                    if mime.startswith("image/")
                    else f"用 read_artifact(file_id='{art.artifact_id}') 读取内容"
                ),
            }, ensure_ascii=False))])

    toolkit.register_tool_function(channel_read_attachment, namesake_strategy="override")
