"""Commit successful chat output within the run journal transaction."""

from __future__ import annotations

import asyncio
import functools
import time
import typing as _typing

import core.chat.runtime.compaction as compaction
import core.chat.runtime.followups as followups
import core.chat.runtime.handoff as handoff
import core.runs.events.writer as writer
import core.chat.service as services_ChatService_module
from core.chat.context import now_iso
from core.infra.logging import get_logger
from core.llm.runtime import workspace as _workspace_mod
from core.files.artifacts.service import persist_artifacts as _persist_artifacts


from core.chat.runtime import snapshots

logger = get_logger(__name__)


async def complete_turn(turn, chunk):
    snapshots._flush_thinking(turn)
    internal_budget = chunk.get("_compaction_budget_inputs")
    if isinstance(internal_budget, dict):
        turn.compaction_budget_inputs = dict(internal_budget)
    # Strict workspace gate: pinned list is the sole source of
    # user-visible artifacts. See chats.py:_stream_sse_response.
    _ws_pinned = _workspace_mod.get_pinned()
    _ws_files = _workspace_mod.get_pinned_file_ids()
    usage_payload = chunk.get("usage") or None
    context_usage_payload = chunk.get("context_usage") or None
    compaction_pending = not chunk.get(
        "manual_compaction"
    ) and compaction._should_schedule_compaction(
        model_name=turn.model_name,
        usage=usage_payload,
        budget_inputs=turn.compaction_budget_inputs,
    )
    # 本轮总耗时：同一个值既下发给正在看的这一页，也写进 extra_data 供历史
    # 加载使用。以前只写库不下发，前端只能自己按「占位气泡创建到现在」估一个，
    # 把网络往返和排队也算了进去 —— 于是同一条回答，实时看到的耗时和刷新后
    # 从历史读出来的对不上。
    _duration_ms = int((time.monotonic() - turn.run_started_monotonic) * 1000)
    turn.metadata = {
        "type": "meta",
        "duration_ms": _duration_ms,
        "route": chunk.get("route", "main"),
        "sources": chunk.get("sources", []),
        "artifacts": _ws_pinned,
        "warnings": chunk.get("warnings", []),
        "is_markdown": chunk.get("is_markdown", False),
        "chat_id": turn.chat_id,
        "message_id": turn.current_message_id,
        "citations": chunk.get("citations", []),
        "workspace_files": _ws_files,
        "context_usage": context_usage_payload,
        "compaction_pending": compaction_pending,
        "ontology_governance": chunk.get("ontology_governance"),
        # Skeleton marker: settlement runs after the stream closes.
        "evolution_pending": chunk.get("evolution_pending"),
    }
    await turn.emit(turn.metadata)

    # Persist: assistant message + artifacts
    _persist_extra = {
        "timestamp": now_iso(),
        "route": turn.metadata.get("route"),
        "is_markdown": turn.metadata.get("is_markdown", False),
        "sources": turn.metadata.get("sources", []),
        "artifacts": turn.metadata.get("artifacts", []),
        "warnings": turn.metadata.get("warnings", []),
        "citations": turn.metadata.get("citations", []),
        "message_id": turn.current_message_id,
        "workspace_files": _ws_files,
        "duration_ms": _duration_ms,
        "segments": turn.segments.payload(),
    }
    if isinstance(context_usage_payload, dict):
        _persist_extra["context_usage"] = context_usage_payload
    if turn.metadata.get("ontology_governance"):
        _persist_extra["ontology_governance"] = turn.metadata["ontology_governance"]
    if turn.context.get("model_provider_id"):
        _persist_extra["model_provider_id"] = turn.context.get("model_provider_id")
    turn.journal.save_snapshot(
        turn.run_id,
        owner=turn.owner,
        phase="model_completed",
        safety="replayable",
        snapshot=writer._json_safe(
            {
                "assistant_content": turn.full_response,
                "message_id": turn.current_message_id,
                "model_name": turn.model_name,
                "thinking": snapshots._thinking_payload(turn),
                "tool_calls": turn.tool_calls_log if turn.tool_calls_log else None,
                "model_steps": snapshots._model_steps_payload(turn),
                "usage": usage_payload,
                "extra_data": _persist_extra,
                "artifacts": _ws_pinned,
                "context": turn.context,
            }
        ),
    )

    queued_handoff: _typing.Dict[str, _typing.Any] = {}

    def _commit_output(db) -> None:
        chat_service = services_ChatService_module.ChatService(db)
        # The row already exists (created at admission, refreshed
        # while streaming); this is the final write that also drops
        # the in-flight marker because ``_persist_extra`` never
        # carries it.
        chat_service.upsert_message(
            chat_id=turn.chat_id,
            role="assistant",
            content=turn.full_response,
            model=turn.model_name,
            thinking=snapshots._thinking_payload(turn),
            tool_calls=turn.tool_calls_log if turn.tool_calls_log else None,
            model_steps=snapshots._model_steps_payload(turn),
            usage=usage_payload,
            message_id=turn.current_message_id,
            chat_seq=turn.current_chat_seq,
            extra_data=_persist_extra,
            invalidate_mid_turn_checkpoint=compaction_pending,
            commit=False,
        )
        # Build a ProjectScope from the workflow context and pass it
        # explicitly so pinned files keep their project ownership.
        from core.projects.edition import project_scope_from_context

        _persist_artifacts(
            db,
            turn.user_id,
            turn.chat_id,
            _ws_pinned,
            scope=project_scope_from_context(turn.context),
            commit=False,
        )
        prepared = handoff._commit_queued_handoff_in_session(
            db,
            source_run_id=turn.run_id,
            chat_id=turn.chat_id,
            user_id=turn.user_id,
            context=turn.context,
            model_name=turn.model_name,
        )
        if prepared is not None:
            queued_handoff.update(prepared)

    # Offloaded to a worker thread on purpose. This is the one
    # journal call that commits the turn's real side effects (the
    # assistant message, pinned artifacts, a queued handoff), so it
    # is the one that can sit waiting on a row lock. ``complete``
    # is synchronous SQLAlchemy; calling it inline blocks the event
    # loop, and a blocked loop cannot run the coroutine holding the
    # conflicting transaction — the process deadlocks and every
    # request, stream and health probe stops answering. In a thread
    # a lock wait costs this run its latency and nothing else.
    # ``_commit_output`` and everything it calls are plain sync
    # code, so they are safe to run off the loop.
    completed = await asyncio.to_thread(
        functools.partial(
            turn.journal.complete,
            turn.run_id,
            owner=turn.owner,
            status="completed",
            usage=usage_payload,
            last_event_offset=turn.offsets.last,
            commit_effect=_commit_output,
            committed_operation={
                "operation_type": "message_committed",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": turn.current_message_id},
            },
        )
    )
    if not completed:
        logger.warning(
            "chat_run_terminal_cas_lost",
            run_id=turn.run_id,
            owner=turn.owner,
        )
        turn.stopped = True
        return

    # The DB lease has intentionally ended. Stop the heartbeat
    # before scheduling the handoff and projecting terminal events;
    # otherwise its next renew would interpret normal completion as
    # lease loss and cancel this coroutine mid-handoff.
    turn.lease_task.cancel()

    if queued_handoff:
        handoff._schedule_queued_handoff(
            queued_handoff,
            chat_id=turn.chat_id,
            user_id=turn.user_id,
        )
        await turn.emit(
            handoff._queued_run_started_event(turn.run_id, turn.chat_id, queued_handoff),
            terminal=True,
        )

    if not chunk.get("manual_compaction"):
        followups._spawn_followup_task(
            run_id=turn.run_id,
            chat_id=turn.chat_id,
            user_msg=turn.latest_user_message,
            response=turn.full_response,
            msg_id=turn.current_message_id,
        )

    # End-of-turn compaction: when real token usage crosses the
    # threshold → generate a summary checkpoint in the background;
    # doesn't block stream close, and failures don't affect the main
    # conversation.
    compaction._spawn_compaction_task(
        run_id=turn.run_id,
        chat_id=turn.chat_id,
        model_name=turn.model_name,
        usage=usage_payload,
        budget_inputs=turn.compaction_budget_inputs,
        should_schedule=compaction_pending,
        message_id=turn.current_message_id,
    )
