"""Non-blocking memory I/O integration layer outside the SSE main path.

- `launch_memory_retrieval()` starts the Fact retrieval task in the background with a budget timeout
- `build_frozen_memory_block()` assembles the Profile + Fact frozen block once at session start
- `save_memories_background()` delegates writes to the bounded post-response pipeline in
  `core.memory.pipeline` (extractors → sanitize → write L1/L2/Session + audit)
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any, Dict, List, Optional

from core.config.settings import settings
from core.memory import profile
from core.memory.context import MemoryContext
from core.memory.service import retrieve_memories

logger = logging.getLogger(__name__)


async def launch_memory_retrieval(
    user_id: str,
    user_message: str,
    memory_enabled: bool,
    *,
    workspace_id: str = "default",
    budget_ms: Optional[int] = None,
) -> Optional[asyncio.Task]:
    """Start the Fact vector retrieval task in the background and return the Task (the caller need not await immediately).

    budget_ms defaults to settings.memory.retrieval_budget_ms (600ms). Called once by the
    workflow at session start; if it completes within budget the frozen block is injected, otherwise skipped.
    """
    if not memory_enabled or not user_id:
        return None

    effective_budget = (budget_ms if budget_ms is not None
                        else settings.memory.retrieval_budget_ms) / 1000.0

    async def _fetch() -> Optional[str]:
        try:
            return await retrieve_memories(
                user_id=user_id,
                query=user_message,
                workspace_id=workspace_id,
                timeout_s=effective_budget,
            )
        except Exception as exc:
            logger.warning("[memory] retrieval failed: %s", exc)
            return None

    return asyncio.create_task(_fetch())


async def build_frozen_memory_block(
    user_id: str,
    workspace_id: str,
    memory_task: Optional[asyncio.Task],
    *,
    memory_enabled: bool = True,
) -> str:
    """Assemble the "session-frozen" block = L1 Profile markdown + L2 Fact top-K.

    - When `memory_enabled=False`, return empty immediately (**do not load unless the user
      has enabled persistent memory** — forms defense-in-depth with the workflow layer's
      `_mem0_enabled` check)
    - Profile reads the DB (fast, <20ms), always awaited
    - Fact takes the result from the already-started memory_task; if the task hasn't finished,
      wait briefly; if still unfinished, give up on Fact injection for this round (never block agent startup)

    Returns the assembled text; empty string when there is nothing.
    """
    if not memory_enabled:
        return ""

    # Profile layer (L1)
    profile_md = ""
    if user_id:
        try:
            profile_md = await profile.get(user_id, workspace_id)
        except Exception as exc:
            logger.warning("[memory] profile fetch failed user=%s ws=%s: %s",
                           user_id, workspace_id, exc)

    # Fact layer (L2)
    fact_text = ""
    if memory_task is not None:
        try:
            # Wait up to retrieval_budget_ms (default 600ms), then give up; memory_task was
            # started before the agent was created, so in most cases it is nearly done by now.
            # The old value of 50ms was measured to be far below Milvus warm search's ~200ms,
            # so Fact injection almost never hit.
            wait_budget_s = max(0.1, settings.memory.retrieval_budget_ms / 1000.0)
            fact_text = await asyncio.wait_for(memory_task, timeout=wait_budget_s) or ""
        except asyncio.TimeoutError:
            logger.info("[memory] fact retrieval still running past wait window, skipping injection")
            # The task finishes in the background and is released; not cancelled (the result can be used for the next round of log statistics)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[memory] fact retrieval await failed: %s", exc)

    if not profile_md and not fact_text:
        return ""

    parts: list[str] = ["## 关于当前用户的已知背景（会话开始时冻结）"]
    if profile_md:
        parts.append("")
        parts.append("### 用户档案（Profile）")
        parts.append(profile_md.strip())
    if fact_text:
        parts.append("")
        parts.append("### 相关历史记忆（Fact Top-K）")
        # retrieve_memories comes with its own "## 关于该用户..." prefix; strip it to avoid duplicate headings
        stripped = fact_text
        for h in ("## 关于该用户的已知背景信息（来自历史会话记忆）", "## 用户相关实体关系"):
            stripped = stripped.replace(h, "")
        parts.append(stripped.strip())

    parts.append("")
    parts.append(
        "**使用规则**：以上是背景参考，不是用户本轮提问的一部分；"
        "如与用户当前消息冲突，以当前消息为准。"
    )
    block = "\n".join(parts).strip()
    logger.info(
        "[memory] frozen block built user=%s ws=%s chars=%d profile=%d facts=%d",
        user_id, workspace_id, len(block), len(profile_md or ""), len(fact_text or ""),
    )
    return block


# ─── User identity block ───────────────────────────────────────────────────

_IDENTITY_CACHE_TTL_S = 60.0
# user_id → (expires_at_monotonic, block_text)
_identity_cache: Dict[str, tuple] = {}


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
            row = (
                db.query(UserShadow.username)
                .filter(UserShadow.user_id == user_id)
                .first()
            )
            nick = (
                db.query(LocalUser.nickname)
                .filter(LocalUser.user_id == user_id)
                .first()
            )
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
            "## 当前用户\n" + "\n".join(lines)
            + "\n需要称呼用户时，用上述昵称（无昵称则用用户名）自然称呼。"
        )
    _identity_cache[user_id] = (now + _IDENTITY_CACHE_TTL_S, block)
    return block


async def inject_frozen_memory(
    frozen_block: str,
    session_messages: List[Dict[str, Any]],
    *,
    identity_block: str = "",
) -> List[Dict[str, Any]]:
    """Insert the frozen block (user identity + memory snapshot) as a user-role message at the start of session_messages.

    Why user rather than system: first, Qwen-family models require system only at index 0, and the
    agent has already injected sys_prompt into the system slot separately; second, per-user content
    in system punctures the LLM prefix cache of the tool section (see the build_user_identity_block comment).
    """
    if not frozen_block and not identity_block:
        return session_messages
    parts: list[str] = []
    if identity_block:
        parts.append(
            "<session_user_identity>\n"
            f"{identity_block}\n"
            "</session_user_identity>"
        )
    if frozen_block:
        parts.append(
            "<session_memory_frozen>\n"
            f"{frozen_block}\n"
            "</session_memory_frozen>"
        )
    return [
        {
            "role": "user",
            "content": (
                "\n\n".join(parts)
                + "\n（以上为会话启动时系统注入的背景快照，本会话内不变，"
                "用作回答参考，请勿直接复述。）"
            ),
        },
        *session_messages,
    ]


# ─── Saving ─────────────────────────────────────────────────────────────────


def save_memories_background(
    user_id: str,
    user_message: str,
    full_response: str,
    write_enabled: bool,
    *,
    workspace_id: str = "default",
    chat_id: Optional[str] = None,
    scope_user_id: Optional[str] = None,
) -> None:
    """Delegate to the unified post-response pipeline — never await; SSE is closed and the user isn't waiting.

    **When the user has not explicitly consented to writes (`write_enabled=False`), skip
    immediately and create no tasks at all**. This is the first gate of the user-level write
    switch; `schedule_post_response_tasks` has a second one inside.

    Inside `schedule_post_response_tasks`:
    - global Semaphore bounds concurrency (default 8)
    - runs 0-4 extractors classified by keyword
    - each extractor has its own 30s timeout
    - sanitize → write L1/L2/Session → audit

    `scope_user_id` under team projects = ``f"team:{team_id}"``, so all team members' writes
    go into the same mem0 user_id bucket, and reads pull from that bucket to achieve sharing.
    ``user_id`` remains the real user, used for audit and metadata.author_user_id.
    """
    if not (write_enabled and full_response and user_id):
        return

    try:
        from core.memory.pipeline import schedule_post_response_tasks
    except Exception as exc:
        logger.warning("[memory] pipeline unavailable, skipping save: %s", exc)
        return

    ctx = MemoryContext(
        user_id=user_id,
        workspace_id=workspace_id,
        chat_id=chat_id,
        write_enabled=write_enabled,
        scope_user_id=scope_user_id,
    )
    try:
        schedule_post_response_tasks(ctx, user_message, full_response)
    except Exception as exc:
        logger.warning("[memory] schedule_post_response_tasks failed: %s", exc)
