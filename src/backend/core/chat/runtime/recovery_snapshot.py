"""Persist interrupted turn snapshots and recovery terminal markers."""

from __future__ import annotations

import typing as _typing
from typing import Optional

import core.chat.runtime.handoff as handoff
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.runs.settings as settings
import core.chat.service as services_ChatService_module
from core.infra.logging import get_logger
from core.runs.journal import RecoveryDecision
from core.db.threads import run_in_session

logger = get_logger(__name__)


async def _commit_recovered_chat_snapshot(decision: RecoveryDecision) -> Optional[bool]:
    snapshot = dict(decision.snapshot or {})
    content = snapshot.get("assistant_content")
    message_id = str(snapshot.get("message_id") or decision.message_id)
    if not isinstance(content, str) or not message_id:
        paused = ownership.journal().needs_attention(
            decision.run_id,
            reason="model-completed snapshot is incomplete",
        )
        return False if paused else None
    owner = ownership.new_worker_owner(decision.run_id)
    recovered_run = runtime.get_run(decision.run_id)
    journal = ownership.journal()
    from core.runs.api_recovery import validate_recovery_context
    from sqlalchemy.exc import SQLAlchemyError

    try:
        def _restore_context(db):
            restored_context = validate_recovery_context(
                db,
                recovered_run,
                snapshot.get("context"),
                user_id=decision.user_id,
                chat_id=decision.chat_id,
            )
            return restored_context
        restored_context = await run_in_session(_restore_context)
        worker_args = snapshot.get("worker_args")
        if isinstance(worker_args, dict) and "context" in worker_args:
            def _check_worker_context(db):
                validate_recovery_context(
                    db,
                    recovered_run,
                    worker_args["context"],
                    user_id=decision.user_id,
                    chat_id=decision.chat_id,
                )
            await run_in_session(_check_worker_context)
    except (ValueError, SQLAlchemyError):
        paused = journal.needs_attention(
            decision.run_id, reason="agent API recovery scope is unavailable or inconsistent"
        )
        return False if paused else None
    if not journal.claim(
        decision.run_id,
        owner=owner,
        lease_seconds=settings.RUN_LEASE_SECONDS,
    ):
        return None
    try:
        from core.files.artifacts.service import persist_artifacts as _persist_artifacts
        from core.projects.edition import project_scope_from_context

        queued_handoff: _typing.Dict[str, _typing.Any] = {}

        def _commit_recovered_output(db) -> None:
            services_ChatService_module.ChatService(db).upsert_message(
                chat_id=decision.chat_id,
                role="assistant",
                content=content,
                message_id=message_id,
                chat_seq=(
                    int(recovered_run.assistant_chat_seq)
                    if recovered_run is not None and recovered_run.assistant_chat_seq is not None
                    else None
                ),
                model=(
                    str(snapshot["model_name"]) if snapshot.get("model_name") is not None else None
                ),
                thinking=(
                    list(snapshot["thinking"])
                    if isinstance(snapshot.get("thinking"), list)
                    else None
                ),
                tool_calls=(
                    list(snapshot["tool_calls"])
                    if isinstance(snapshot.get("tool_calls"), list)
                    else None
                ),
                model_steps=(
                    list(snapshot["model_steps"])
                    if isinstance(snapshot.get("model_steps"), list)
                    else None
                ),
                usage=(
                    dict(snapshot["usage"]) if isinstance(snapshot.get("usage"), dict) else None
                ),
                extra_data=(
                    dict(snapshot["extra_data"])
                    if isinstance(snapshot.get("extra_data"), dict)
                    else {}
                ),
                commit=False,
            )
            artifacts = snapshot.get("artifacts")
            if isinstance(artifacts, list):
                _persist_artifacts(
                    db,
                    decision.user_id,
                    decision.chat_id,
                    artifacts,
                    scope=project_scope_from_context(restored_context),
                    commit=False,
                )
            worker_args = snapshot.get("worker_args")
            recovery_args = worker_args if isinstance(worker_args, dict) else {}
            context_snapshot = snapshot.get("context")
            prepared = handoff._commit_queued_handoff_in_session(
                db,
                source_run_id=decision.run_id,
                chat_id=decision.chat_id,
                user_id=decision.user_id,
                context=(
                    restored_context
                    if isinstance(context_snapshot, dict)
                    else dict(recovery_args.get("context") or {})
                ),
                model_name=(
                    str(snapshot["model_name"])
                    if snapshot.get("model_name") is not None
                    else (
                        str(recovery_args["model_name"])
                        if recovery_args.get("model_name") is not None
                        else None
                    )
                ),
            )
            if prepared is not None:
                queued_handoff.update(prepared)

        completed = journal.complete(
            decision.run_id,
            owner=owner,
            status="completed",
            usage=(dict(snapshot["usage"]) if isinstance(snapshot.get("usage"), dict) else None),
            commit_effect=_commit_recovered_output,
            committed_operation={
                "operation_type": "message_committed_from_snapshot",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": message_id},
            },
        )
        if completed:
            if queued_handoff:
                handoff._schedule_queued_handoff(
                    queued_handoff,
                    chat_id=decision.chat_id,
                    user_id=decision.user_id,
                    task_prefix="chat_run_handoff_recovery",
                )
                await handoff._write_queued_run_started_projection(
                    decision.run_id,
                    chat_id=decision.chat_id,
                    handoff=queued_handoff,
                )
            await _write_recovered_terminal_marker(
                decision.run_id,
                chat_id=decision.chat_id,
            )
        return completed
    except Exception as exc:  # noqa: BLE001 - pause instead of replaying model output
        logger.error(
            "chat_run_snapshot_commit_failed",
            run_id=decision.run_id,
            error=str(exc),
            exc_info=True,
        )
        paused = journal.needs_attention(
            decision.run_id,
            reason=str(exc)[:1000],
            owner=owner,
        )
        return False if paused else None


async def _write_recovered_terminal_marker(run_id: str, *, chat_id: str) -> None:
    event = {"type": settings.TERMINAL_TYPE, "chat_id": chat_id, "_recovered": True}
    try:
        offset = ownership.journal().allocate_event_offset(run_id, terminal=True)
        await writer.xadd_event(run_id, offset, event)
        await writer.expire_stream(run_id)
    except Exception as exc:  # Redis is only a projection
        logger.warning("chat_run_recovered_terminal_write_failed", run_id=run_id, error=str(exc))
