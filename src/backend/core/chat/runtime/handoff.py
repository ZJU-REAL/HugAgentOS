"""Commit queued chat instructions and hand off to the next durable turn."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import core.chat.inflight as inflight
import core.chat.runtime.worker as worker
import core.db.engine as engine
import core.db.models as models
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.chat.service as services_ChatService_module
import core.runs.journal as run_journal
from core.db.models import ChatRun
from core.infra.logging import get_logger
from core.chat.service import ChatService

logger = get_logger(__name__)


def _persisted_history(chat_service: ChatService, chat_id: str) -> List[Dict[str, Any]]:
    """The model-facing history a successor or recovery run starts from.

    Read back from the rows already committed in this transaction — the step
    record included — never rebuilt from text the source run held in memory.
    """
    from core.chat.compaction.service import _load_history

    return writer._json_safe(_load_history(chat_service, chat_id, repair=False))


def _commit_queued_handoff_in_session(
    db,
    *,
    source_run_id: str,
    chat_id: str,
    user_id: str,
    context: Dict[str, Any],
    model_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Atomically consume one terminal queued item and stage its ChatRun.

    This includes a steer accepted after the source run crossed its final safe
    model/tool boundary. Such an instruction can no longer enter the source
    ReAct loop, so completion hands it to a successor instead of stranding it.

    The successor's history is read back from the rows this transaction just
    committed — the step record included — never rebuilt from the text the
    source run happened to hold in memory.
    """
    from core.db.models.chat import reserve_chat_sequences
    from core.chat.steer_queue import SteerQueue

    source_run = db.get(models.ChatRun, source_run_id)
    if source_run is None:
        return None
    root_run_id = str(context.get("handoff_root_run_id") or source_run_id)
    handoff = SteerQueue.consume_next_handoff_in_session(
        db,
        source_run_id=source_run_id,
        root_run_id=root_run_id,
        chat_id=chat_id,
        applied_operation_seq=int(source_run.operation_seq or 0) + 1,
    )
    if handoff is None or not handoff.applied_run_id:
        return None

    first_seq = reserve_chat_sequences(
        db,
        chat_id,
        count=2,
        owner_user_id=user_id,
    )
    next_run_id = handoff.applied_run_id
    next_message_id = (
        f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{handoff.queue_id}:assistant').hex[:16]}"
    )
    queued_user_message_id = (
        f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{handoff.queue_id}:user').hex[:16]}"
    )
    next_context = writer._json_safe(dict(context or {}))
    next_context.pop("journal_owner", None)
    next_context.update(
        {
            "run_id": next_run_id,
            "message_id": next_message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "handoff_queue_id": handoff.queue_id,
            "handoff_delivery_mode": handoff.delivery_mode,
            "handoff_root_run_id": root_run_id,
        }
    )
    next_session_messages = _persisted_history(services_ChatService_module.ChatService(db), chat_id)
    next_session_messages.append({"role": "user", "content": handoff.message})
    worker_args = {
        "session_messages": next_session_messages,
        "effective_user_message": handoff.message,
        "raw_user_message": handoff.message,
        "context": next_context,
        "model_name": model_name,
    }
    request_payload = dict(source_run.request_payload or {})
    request_payload.update(
        {
            "message": handoff.message,
            "chat_id": chat_id,
            "attachments": [],
            "handoff": {
                "queue_id": handoff.queue_id,
                "steer_id": handoff.steer_id,
                "delivery_mode": handoff.delivery_mode,
                "source_run_id": source_run_id,
            },
        }
    )
    run_journal.RunJournal.accept_in_session(
        db,
        run_id=next_run_id,
        message_id=next_message_id,
        chat_id=chat_id,
        user_id=user_id,
        request_payload=writer._json_safe(request_payload),
        recovery_snapshot={"kind": "chat", "worker_args": writer._json_safe(worker_args)},
        user_message_id=queued_user_message_id,
        user_chat_seq=first_seq,
        assistant_chat_seq=first_seq + 1,
        writer_slot="main",
    )
    services_ChatService_module.ChatService(db).upsert_message(
        chat_id=chat_id,
        role="user",
        content=handoff.message,
        model=model_name,
        message_id=queued_user_message_id,
        chat_seq=first_seq,
        extra_data={
            "queued_handoff": True,
            "steer_queue_id": handoff.queue_id,
            "steer_id": handoff.steer_id,
            "steer_seq": handoff.steer_seq,
            "delivery_mode": handoff.delivery_mode,
            "source_run_id": source_run_id,
            "run_id": next_run_id,
        },
        commit=False,
    )
    db.add(
        inflight.new_assistant_row(
            message_id=next_message_id,
            chat_id=chat_id,
            chat_seq=first_seq + 1,
            run_id=next_run_id,
            model=model_name,
            created_at=clock._utcnow(),
        )
    )
    db.flush()
    return {
        "run_id": next_run_id,
        "message_id": next_message_id,
        "user_message_id": queued_user_message_id,
        "assistant_chat_seq": first_seq + 1,
        "queue_id": handoff.queue_id,
        "steer_id": handoff.steer_id,
        "steer_seq": handoff.steer_seq,
        "delivery_mode": handoff.delivery_mode,
        **worker_args,
    }


