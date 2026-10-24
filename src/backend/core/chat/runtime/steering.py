"""Apply queued steering while preserving ordered message and event commits."""

from __future__ import annotations

import time
import uuid

import core.chat.inflight as inflight
import core.chat.runtime.handoff as handoff
import core.db.models as models
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.chat.service as services_ChatService_module
from core.chat.context import now_iso
from core.infra.logging import get_logger


from core.chat.runtime import snapshots

logger = get_logger(__name__)


async def apply_steer(turn, chunk):
    snapshots._flush_thinking(turn)
    steer_id = str(chunk.get("steer_id") or "")[:64]
    steer_queue_id = str(chunk.get("queue_id") or "")[:64]
    steer_claim_owner = str(chunk.get("claim_owner") or "")[:160]
    steer_seq = int(chunk.get("steer_seq") or 0)
    steer_message = str(chunk.get("message") or "").strip()
    steer_message_id = (
        f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{turn.run_id}:{steer_id}').hex[:16]}"
        if steer_id
        else f"msg_{uuid.uuid4().hex[:16]}"
    )
    next_assistant_message_id = (
        f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{turn.run_id}:{steer_id}:assistant').hex[:16]}"
        if steer_id
        else f"msg_{uuid.uuid4().hex[:16]}"
    )
    had_assistant_output = bool(turn.full_response or turn.tool_calls_log)
    if steer_message:
        # Close the visible assistant segment before inserting the
        # user's steer. Persisting in this order is what keeps a
        # refresh from moving every mid-run user message above the
        # whole assistant response.
        def _commit_steer(db) -> None:
            if steer_queue_id and steer_claim_owner:
                from core.chat.steer_queue import SteerQueue

                owned_run = db.get(models.ChatRun, turn.run_id)
                SteerQueue.mark_applied_in_session(
                    db,
                    queue_id=steer_queue_id,
                    claim_owner=steer_claim_owner,
                    applied_run_id=turn.run_id,
                    applied_operation_seq=int(owned_run.operation_seq or 0) + 1,
                )
            else:
                owned_run = db.get(models.ChatRun, turn.run_id)
            chat_service = services_ChatService_module.ChatService(db)
            if had_assistant_output:
                chat_service.upsert_message(
                    chat_id=turn.chat_id,
                    role="assistant",
                    content=turn.full_response,
                    model=turn.model_name,
                    thinking=snapshots._thinking_payload(turn),
                    tool_calls=turn.tool_calls_log if turn.tool_calls_log else None,
                    model_steps=snapshots._model_steps_payload(turn),
                    message_id=turn.current_message_id,
                    chat_seq=turn.current_chat_seq,
                    extra_data={
                        "timestamp": now_iso(),
                        "is_markdown": bool(
                            "\n" in turn.full_response
                            or "```" in turn.full_response
                            or "**" in turn.full_response
                        ),
                        "message_id": turn.current_message_id,
                        "run_id": turn.run_id,
                        "steer_segment": True,
                        "duration_ms": int((time.monotonic() - turn.run_started_monotonic) * 1000),
                        "segments": turn.segments.payload(),
                    },
                    commit=False,
                )
            # Persist once the middleware has actually injected the
            # instruction. A deterministic id makes replay safe.
            chat_service.upsert_message(
                chat_id=turn.chat_id,
                role="user",
                content=steer_message,
                message_id=steer_message_id,
                extra_data={
                    "timestamp": now_iso(),
                    "steer": True,
                    "run_id": turn.run_id,
                    "steer_id": steer_id,
                    "steer_queue_id": steer_queue_id or None,
                    "steer_seq": steer_seq or None,
                },
                commit=False,
            )
            # The next segment's row exists before its first token,
            # like every other admitted turn.
            db.add(
                inflight.new_assistant_row(
                    message_id=next_assistant_message_id,
                    chat_id=turn.chat_id,
                    chat_seq=None,
                    run_id=turn.run_id,
                    model=turn.model_name,
                    created_at=clock._utcnow(),
                )
            )
            # The durable queue acknowledgement and the exact
            # post-steer prompt must share one transaction. If this
            # worker disappears before the next model snapshot, a
            # replacement worker resumes from this context instead
            # of losing an already-applied instruction.
            post_steer_messages = handoff._persisted_history(chat_service, turn.chat_id)
            post_steer_context = writer._json_safe(dict(turn.context or {}))
            post_steer_context.pop("journal_owner", None)
            post_steer_context.update(
                {
                    "run_id": turn.run_id,
                    "message_id": next_assistant_message_id,
                    "chat_id": turn.chat_id,
                    "user_id": turn.user_id,
                }
            )
            owned_run.recovery_snapshot = {
                "kind": "chat",
                "worker_args": {
                    "session_messages": post_steer_messages,
                    "effective_user_message": steer_message,
                    "raw_user_message": steer_message,
                    "context": post_steer_context,
                    "model_name": turn.model_name,
                },
            }
            owned_run.snapshot_version = int(owned_run.snapshot_version or 0) + 1

        turn.journal.append_operation(
            turn.run_id,
            owner=turn.owner,
            operation_type="steer_messages_committed",
            phase="steer_committed",
            safety="side_effect_committed",
            payload={
                "steer_id": steer_id,
                "queue_id": steer_queue_id or None,
                "steer_seq": steer_seq or None,
                "message_id": steer_message_id,
                "previous_assistant_message_id": (
                    turn.current_message_id if had_assistant_output else None
                ),
            },
            commit_effect=_commit_steer,
        )
        turn.latest_user_message = steer_message
    await turn.emit(
        {
            "type": "steer_applied",
            "chat_id": turn.chat_id,
            "run_id": turn.run_id,
            "steer_id": steer_id,
            "queue_id": steer_queue_id or None,
            "steer_seq": steer_seq or None,
            "message": steer_message,
            "message_id": steer_message_id,
            "previous_assistant_message_id": (
                turn.current_message_id if had_assistant_output else None
            ),
            "next_assistant_message_id": next_assistant_message_id,
            "had_assistant_output": had_assistant_output,
        }
    )
    turn.current_message_id = next_assistant_message_id
    turn.current_chat_seq = None
    # The workflow reads this dict again when it emits the final
    # evolution/memory settlement marker, so keep that marker bound
    # to the post-steer assistant segment too.
    turn.context["message_id"] = turn.current_message_id
    turn.full_response = ""
    turn.tool_calls_log = []
    turn.model_steps = []
    turn.thinking_parts.clear()
    turn.round_block = None
    turn.thinking_log.clear()
    turn.segments.reset()
    turn.metadata = {}
    try:
        from core.harness.log_context import set_current_message_id

        set_current_message_id(turn.current_message_id)
    except Exception as fallback_error:  # pragma: no cover - logging must never fail a run
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.chat.runtime.steering:apply_steer', fallback_error)
