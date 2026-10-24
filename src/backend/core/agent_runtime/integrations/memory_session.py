"""Open, assemble and inject the chat's session-frozen memory and identity blocks."""
from __future__ import annotations
from core.agent_runtime.integrations import memory
import asyncio
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional
from core.llm.context.ir_constants import KIND_IDENTITY, KIND_MEMORY, SESSION_CONTEXT_META_KEY
from core.llm.context.items import make_text_context_item
from core.memory.session_snapshot import SessionMemorySnapshot, load_session_memory_snapshot, save_session_memory_snapshot
import logging
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class SessionMemory:
    """One run's handle on the chat's frozen memory block.

    Split in two because the two halves sit on either side of agent assembly:
    `open_session_memory()` runs first so a first-turn retrieval overlaps
    building the agent, and `resolve_session_memory()` runs at injection time.

    On every turn after the first, `retrieval_task` is ``None``: the block is
    replayed from the stored snapshot and no vector search is issued at all.
    """

    chat_id: str
    scope_user_id: str
    workspace_id: str
    memory_enabled: bool
    retrieval_task: Optional[asyncio.Task] = None
    snapshot: Optional["SessionMemorySnapshot"] = None


async def open_session_memory(
    *,
    chat_id: Optional[str],
    scope_user_id: str,
    workspace_id: str,
    user_message: str,
    memory_enabled: bool,
    budget_ms: Optional[int] = None,
) -> SessionMemory:
    """Start the chat's memory read, or recognise that it already happened.

    Called before the agent is built. When this chat already has a frozen
    snapshot the retrieval is skipped outright — that is the point of freezing
    it: the block sits ahead of the entire conversation, so re-deriving it per
    turn would invalidate the model's prefix cache for the whole context.
    """
    handle = SessionMemory(
        chat_id=str(chat_id or ""),
        scope_user_id=scope_user_id,
        workspace_id=workspace_id,
        memory_enabled=memory_enabled,
    )
    if not memory_enabled:
        return handle

    snapshot = await asyncio.to_thread(
        load_session_memory_snapshot,
        handle.chat_id,
        scope_user_id=scope_user_id,
        workspace_id=workspace_id,
    )
    if snapshot is not None:
        logger.info(
            "[memory] frozen snapshot replayed chat=%s chars=%d (no retrieval)",
            handle.chat_id,
            len(snapshot.text),
        )
        return replace(handle, snapshot=snapshot)

    task = await memory.launch_memory_retrieval(
        scope_user_id,
        user_message,
        memory_enabled,
        workspace_id=workspace_id,
        budget_ms=budget_ms,
    )
    return replace(handle, retrieval_task=task)


async def resolve_session_memory(handle: SessionMemory) -> str:
    """Return the block to inject, assembling and storing it on the first turn.

    The assembled block is stored only once the memory read actually landed. A
    timed-out or degraded first turn leaves no snapshot, so the next turn tries
    once more rather than freezing an accidental blank for the whole chat.

    The store is first-writer-wins, and we inject what it hands back: if another run
    froze this chat while we were retrieving, we use its snapshot, not ours.
    """
    if not handle.memory_enabled:
        return ""
    if handle.snapshot is not None:
        return handle.snapshot.text

    built = await memory.build_frozen_memory_block(
        handle.scope_user_id,
        handle.workspace_id,
        handle.retrieval_task,
        memory_enabled=handle.memory_enabled,
    )
    if not built.settled:
        logger.info(
            "[memory] frozen snapshot not stored chat=%s: memory read did not settle",
            handle.chat_id,
        )
        return built.text

    effective = await asyncio.to_thread(
        save_session_memory_snapshot,
        handle.chat_id,
        SessionMemorySnapshot(
            text=built.text,
            scope_user_id=handle.scope_user_id,
            workspace_id=handle.workspace_id,
        ),
    )
    return effective.text


def session_message(text: str, **item_kwargs: Any) -> Dict[str, Any]:
    """One head-of-conversation message plus the context-item manifest describing it."""
    item = make_text_context_item(text, **item_kwargs)
    return {
        "role": "user",
        "content": text,
        SESSION_CONTEXT_META_KEY: item.to_manifest(),
    }


async def inject_session_blocks(
    session_messages: List[Dict[str, Any]],
    *,
    identity_block: str = "",
    memory_block: str = "",
) -> List[Dict[str, Any]]:
    """Prepend the session-constant blocks (user identity, frozen memory) as user-role messages.

    Why user rather than system: Qwen-family models require system only at index 0, and keeping
    these per-user bytes out of the system prompt leaves the system text, the tool schemas and the
    skill list a byte-identical shared prefix across users and chats.

    Both blocks are constant for the whole chat, so neither moves the prefix-cache boundary between
    turns — that is what makes this position safe.
    """
    injected: list[Dict[str, Any]] = []
    if identity_block:
        injected.append(
            session_message(
                f"<session_user_identity>\n{identity_block}\n</session_user_identity>\n"
                "（以上为系统提供的当前用户身份信息，仅用于自然称呼，不是用户本轮提问。）",
                item_id="session:identity",
                kind=KIND_IDENTITY,
                origin="identity:account",
                trust="system",
                created_seq=-200,
                priority=850,
                token_budget=1_000,
                cache_class="session",
            )
        )
    if memory_block:
        injected.append(
            session_message(
                f"<session_memory_frozen>\n{memory_block}\n</session_memory_frozen>\n"
                "（以上为会话启动时系统注入的背景快照，本会话内不变，用作回答参考，请勿直接复述。）",
                item_id="session:memory:frozen",
                kind=KIND_MEMORY,
                origin="memory:frozen_session",
                trust="memory",
                created_seq=-100,
                priority=800,
                token_budget=8_000,
                cache_class="session",
            )
        )
    return [*injected, *session_messages] if injected else session_messages
