"""Build and cache the per-user identity block injected at the head of a chat."""
from __future__ import annotations
import asyncio
import logging
from collections import OrderedDict
from time import monotonic

from core.agent_runtime.integrations import memory_state

logger = logging.getLogger(__name__)

_IDENTITY_CACHE_MAX_ENTRIES = 1024
# user_id -> (expires_at, block), least recently written first.
_identity_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


def _remember(user_id: str, expires_at: float, block: str) -> None:
    """Store a block, dropping expired entries and then the oldest beyond the cap."""
    now = monotonic()
    for stale in [key for key, (deadline, _) in _identity_cache.items() if deadline <= now]:
        del _identity_cache[stale]
    _identity_cache[user_id] = (expires_at, block)
    _identity_cache.move_to_end(user_id)
    while len(_identity_cache) > _IDENTITY_CACHE_MAX_ENTRIES:
        _identity_cache.popitem(last=False)

async def build_user_identity_block(user_id: str) -> str:
    """Assemble the "current user" identity block (username / nickname) for injection into the session-frozen message.

    Deliberately goes through the user-role frozen block rather than the end of the system prompt:
    the server-side chat template renders the tool schemas **after** the system text, so any
    per-user bytes in system split the LLM prefix cache of the tool section (the largest chunk
    of the prompt). Measured: identical system differing only in the trailing username → TTFT
    degrades from 0.7s back to cold-start levels. The frozen block sits at the start of the
    message sequence and renders after the tool section, so the system+tools shared prefix stays intact.

    Returns empty string for anonymous / unknown users. Result cached per user_id for 60s.
    """
    if not user_id or user_id == "anonymous":
        return ""
    now = monotonic()
    hit = _identity_cache.get(user_id)
    if hit and now < hit[0]:
        return hit[1]

    def _query() -> tuple:
        from core.db.engine import SessionLocal
        from core.db.models import LocalUser, UserShadow

        with SessionLocal() as db:
            row = db.query(UserShadow.username).filter(UserShadow.user_id == user_id).first()
            nick = db.query(LocalUser.nickname).filter(LocalUser.user_id == user_id).first()
            return (
                (row[0] or "").strip() if row else "",
                (nick[0] or "").strip() if nick else "",
            )

    try:
        username, nickname = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warning("[identity] user lookup failed user=%s: %s", user_id, exc)
        return ""

    lines: list[str] = []
    if username and username != "anonymous":
        lines.append(f"- 用户名：{username}")
    if nickname and nickname != username:
        lines.append(f"- 昵称：{nickname}")
    block = ""
    if lines:
        block = (
            "## 当前用户\n"
            + "\n".join(lines)
            + "\n需要称呼用户时，用上述昵称（无昵称则用用户名）自然称呼。"
        )
    _remember(user_id, now + memory_state.IDENTITY_CACHE_TTL_S, block)
    return block
