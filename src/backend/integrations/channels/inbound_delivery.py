"""Channel delivery responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import core.runs.events.subscription as subscription
from integrations.channels.protocol import InboundMsg
import logging
logger = logging.getLogger(__name__)

class ChannelRunError(RuntimeError):
    """A user-facing terminal error emitted by the shared chat-run stream."""


async def collect_reply(run_id: str):
    """Follow the run's event stream, accumulating assistant text + capturing artifact files generated this turn (meta.artifacts).

    Structured-reasoning models emit thinking as separate ``thinking`` events (ignored
    here), but inline-thinking models embed ``<think>…</think>`` in the content deltas
    themselves — strip those before handing the text to the channel, or the raw chain
    of thought goes out as message text (the web UI parses the tags; IM cannot).
    """
    from integrations.channels.markdown import strip_inline_thinking
    pass

    full = ""
    artifacts: List[Dict[str, Any]] = []
    from integrations.channels.registry import current_relay_adapter

    relay = current_relay_adapter()
    async for event in subscription.follow_run(run_id):
        et = event.get("type")
        if relay and et in {"file_confirm", "ask_user_question"}:
            relay.error = "此操作需要确认，请在桌面端处理后重试"
            await relay.cancel()
            raise ChannelRunError(relay.error)
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


async def recall_placeholder(adapter, conn, msg: InboundMsg, placeholder_id: str) -> None:
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


async def replace_placeholder(
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
        await recall_placeholder(adapter, conn, msg, placeholder_id)
    fr = await adapter.send_text(conn, msg, text)
    if not fr.success:
        logger.warning("[channels] 占位替换消息发送失败 kind=%s detail=%s", fr.error_kind, fr.error_detail)


async def deliver_reply(adapter, conn, msg: InboundMsg, reply: str, placeholder_id: Optional[str]) -> None:
    """#2+#4: chunk long replies; if there is a placeholder message, edit the first chunk into it and send the rest as follow-up messages.
    Channel supports recall but not edit (DingTalk) → recall the placeholder then send, a visually equivalent replacement.

    When the channel supports markdown (caps.supports_markdown + send_markdown), send with native rendering;
    run prepare_markdown on the whole text before chunking (tables etc. can no longer be recognized and converted once cut by chunk boundaries).
    [ref:tool-N] citation markers only render on the web client, so they are always stripped before going out (all channels)."""
    from integrations.channels.markdown import strip_citation_markers
    from integrations.channels.protocol import chunk_text

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
            await recall_placeholder(adapter, conn, msg, placeholder_id)
    for c in rest:
        fr = await (send_md or adapter.send_text)(conn, msg, c)
        if not fr.success:
            logger.warning("[channels] 回复分段推送失败 kind=%s detail=%s", fr.error_kind, fr.error_detail)
