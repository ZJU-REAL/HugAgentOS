"""Recent chat trajectory support for memory extraction.

Most memories can be judged from the current user/assistant pair.  A verified
correction cannot: its evidence is spread across at least three messages — an
assistant failure, a user-provided change of method, and a later successful
result.  This module loads a small bounded window after the response has been
persisted and detects that high-confidence shape without putting database I/O
back on the SSE hot path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from core.memory.context import MemoryContext

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 8
_MAX_MESSAGE_CHARS = 2400
_MAX_TRANSCRIPT_CHARS = 12000

_ASSISTANT_FAILURE_CUES = re.compile(
    r"(?:无法|不能|没法|失败|未成功|不支持|不可用|无外网|无网络|网络隔离|"
    r"解析不了|拿不到|做不到|cannot|can't|unable|failed|not available|no network)",
    re.IGNORECASE,
)
_USER_METHOD_CHANGE_CUES = re.compile(
    r"(?:不对|错了|不是.{0,50}而是|不要.{0,50}(?:而是|改用)|改用|换成|"
    r"直接(?:用|走|调用|执行|下载|读取)|应该(?:先|用|改)|可以(?:先|用|改)|"
    r"你(?:可以|把|先|直接)|请把|试试|先.{0,100}再|"
    r"instead|try using|use .{0,80} instead|you can|you should)",
    re.IGNORECASE | re.DOTALL,
)
_USER_EXPLICIT_CORRECTION_CUES = re.compile(
    r"(?:不对|错了|你(?:刚才|之前).{0,80}(?:错|不对|没|没有|不应该)|"
    r"不是.{0,60}(?:而是|应该)|不要.{0,60}(?:而是|应该|要)|"
    r"应该.{0,80}而不是|不能.{0,80}就|"
    r"that's wrong|that is wrong|not correct|you were wrong|"
    r"you should have|instead of what you did)",
    re.IGNORECASE | re.DOTALL,
)
_ASSISTANT_SUCCESS_CUES = re.compile(
    r"(?:成功|已(?:下载|完成|获取|生成|交付|验证|解决|修复)|"
    r"验证.{0,30}(?:通过|完整|正常)|现在可以|一下就成功|"
    r"succeeded|successful|completed|verified|now works)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RecentTrajectory:
    """Bounded text sent to the memory judge plus a deterministic quality signal."""

    transcript: str
    verified_correction: bool = False


def is_verified_correction(messages: Sequence[Mapping[str, str]]) -> bool:
    """Return true for a user-corrected attempt followed by a successful retry.

    Two evidence paths qualify:

    * an earlier assistant message explicitly reports failure, then the user
      changes the method and the retry succeeds;
    * the assistant gave an earlier result without admitting failure, but the
      user explicitly corrects that method and the retry succeeds.

    A bare retry is deliberately insufficient.  That keeps transient outages
    and ordinary follow-up turns out of long-term procedural memory.
    """

    for correction_index, correction in enumerate(messages):
        if correction.get("role") != "user":
            continue
        correction_text = correction.get("content") or ""
        if not _USER_METHOD_CHANGE_CUES.search(correction_text):
            continue

        prior_assistant_messages = [
            message for message in messages[:correction_index] if message.get("role") == "assistant"
        ]
        if not prior_assistant_messages:
            continue
        prior_explicit_failure = any(
            _ASSISTANT_FAILURE_CUES.search(message.get("content") or "")
            for message in prior_assistant_messages
        )
        user_explicitly_corrected = bool(_USER_EXPLICIT_CORRECTION_CUES.search(correction_text))
        if not (prior_explicit_failure or user_explicitly_corrected):
            continue

        for outcome in messages[correction_index + 1 :]:
            if outcome.get("role") != "assistant":
                continue
            if _ASSISTANT_SUCCESS_CUES.search(outcome.get("content") or ""):
                return True
    return False


def _load_messages_sync(ctx: MemoryContext) -> list[dict[str, str]]:
    if not ctx.chat_id:
        return []

    from core.db.engine import SessionLocal
    from core.db.repository import ChatMessageRepository

    with SessionLocal() as session:
        repository = ChatMessageRepository(session)
        before = None
        if ctx.message_id:
            current = repository.get_by_id(ctx.message_id)
            if (
                current is not None
                and current.chat_id == ctx.chat_id
                and current.role == "assistant"
            ):
                before = current.created_at

        rows = repository.list_recent_by_chat(
            ctx.chat_id,
            limit=_MAX_MESSAGES,
            before=before,
        )
        return [
            {
                "role": str(row.role),
                "content": str(row.content or "")[:_MAX_MESSAGE_CHARS],
            }
            for row in rows
        ]


def _ensure_current_pair(
    messages: list[dict[str, str]],
    user_message: str,
    assistant_message: str,
) -> list[dict[str, str]]:
    """Include the just-finished pair when persistence races or DB reads fail."""

    result = list(messages)
    if (
        result
        and result[-1].get("role") == "assistant"
        and result[-1].get("content") == (assistant_message[:_MAX_MESSAGE_CHARS])
    ):
        return result

    current_user = user_message[:_MAX_MESSAGE_CHARS]
    if not (
        result and result[-1].get("role") == "user" and result[-1].get("content") == current_user
    ):
        result.append({"role": "user", "content": current_user})
    result.append(
        {
            "role": "assistant",
            "content": assistant_message[:_MAX_MESSAGE_CHARS],
        }
    )
    return result[-_MAX_MESSAGES:]


def _render(messages: Sequence[Mapping[str, str]]) -> str:
    rendered = "\n\n".join(
        f"[{str(message.get('role') or '').upper()}]\n{message.get('content') or ''}"
        for message in messages
    )
    return rendered[-_MAX_TRANSCRIPT_CHARS:]


async def load_recent_trajectory(
    ctx: MemoryContext,
    user_message: str,
    assistant_message: str,
) -> RecentTrajectory:
    """Load and classify a bounded recent trajectory off the response path."""

    messages: list[dict[str, str]] = []
    if ctx.chat_id:
        try:
            loop = asyncio.get_running_loop()
            messages = await loop.run_in_executor(None, _load_messages_sync, ctx)
        except Exception as exc:  # noqa: BLE001 - current pair remains a safe fallback
            logger.debug("[memory_trajectory] recent chat load failed: %s", exc)

    messages = _ensure_current_pair(messages, user_message, assistant_message)
    return RecentTrajectory(
        transcript=_render(messages),
        verified_correction=is_verified_correction(messages),
    )
