"""Admit a normal chat run and start its lease-owning worker."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import core.chat.runtime.worker as worker
import core.db.engine as engine
import core.db.models as models
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
from core.db.models import ChatRun
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def start_run(
    *,
    accepted_run: Optional[ChatRun] = None,
    chat_id: str,
    user_id: str,
    session_messages: List[Dict[str, Any]],
    effective_user_message: str,
    raw_user_message: str,
    context: Dict[str, Any],
    request_payload: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
) -> ChatRun:
    """Launch a ChatRun that ChatSequencer has already durably accepted.

    Acceptance (user message + pending run) must commit before this function is
    called.  Keeping launch separate makes a process crash between the two
    recoverable by the Run Journal in #64 instead of losing the request.
    """

    def persist_launch():
        recovery_snapshot = writer._json_safe(
            {
                "kind": "chat",
                "worker_args": {
                    "session_messages": session_messages,
                    "effective_user_message": effective_user_message,
                    "raw_user_message": raw_user_message,
                    "context": context,
                    "model_name": model_name,
                },
            }
        )
        if accepted_run is None:
            run = ownership.create_run_record(
                chat_id=chat_id,
                user_id=user_id,
                request_payload=dict(request_payload or {}),
                recovery_snapshot=recovery_snapshot,
            )
        else:
            run = accepted_run
            if run.chat_id != chat_id or run.user_id != user_id or run.status != "pending":
                raise ValueError("accepted_run does not match the launch request")
            with engine.SessionLocal() as db:
                durable_run = db.get(models.ChatRun, run.run_id)
                if durable_run is None or durable_run.status != "pending":
                    raise ValueError("accepted_run is no longer pending")
                durable_run.recovery_snapshot = recovery_snapshot
                durable_run.snapshot_version = max(int(durable_run.snapshot_version or 0), 1)
                durable_run.run_phase = durable_run.run_phase or "accepted"
                durable_run.last_operation_safety = (
                    durable_run.last_operation_safety or "replayable"
                )
                durable_run.updated_at = clock._utcnow()
                db.commit()
        return run

    run = await asyncio.to_thread(persist_launch)
    journal_owner = ownership.new_worker_owner(run.run_id)
    runtime.register_run_task(
        run.run_id,
        worker._run_workflow(
            run_id=run.run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            assistant_chat_seq=run.assistant_chat_seq,
            session_messages=session_messages,
            effective_user_message=effective_user_message,
            raw_user_message=raw_user_message,
            context=context,
            model_name=model_name,
            journal_owner=journal_owner,
        ),
        name=f"chat_run:{run.run_id}",
    )
    logger.info("chat_run_started", run_id=run.run_id, chat_id=chat_id, user_id=user_id)
    return run
