"""Memory retrieval for a turn: background recall, the frozen memory block and post-turn saving."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Optional
from core.config.settings import settings
from core.memory import profile
from core.memory.context import MemoryContext
from core.memory.retrieval_types import MemoryRetrievalResult
from core.memory.service import retrieve_memories_structured
import logging
from core.agent_runtime.integrations import memory_state

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

    effective_budget = (
        budget_ms if budget_ms is not None else settings.memory.retrieval_budget_ms
    ) / 1000.0

    # The assembly boundary below owns the latency budget.  Passing the same
    # budget into the service would make its inner wait_for cancel _do_search
    # before build_frozen_memory_block() can shield the task.

    async def _fetch() -> Optional[MemoryRetrievalResult]:
        try:
            return await retrieve_memories_structured(
                user_id=user_id,
                query=user_message,
                workspace_id=workspace_id,
                timeout_s=None,
            )
        except Exception as exc:
            logger.warning("[memory] retrieval failed: %s", exc)
            return MemoryRetrievalResult.degraded_result("orchestration_error")

    return track_memory_retrieval(asyncio.create_task(_fetch()), budget_s=effective_budget)


def as_result(value: object) -> Optional[MemoryRetrievalResult]:
    """Normalise whatever the retrieval task produced.

    The task may legitimately yield ``None`` (never started) and, for callers
    that still hand us a pre-rendered string, we degrade gracefully rather than
    crash the turn.
    """
    if isinstance(value, MemoryRetrievalResult):
        return value
    return None


def track_memory_retrieval(task: asyncio.Task, *, budget_s: Optional[float] = None) -> asyncio.Task:
    """Attach lifecycle observation without taking ownership of cancellation."""

    memory_state.process_state().retrieval_states[task] = "running"
    if budget_s is not None:
        memory_state.process_state().retrieval_budgets[task] = max(0.0, budget_s)

    def _finished(done: asyncio.Task) -> None:
        previous = memory_state.process_state().retrieval_states.get(done)
        if done.cancelled():
            memory_state.process_state().retrieval_states[done] = "cancelled"
            return
        try:
            value = done.result()
        except Exception:
            memory_state.process_state().retrieval_states[done] = "failed"
            return
        result = as_result(value)
        if result is not None:
            set_last_retrieval(done, result)
        memory_state.process_state().retrieval_states[done] = (
            "completed_after_timeout" if previous == "timed_out_running" else "completed"
        )

    task.add_done_callback(_finished)
    return task


def get_retrieval_state(task: Optional[asyncio.Task]) -> Optional[str]:
    if task is None:
        return None
    return memory_state.process_state().retrieval_states.get(task)


def set_last_retrieval(task: asyncio.Task, result: MemoryRetrievalResult) -> None:
    try:
        memory_state.process_state().last_retrieval[task] = result
    except TypeError:  # pragma: no cover - non-weakrefable task
        pass


def get_last_retrieval(task: Optional[asyncio.Task]) -> Optional[MemoryRetrievalResult]:
    """Structured recall for a turn, for trace-event emission.

    Returns ``None`` when retrieval never ran or timed out — callers must treat
    that as "no evidence", never as "memory contributed nothing".
    """
    if task is None:
        return None
    try:
        return memory_state.process_state().last_retrieval.get(task)
    except TypeError:  # pragma: no cover
        return None


@dataclass(frozen=True)
class FrozenMemoryBlock:
    """An assembled frozen block plus whether the read behind it actually landed.

    ``settled`` is false when the L2 read timed out or came back degraded. Such
    a block is missing facts for reasons that say nothing about what the user's
    memory holds, so it must not be frozen for the remainder of the session.
    """

    text: str
    settled: bool


async def build_frozen_memory_block(
    user_id: str,
    workspace_id: str,
    memory_task: Optional[asyncio.Task],
    *,
    memory_enabled: bool = True,
) -> FrozenMemoryBlock:
    """Assemble the "session-frozen" block = L1 Profile markdown + L2 Fact top-K.

    - When `memory_enabled=False`, return empty immediately (**do not load unless the user
      has enabled persistent memory** — forms defense-in-depth with the workflow layer's
      `_mem0_enabled` check)
    - Profile reads the DB (fast, <20ms), always awaited
    - Fact takes the result from the already-started memory_task; if the task hasn't finished,
      wait briefly; if still unfinished, give up on Fact injection for this round (never block agent startup)

    Called once per chat by `resolve_session_memory()`; later turns replay the
    stored snapshot instead of reassembling.
    """
    if not memory_enabled:
        return FrozenMemoryBlock(text="", settled=False)

    # Profile layer (L1)
    profile_md = ""
    if user_id:
        try:
            profile_md = await profile.get(user_id, workspace_id)
        except Exception as exc:
            logger.warning(
                "[memory] profile fetch failed user=%s ws=%s: %s", user_id, workspace_id, exc
            )

    # Fact layer (L2). With no retrieval task there is no L2 read to wait for,
    # so a Profile-only block is already the complete answer for this session.
    fact_text = ""
    fact_settled = True
    fact_result: Optional[MemoryRetrievalResult] = None
    if memory_task is not None:
        fact_settled = False
        try:
            # Wait up to retrieval_budget_ms (default 600ms), then give up; memory_task was
            # started before the agent was created, so in most cases it is nearly done by now.
            # The old value of 50ms was measured to be far below Milvus warm search's ~200ms,
            # so Fact injection almost never hit.
            wait_budget_s = memory_state.process_state().retrieval_budgets.get(
                memory_task, max(0.1, settings.memory.retrieval_budget_ms / 1000.0)
            )
            fact_result = as_result(
                await asyncio.wait_for(asyncio.shield(memory_task), timeout=wait_budget_s)
            )
            fact_text = fact_result.to_text() if fact_result is not None else ""
            # A degraded result means the store was unreachable or the budget
            # blew inside the service — an empty answer we must not freeze.
            fact_settled = fact_result is not None and not fact_result.degraded
            # Stash the structured recall on the task so the workflow can emit a
            # retrieval trace event without re-running the search. Attribution
            # needs ids/ranks/scores that the rendered text has already lost.
            if fact_result is not None:
                set_last_retrieval(memory_task, fact_result)
        except asyncio.TimeoutError:
            if not memory_task.done():
                memory_state.process_state().retrieval_states[memory_task] = "timed_out_running"
            logger.info(
                "[memory] fact retrieval still running past wait window, skipping injection"
            )
            # ``shield`` makes the continue-in-background policy explicit. The
            # done callback retains the structured result and changes the
            # observable state to completed_after_timeout.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[memory] fact retrieval await failed: %s", exc)

    if not profile_md and not fact_text:
        return FrozenMemoryBlock(text="", settled=fact_settled)

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
    block = "\n".join(parts).strip()
    logger.info(
        "[memory] frozen block built user=%s ws=%s chars=%d profile=%d facts=%d settled=%s",
        user_id,
        workspace_id,
        len(block),
        len(profile_md or ""),
        len(fact_text or ""),
        fact_settled,
    )
    return FrozenMemoryBlock(text=block, settled=fact_settled)


def save_memories_background(
    user_id: str,
    user_message: str,
    full_response: str,
    write_enabled: bool,
    *,
    workspace_id: str = "default",
    chat_id: Optional[str] = None,
    scope_user_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """Delegate to the unified post-response pipeline — never await; SSE is closed and the user isn't waiting.

    **When the user has not explicitly consented to writes (`write_enabled=False`), skip
    immediately and create no tasks at all**. This is the first gate of the user-level write
    switch; `schedule_post_response_tasks` has a second one inside.

    Inside `schedule_post_response_tasks`:
    - a durable outbox row is committed before the function returns
    - leased workers resume pending/retry rows after process restart
    - global Semaphore bounds worker concurrency (default 8)
    - runs 0-5 extractors picked by the router's substance floor + LLM write gate
    - each extractor has its own 30s timeout
    - sanitize → write L1/L2/Session → audit

    ``scope_user_id`` optionally selects an edition-owned shared memory bucket.
    ``user_id`` remains the real user for audit metadata.
    """
    if not (write_enabled and full_response and user_id):
        report_no_memory_writes(message_id)
        return

    try:
        from core.memory.pipeline import schedule_post_response_tasks
    except Exception as exc:
        logger.warning("[memory] pipeline unavailable, skipping save: %s", exc)
        report_no_memory_writes(message_id)
        return

    ctx = MemoryContext(
        user_id=user_id,
        workspace_id=workspace_id,
        chat_id=chat_id,
        write_enabled=write_enabled,
        scope_user_id=scope_user_id,
        message_id=message_id,
    )
    try:
        schedule_post_response_tasks(ctx, user_message, full_response)
    except Exception as exc:
        logger.warning("[memory] schedule_post_response_tasks failed: %s", exc)
        report_no_memory_writes(message_id, failed=True)


def report_no_memory_writes(message_id: Optional[str], *, failed: bool = False) -> None:
    """Tell the turn's settlement that no write pipeline will report for it.

    Every path that declines to schedule the pipeline must say so, otherwise the
    turn's card waits out the settlement watchdog for nothing.
    """
    if not message_id:
        return
    try:
        from core.evolution.settlement.settlement_runner import report_memory_writes

        report_memory_writes(message_id, items=[], failed=failed)
    except Exception as exc:  # noqa: BLE001 - never break the response path
        logger.debug("[memory] settlement report skipped: %s", exc)
