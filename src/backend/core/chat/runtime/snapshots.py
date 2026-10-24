"""Checkpoint visible turn output and preserve partial output on cancellation."""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Dict, List, Optional

import core.chat.inflight as inflight
import core.chat.message_parser as message_parser
import core.db.engine as engine
import core.llm.runtime.model_steps as model_steps
import core.runs.events.writer as writer
import core.chat.service as services_ChatService_module
from core.chat.context import now_iso
from core.infra.text_bounds import bound_result_for_history
from core.infra.logging import get_logger
from core.llm.runtime import workspace as _workspace_mod
from core.files.artifacts.service import persist_artifacts as _persist_artifacts

logger = get_logger(__name__)


def _flush_thinking(turn) -> None:
    if not turn.thinking_parts:
        return
    block = "".join(turn.thinking_parts)
    turn.thinking_parts.clear()
    # 结构化 reasoning 的尾部增量可能在正文已开始后才到达（正文首 token
    # 已出、思考收尾的"。"后到）。实时侧把它并回前一个思考块
    # （appendThinkingContentBeforeTrailingText），落库遵循同一规则——
    # 否则思考块会把正文句子从中间切开，刷新后与实时展示不一致。
    if turn.round_block is not None:
        turn.round_block["content"] += block
        return
    turn.round_block = {"content": block}
    turn.thinking_log.append(turn.round_block)
    turn.segments.add_thinking(len(turn.thinking_log) - 1)


def _thinking_payload(turn) -> Optional[List[Dict[str, Any]]]:
    blocks = [b for b in turn.thinking_log if b.get("content")]
    return blocks or None


def _model_steps_payload(turn) -> Optional[List[Dict[str, Any]]]:
    return list(turn.model_steps) or None


def _turn_extra(turn, **more: Any) -> Dict[str, Any]:
    return {
        "timestamp": now_iso(),
        "message_id": turn.current_message_id,
        "run_id": turn.run_id,
        "is_markdown": message_parser.looks_markdown(turn.full_response),
        **more,
    }


def _write_inflight_row(turn, snapshot: Dict[str, Any]) -> bool:
    with engine.SessionLocal() as db:
        owned = services_ChatService_module.ChatService(db).refresh_streaming_message(
            run_id=turn.run_id,
            owner=turn.owner,
            message_id=snapshot["message_id"],
            content=snapshot["content"],
            thinking=snapshot["thinking"],
            tool_calls=snapshot["tool_calls"],
            model_steps=snapshot["model_steps"],
            extra_data={
                inflight.IN_FLIGHT_KEY: inflight.mark(
                    turn.run_id, "streaming", event_offset=snapshot["event_offset"]
                ),
                "message_id": snapshot["message_id"],
                "run_id": turn.run_id,
                "is_markdown": message_parser.looks_markdown(snapshot["content"]),
                "segments": snapshot["segments"],
            },
        )
        db.commit()
        return owned


async def _flush_inflight(turn) -> bool:
    _flush_thinking(turn)
    if not (turn.full_response or turn.tool_calls_log or turn.model_steps or turn.thinking_log):
        return True
    async with turn.flush_lock:
        # Canonical steps can advance without a user-visible SSE event.
        snapshot_key = (turn.current_message_id, turn.offsets.last, len(turn.model_steps))
        if snapshot_key == turn.flushed_snapshot_key:
            return True
        # Tool results go in at history size; the read path bounds them the
        # same way, and the final write carries the full payload.
        snapshot = writer._json_safe(
            {
                "content": turn.full_response,
                "thinking": _thinking_payload(turn),
                "model_steps": _model_steps_payload(turn),
                "tool_calls": [
                    (
                        {**tc, "result": bound_result_for_history(tc["result"])[0]}
                        if "result" in tc
                        else tc
                    )
                    for tc in turn.tool_calls_log
                ]
                or None,
                "segments": turn.segments.snapshot(),
                "message_id": turn.current_message_id,
                "event_offset": turn.offsets.last,
            }
        )
        try:
            if not await asyncio.to_thread(functools.partial(_write_inflight_row, turn), snapshot):
                logger.warning("chat_run_inflight_fenced", run_id=turn.run_id, owner=turn.owner)
                return False
        except Exception:  # noqa: BLE001 - a missed checkpoint must not fail the run
            logger.warning("chat_run_inflight_persist_failed", run_id=turn.run_id, exc_info=True)
            return False  # Keep dirty so the periodic checkpoint retries this snapshot.
        turn.flushed_snapshot_key = snapshot_key
        return True


async def _checkpoint_loop(turn) -> None:
    while True:
        await asyncio.sleep(turn.inflight_interval_s)
        await _flush_inflight(turn)


def _commit_cancelled_partial(turn) -> None:
    """把用户按下停止那一刻已产出的正文与工具卡片落库。

    cancel_run 先写终态并清租约，之后 journal.complete 的 CAS 必然失败，
    worker 再也借不到 commit_effect 提交本轮输出 —— 所以这里直接写库。
    message_id 固定，用 upsert 保证重复取消 / 恢复不会留下两条。
    """
    _flush_thinking(turn)
    if not (turn.full_response or turn.tool_calls_log or turn.model_steps or turn.thinking_log):
        return
    # 停下来时还没回结果的工具：不标记的话前端按"缺 status 即成功"渲染，
    # 刷新后一张没跑完的卡片会显示成执行成功。模型侧的记录同样要配平：
    # 悬空的调用补一条"被中断"的结果，下一轮模型看到的是事实而不是半步。
    for _tc in turn.tool_calls_log:
        if "result" not in _tc and not _tc.get("status"):
            _tc["status"] = "interrupted"
    model_steps.close_dangling_calls(turn.model_steps)
    _ws_pinned = _workspace_mod.get_pinned()
    _extra = {
        "timestamp": now_iso(),
        "is_markdown": bool(
            "\n" in turn.full_response or "```" in turn.full_response or "**" in turn.full_response
        ),
        "message_id": turn.current_message_id,
        "run_id": turn.run_id,
        "cancelled": True,
        "artifacts": _ws_pinned,
        "workspace_files": _workspace_mod.get_pinned_file_ids(),
        "duration_ms": int((time.monotonic() - turn.run_started_monotonic) * 1000),
        "segments": turn.segments.payload(),
    }
    if turn.context.get("model_provider_id"):
        _extra["model_provider_id"] = turn.context.get("model_provider_id")
    from core.projects.edition import project_scope_from_context

    with engine.SessionLocal() as db:
        chat_service = services_ChatService_module.ChatService(db)
        chat_service.upsert_message(
            chat_id=turn.chat_id,
            role="assistant",
            content=turn.full_response,
            model=turn.model_name,
            thinking=_thinking_payload(turn),
            tool_calls=turn.tool_calls_log if turn.tool_calls_log else None,
            model_steps=_model_steps_payload(turn),
            message_id=turn.current_message_id,
            chat_seq=turn.current_chat_seq,
            extra_data=_extra,
            commit=False,
        )
        _persist_artifacts(
            db,
            turn.user_id,
            turn.chat_id,
            _ws_pinned,
            scope=project_scope_from_context(turn.context),
            commit=False,
        )
        db.commit()


def _persist_cancelled_partial(turn) -> None:
    try:
        _commit_cancelled_partial(turn)
    except Exception:  # noqa: BLE001 - 落库失败不能吞掉取消收尾
        logger.warning(
            "chat_run_cancelled_partial_persist_failed",
            run_id=turn.run_id,
            exc_info=True,
        )
