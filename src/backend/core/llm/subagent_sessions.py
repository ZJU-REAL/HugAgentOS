"""Resumable sub-agent sessions for ``call_subagent``.

A dispatch normally ends with the child agent's context discarded, so a
follow-up task restarts from zero and re-reads the same files, re-runs the same
commands and often repeats the same mistake. Persisting the child's context
under a short handle lets the parent send the next task to the *same* child.

The state is ephemeral and conversation-scoped, so it lives in the
deployment's ephemeral state under the run's own stream TTL — Redis where one
is configured, an in-process map where none is.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from core.infra.ephemeral import get_ephemeral_state
from core.llm.human_interaction import STREAM_TTL_SECONDS

logger = logging.getLogger(__name__)

_KEY_PREFIX = "hugagent:subagent_session:"

# A resumable handle is only useful while the run that created it is still
# alive, so it expires with the run's own ephemeral state.
_TTL_SECONDS = STREAM_TTL_SECONDS

# A child that read large files can carry a context far bigger than anything
# worth storing. Older turns are dropped until the payload fits.
_MAX_PAYLOAD_BYTES = 512 * 1024


def new_handle() -> str:
    return f"sub-{uuid.uuid4().hex[:10]}"


def _key(handle: str) -> str:
    return f"{_KEY_PREFIX}{handle}"


def _fit_payload(record: Dict[str, Any]) -> Optional[str]:
    """Serialize ``record``, dropping the oldest resumable turns until it fits.

    The first message is the child's original task and is never dropped — it is
    what keeps a resumed run anchored to what it was asked to do.
    """
    messages: List[Dict[str, Any]] = list(record.get("messages") or [])
    while True:
        payload = json.dumps(record, ensure_ascii=False)
        if len(payload.encode("utf-8")) <= _MAX_PAYLOAD_BYTES:
            return payload
        if len(messages) <= 2:
            return None
        messages.pop(1)
        record["messages"] = messages


async def save(
    *,
    handle: str,
    agent_id: str,
    user_id: str,
    chat_id: Optional[str],
    messages: List[Dict[str, Any]],
) -> bool:
    """Persist a child's context under ``handle``. Never raises."""
    if not handle or not messages:
        return False

    payload = _fit_payload(
        {
            "agent_id": str(agent_id),
            "user_id": str(user_id or ""),
            "chat_id": str(chat_id or ""),
            "messages": messages,
        }
    )
    if payload is None:
        logger.info("[subagent_sessions] context too large to persist: agent=%s", agent_id)
        return False

    try:
        await get_ephemeral_state().put(_key(handle), payload, ttl=_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — resume is an optimization, never a hard dependency
        logger.debug("[subagent_sessions] save failed, resume unavailable: %s", exc)
        return False
    return True


async def load(
    *,
    handle: str,
    agent_id: str,
    user_id: str,
) -> Optional[List[Dict[str, Any]]]:
    """Return the context stored under ``handle``, or None when unusable.

    A handle only resumes the sub-agent and user it was created for, so a
    hallucinated or copied handle cannot pull another agent's context.
    """
    if not handle:
        return None

    try:
        raw = await get_ephemeral_state().get(_key(handle))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[subagent_sessions] load failed: %s", exc)
        return None

    if raw is None:
        return None
    payload = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)

    try:
        record = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None

    if record.get("agent_id") != str(agent_id):
        return None
    if record.get("user_id") != str(user_id or ""):
        return None

    messages = record.get("messages")
    return messages if isinstance(messages, list) and messages else None


__all__ = ["new_handle", "save", "load"]
