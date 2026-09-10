"""Task-scoped channel I/O. Desktop execution never receives channel credentials."""

from __future__ import annotations

import base64
from dataclasses import asdict

from core.channels.protocol import InboundMsg, SendResult
from core.channels.registry import get_adapter
from core.db.models.channel_relay import ChannelRelayOperation
from core.infra.exceptions import BadRequestError
from core.services.channel_relay import ChannelRelayService
from sqlalchemy.exc import IntegrityError

MAX_FILE = 50 * 1024 * 1024
READ_OPS = {"resolve_addressed", "fetch_history", "download_resource"}
SEND_OPS = {
    "send_text",
    "send_placeholder",
    "send_markdown",
    "edit_message",
    "recall_message",
    "push_file",
}


def pack_bytes(data):
    if data is None:
        return None
    if len(data) > MAX_FILE:
        raise BadRequestError("文件超过 50 MB 限制")
    return base64.b64encode(data).decode("ascii")


def unpack_bytes(data):
    if not isinstance(data, str) or len(data) > MAX_FILE * 4 // 3 + 8:
        raise BadRequestError("文件超过 50 MB 限制")
    try:
        out = base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise BadRequestError("文件编码无效") from exc
    if len(out) > MAX_FILE:
        raise BadRequestError("文件超过 50 MB 限制")
    return out


async def channel_operation(db, owner, device, delivery_id, lease, op_id, method, args):
    if method not in READ_OPS | SEND_OPS:
        raise BadRequestError("不支持的渠道操作")
    task, conn = ChannelRelayService(db).authorized(owner, device, delivery_id, lease)
    msg = InboundMsg(**task.payload["message"])
    adapter = get_adapter(conn.channel_type)
    opkey = f"{delivery_id}:{op_id}"
    if method in SEND_OPS:
        previous = db.get(ChannelRelayOperation, opkey)
        if previous:
            if previous.status == "completed":
                return previous.result
            raise BadRequestError("消息发送结果不确定，已阻止重复发送")
        db.add(ChannelRelayOperation(operation_id=opkey, status="started"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise BadRequestError("消息操作正在处理，已阻止重复发送")
    callback = getattr(adapter, method, None)
    result = None
    if method == "resolve_addressed":
        result = await callback(conn, msg) if callback else True
    elif method == "fetch_history":
        items = (
            await callback(
                conn,
                msg.external_conversation_id,
                since_ms=max(0, int(args.get("since_ms", 0))),
                limit=min(100, max(1, int(args.get("limit", 50)))),
                newest_first=bool(args.get("newest_first", False)),
            )
            if callback
            else []
        )
        result = [asdict(item) for item in items]
        payload = dict(task.payload)
        resources = dict(payload.get("resources") or {})
        for item in items:
            for attachment in item.attachments:
                resources[str(attachment.get("key"))] = {
                    "attachment": attachment,
                    "message_id": item.message_id,
                    "raw": item.raw,
                }
        payload["resources"] = dict(list(resources.items())[-200:])
        task.payload = payload
        db.commit()
    elif method == "download_resource":
        requested = args.get("attachment") or {}
        known = {
            str(a.get("key")): {"attachment": a, "message_id": msg.message_id, "raw": msg.raw}
            for a in msg.attachments
        }
        known.update(task.payload.get("resources") or {})
        entry = known.get(str(requested.get("key")))
        if not entry:
            raise BadRequestError("附件不属于当前渠道任务")
        msg.message_id, msg.raw = entry["message_id"], entry["raw"]
        result = pack_bytes(await callback(conn, msg, entry["attachment"])) if callback else None
    elif method in {"edit_message", "recall_message"}:
        mid = str(args.get("message_id") or "")
        known_ids = set()
        rows = (
            db.query(ChannelRelayOperation)
            .filter(
                ChannelRelayOperation.operation_id.like(delivery_id + ":%"),
                ChannelRelayOperation.status == "completed",
            )
            .all()
        )
        for row in rows:
            if isinstance(row.result, dict) and row.result.get("message_id"):
                known_ids.add(row.result["message_id"])
        if mid not in known_ids:
            raise BadRequestError("消息不属于当前渠道任务")
        if not callback:
            result = asdict(SendResult.fail("bad_format", "渠道不支持此操作"))
        elif method == "edit_message":
            result = asdict(await callback(conn, mid, str(args.get("text") or "")[:200000]))
        else:
            result = asdict(await callback(conn, msg, mid))
    elif method == "push_file":
        result = (
            asdict(
                await callback(
                    conn,
                    msg,
                    unpack_bytes(args.get("content")),
                    str(args.get("filename") or "file")[:255],
                    str(args.get("mime_type") or "application/octet-stream")[:128],
                )
            )
            if callback
            else asdict(SendResult.fail("bad_format", "渠道不支持文件"))
        )
    else:
        callback = callback or adapter.send_text
        text = str(args.get("text") or "")[:200000]
        if method == "send_markdown" and hasattr(adapter, "prepare_markdown"):
            text = adapter.prepare_markdown(text)
        result = asdict(await callback(conn, msg, text))
    if method in SEND_OPS:
        row = db.get(ChannelRelayOperation, opkey)
        row.result, row.status = result, "completed"
        db.commit()
    return result
