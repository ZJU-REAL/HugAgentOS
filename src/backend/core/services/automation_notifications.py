"""The per-user automation notification feed.

One owner for what used to be a key format, a cap and a TTL repeated in the
scheduler that writes notifications and the routes that read them.

The feed is advisory UI state with a short life, so it lives in the
deployment's ephemeral state (:mod:`core.infra.ephemeral`) as a single
newest-first JSON array rather than as a store-specific list type — which is
what lets the same code serve a Redis deployment and the desktop's
in-process one.

Writes are read-modify-write. Two concurrent writers for the *same user* can
therefore lose one entry; the backend runs a single process, and the feed is
advisory, so the window is not worth a locking protocol.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from core.infra.ephemeral import get_ephemeral_state
from core.infra.logging import get_logger

logger = get_logger(__name__)

KEY = "jx:notifications:{user_id}"
MAX_ENTRIES = 50
TTL_SECONDS = 7 * 24 * 3600


def _key(user_id: str) -> str:
    return KEY.format(user_id=user_id)


async def _load(user_id: str) -> List[Dict[str, Any]]:
    raw = await get_ephemeral_state().get(_key(user_id))
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


async def _store(user_id: str, entries: List[Dict[str, Any]]) -> None:
    await get_ephemeral_state().put(
        _key(user_id),
        json.dumps(entries[:MAX_ENTRIES], ensure_ascii=False),
        ttl=TTL_SECONDS,
    )


async def push(user_id: str, notification: Dict[str, Any]) -> None:
    """Add one notification to the front of the user's feed."""
    await _store(user_id, [notification, *await _load(user_id)])


async def list_recent(user_id: str) -> List[Dict[str, Any]]:
    """The user's notifications, newest first."""
    try:
        return await _load(user_id)
    except Exception as exc:  # noqa: BLE001 — the feed must never fail a page load
        logger.warning("automation_notifications_read_failed", error=str(exc))
        return []


async def modify(
    user_id: str,
    ids: set,
    transform: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
) -> None:
    """Apply *transform* to the matching entries; return None from it to drop one."""
    kept: List[Dict[str, Any]] = []
    for entry in await _load(user_id):
        if entry.get("id") in ids:
            changed = transform(entry)
            if changed is None:
                continue
            entry = changed
        kept.append(entry)
    await _store(user_id, kept)


__all__ = ["KEY", "MAX_ENTRIES", "TTL_SECONDS", "list_recent", "modify", "push"]
