"""Build and cache the per-user identity block injected at the head of a chat."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from time import monotonic
from typing import Dict
import logging
from core.agent_runtime.integrations import memory_state

logger = logging.getLogger(__name__)

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
    hit = process_state().identity_cache.get(user_id)
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
    process_state().identity_cache[user_id] = (now + memory_state.IDENTITY_CACHE_TTL_S, block)
    return block


@dataclass
class MemoryState:
    identity_cache: Dict[str, tuple] = field(default_factory=lambda: {})


_STATE = MemoryState()


def process_state() -> MemoryState:
    return _STATE