def _schedule_queued_handoff(
    handoff: Dict[str, Any],
    *,
    chat_id: str,
    user_id: str,
    task_prefix: str = "chat_run_handoff",
) -> str:
    """Start a committed handoff; pending DB state remains recovery-safe on failure."""
    next_run_id = str(handoff["run_id"])
    next_owner = ownership.new_worker_owner(next_run_id)
    runtime.register_run_task(
        next_run_id,
        worker._run_workflow(
            run_id=next_run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(handoff["message_id"]),
            assistant_chat_seq=(
                int(handoff["assistant_chat_seq"])
                if handoff.get("assistant_chat_seq") is not None
                else None
            ),
            session_messages=list(handoff.get("session_messages") or []),
            effective_user_message=str(handoff.get("effective_user_message") or ""),
            raw_user_message=str(handoff.get("raw_user_message") or ""),
            context=dict(handoff.get("context") or {}),
            model_name=(str(handoff["model_name"]) if handoff.get("model_name") else None),
            journal_owner=next_owner,
        ),
        name=f"{task_prefix}:{next_run_id}",
    )
    return next_run_id


def _queued_run_started_event(
    source_run_id: str,
    chat_id: str,
    handoff: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "queued_run_started",
        "chat_id": chat_id,
        "source_run_id": source_run_id,
        "run_id": str(handoff["run_id"]),
        "message_id": handoff["message_id"],
        "user_message_id": handoff["user_message_id"],
        "message": handoff["raw_user_message"],
        "queue_id": handoff["queue_id"],
        "steer_id": handoff["steer_id"],
        "steer_seq": handoff["steer_seq"],
        "delivery_mode": handoff["delivery_mode"],
    }


async def _write_queued_run_started_projection(
    source_run_id: str,
    *,
    chat_id: str,
    handoff: Dict[str, Any],
) -> None:
    """Best-effort event projection; the queue API is the durable backfill seam."""
    try:
        offset = ownership.journal().allocate_event_offset(source_run_id, terminal=True)
        await writer.xadd_event(
            source_run_id,
            offset,
            _queued_run_started_event(source_run_id, chat_id, handoff),
        )
    except Exception as exc:  # Redis projection must not undo the committed handoff
        logger.warning(
            "chat_run_handoff_projection_failed",
            run_id=source_run_id,
            child_run_id=handoff.get("run_id"),
            error=str(exc),
        )


def get_successor_run(run_id: str, *, user_id: str) -> Optional[ChatRun]:
    """Read the successor committed by the durable steering handoff."""
    from core.db.models import ChatSteerQueueItem

    with engine.SessionLocal() as db:
        source = db.get(models.ChatRun, run_id)
        if source is None or source.user_id != user_id:
            raise runtime.ChatRunPermissionDenied("run does not belong to user")
        successor_id = (
            db.query(ChatSteerQueueItem.applied_run_id)
            .filter(
                ChatSteerQueueItem.applied_source_run_id == run_id,
                ChatSteerQueueItem.applied_run_id != run_id,
                ChatSteerQueueItem.status == "applied",
                ChatSteerQueueItem.chat_id == source.chat_id,
                ChatSteerQueueItem.user_id == user_id,
            )
            .scalar()
        )
    return runtime.get_run(successor_id) if successor_id else None
