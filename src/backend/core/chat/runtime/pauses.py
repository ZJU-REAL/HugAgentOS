"""Persist tool-outcome and human-hook pauses without reporting success."""

from __future__ import annotations

import core.runs.runtime as runtime
import core.runs.settings as settings
from core.harness.hooks import HookPaused
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def _pause_for_tool_outcome(turn, exc: BaseException) -> bool:
    logger.warning("chat_run_tool_outcome_unknown", run_id=turn.run_id, effect_id=str(exc))
    if not turn.journal.needs_attention(
        turn.run_id,
        owner=turn.owner,
        reason=f"tool outcome pending policy recovery: {exc}",
    ):
        current = runtime.get_run(turn.run_id)
        if current is None or current.status != "needs_attention":
            logger.warning("chat_run_tool_unknown_cas_lost", run_id=turn.run_id, owner=turn.owner)
            return False
    await turn.emit(
        {
            "type": "error",
            "error": "工具调用结果无法安全确定，任务已暂停等待处理",
            "delta": "工具调用结果无法安全确定，任务已暂停等待处理",
            "chat_id": turn.chat_id,
        },
        terminal=True,
    )
    await turn.emit({"type": settings.TERMINAL_TYPE, "chat_id": turn.chat_id}, terminal=True)
    return True


async def _pause_for_hook(turn, exc: HookPaused) -> bool:
    logger.info("chat_run_hook_paused", run_id=turn.run_id, reason=str(exc))
    if not turn.journal.needs_attention(
        turn.run_id,
        owner=turn.owner,
        reason=f"hook requested pause: {exc}",
    ):
        current = runtime.get_run(turn.run_id)
        if current is None or current.status != "needs_attention":
            logger.warning("chat_run_hook_pause_cas_lost", run_id=turn.run_id, owner=turn.owner)
            return False
    await turn.emit(
        {
            "type": "error",
            "error": "任务已按执行策略暂停，等待后续处理",
            "delta": "任务已按执行策略暂停，等待后续处理",
            "chat_id": turn.chat_id,
        },
        terminal=True,
    )
    await turn.emit({"type": settings.TERMINAL_TYPE, "chat_id": turn.chat_id}, terminal=True)
    return True
