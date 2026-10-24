"""Apply workflow events to chat output and durable projections."""

from __future__ import annotations

import core.llm.runtime.model_steps as model_steps
from core.infra.text_bounds import bound_result_for_display
from core.chat.tool_log import (
    attach_subagent_step,
    build_thinking_event,
    build_tool_call_delta_event,
    build_tool_call_event,
    build_tool_call_start_event,
    build_tool_result_event,
    build_user_question_event,
    build_user_question_resolved_event,
)
from core.infra.logging import get_logger


from core.chat.runtime import completion, snapshots, steering

logger = get_logger(__name__)


async def consume_chunk(turn, chunk):
    chunk_type = chunk.get("type")
    if chunk_type == "model_dispatch":
        turn.journal.append_operation(
            turn.run_id,
            owner=turn.owner,
            operation_type="model_dispatch",
            phase="model_inflight",
            safety="replayable",
        )
    elif chunk_type == "thinking":
        _thinking_evt = build_thinking_event(chunk, turn.chat_id)
        if _thinking_evt.get("delta"):
            turn.thinking_parts.append(str(_thinking_evt["delta"]))
        await turn.emit(_thinking_evt)
    elif chunk_type in {"ai_message", "content"}:
        delta = chunk.get("delta", "")
        if delta:
            snapshots._flush_thinking(turn)
            turn.full_response += delta
            turn.segments.add_text(delta)
            await turn.emit(
                {"type": "content", "event": "ai_message", "delta": delta, "chat_id": turn.chat_id}
            )
    elif chunk_type == "content_replace":
        replacement = str(chunk.get("content") or "")
        turn.thinking_parts.clear()
        turn.full_response = replacement
        turn.round_block = None
        turn.thinking_log.clear()
        model_steps.replace_final_text(turn.model_steps, replacement)
        turn.segments.reset(preserve_compaction=True)
        turn.segments.add_text(replacement)
        await turn.emit(
            {
                "type": "content_replace",
                "content": replacement,
                "reason": chunk.get("reason"),
                "chat_id": turn.chat_id,
            }
        )
        await snapshots._flush_inflight(turn)
    elif chunk_type == "steer_applied":
        await steering.apply_steer(turn, chunk)
    elif chunk_type == "tool_call":
        snapshots._flush_thinking(turn)
        turn.round_block = None
        _tc_evt = build_tool_call_event(chunk, turn.chat_id, turn.tool_calls_log)
        turn.segments.add_tools(turn.tool_calls_log)
        await turn.emit(_tc_evt)
        await snapshots._flush_inflight(turn)
    elif chunk_type == "tool_call_start":
        snapshots._flush_thinking(turn)
        turn.round_block = None
        await turn.emit(build_tool_call_start_event(chunk, turn.chat_id))
    elif chunk_type == "tool_call_delta":
        await turn.emit(build_tool_call_delta_event(chunk, turn.chat_id))
    elif chunk_type == "tool_progress":
        # Live status for a running tool card; the result supersedes it, so it is not persisted.
        await turn.emit({**chunk, "chat_id": turn.chat_id})
    elif chunk_type == "tool_result":
        step = chunk.get("model_step")
        if isinstance(step, dict):
            turn.model_steps.append(step)
        _tr_evt = build_tool_result_event(chunk, turn.chat_id, turn.tool_calls_log)
        turn.segments.add_tools(turn.tool_calls_log)
        if isinstance(step, dict):
            if not await snapshots._flush_inflight(turn) and (
                not await snapshots._flush_inflight(turn)
            ):
                raise RuntimeError("Could not persist completed tool result")
        await turn.emit(_tr_evt)
        await snapshots._flush_inflight(turn)
    elif chunk_type == "model_step":
        step = chunk.get("step")
        if isinstance(step, dict):
            turn.model_steps.append(step)
            if step.get("kind") == "tool_result":
                if not await snapshots._flush_inflight(turn) and (
                    not await snapshots._flush_inflight(turn)
                ):
                    raise RuntimeError("Could not persist completed tool result")
            else:
                await snapshots._flush_inflight(turn)
    elif chunk_type == "compaction_progress":
        turn.segments.add_compaction(chunk)
        await turn.emit(chunk)
    elif chunk_type == "context_usage":
        await turn.emit({**chunk, "chat_id": turn.chat_id})
    elif chunk_type in ("heartbeat", "model_progress"):
        return
    elif chunk_type == "tool_pending":
        pending_event = {
            "type": "tool_pending",
            "chat_id": turn.chat_id,
            "reason": chunk.get("reason", "llm_buffering"),
        }
        if chunk.get("scope"):
            pending_event["scope"] = chunk["scope"]
        await turn.emit(pending_event)
    elif chunk_type == "subagent_event":
        try:
            attach_subagent_step(
                turn.tool_calls_log, str(chunk.get("parent_tool_id", "") or ""), chunk
            )
        except Exception:
            logger.debug("attach_subagent_step failed (ignored)", exc_info=True)
        chunk["chat_id"] = turn.chat_id
        await turn.emit(bound_result_for_display(chunk)[0])
    elif chunk_type == "file_confirm":
        await turn.emit(
            {
                "type": "file_confirm",
                "chat_id": turn.chat_id,
                "confirm_id": chunk.get("confirm_id"),
                "op": chunk.get("op"),
                "logical_path": chunk.get("logical_path"),
                "message": chunk.get("message"),
                "expired": chunk.get("expired", False),
            }
        )
    elif chunk_type == "design_pick":
        await turn.emit(
            {
                "type": "design_pick",
                "chat_id": turn.chat_id,
                "confirm_id": chunk.get("confirm_id"),
                "question": chunk.get("question", ""),
                "options": chunk.get("options", []),
                "message": chunk.get("message"),
                "expired": chunk.get("expired", False),
            }
        )
    elif chunk_type == "user_question":
        await turn.emit(build_user_question_event(chunk, turn.chat_id))
    elif chunk_type == "user_question_resolved":
        await turn.emit(build_user_question_resolved_event(chunk, turn.chat_id))
    elif chunk_type == "plan_update":
        await turn.emit(
            {
                "type": "plan_update",
                "chat_id": turn.chat_id,
                "title": chunk.get("title", ""),
                "steps": chunk.get("steps", []),
            }
        )
    elif chunk_type in {
        "ontology_activation",
        "ontology_gate",
        "ontology_review",
        "ontology_repair",
        "ontology_revision",
        "ontology_revision_thinking",
    }:
        ontology_event = dict(chunk)
        ontology_event["chat_id"] = turn.chat_id
        await turn.emit(bound_result_for_display(ontology_event)[0])
    elif chunk_type == "meta":
        await completion.complete_turn(turn, chunk)
