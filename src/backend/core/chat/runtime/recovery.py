"""Recover chat workers from committed snapshots after process loss."""

from __future__ import annotations

import core.chat.runtime.recovery_snapshot as recovery_snapshot
import core.chat.runtime.worker as worker
import core.db.engine as engine
import core.db.models as models
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.runs.settings as settings
import core.runs.tasks as tasks
from core.infra.logging import get_logger
from core.runs.journal import RecoveryDecision
from core.db.threads import run_in_session

logger = get_logger(__name__)


async def recover_orphan_runs() -> int:
    """Recover claimable runs from their last committed database safe point."""
    from core.runs.tool_effects.journal import ToolEffectJournal
    from core.runs.tool_effects.recovery import recover_incomplete_tool_effects

    # Desktop channel runs require a live device lease and a scoped I/O adapter.
    # Neither survives process restart. Fail closed before any tool-effect recovery.
    def _orphaned_channel_runs(db):
        local_channel_runs = [
            (run.run_id, run.chat_id)
            for run in db.query(models.ChatRun)
            .filter(models.ChatRun.status.in_(settings.LIVE_STATUSES))
            .all()
            if (run.request_payload or {}).get("source") == "desktop_channel"
            and run.run_id not in tasks.process_state().active_runs
        ]
        return local_channel_runs
    local_channel_runs = await run_in_session(_orphaned_channel_runs)
    for run_id, chat_id in local_channel_runs:
        if ownership.journal().cancel(
            run_id, reason="desktop channel lease cannot survive process restart"
        ):
            await writer._write_terminal_to_stream(
                run_id,
                chat_id=chat_id,
                error_text="本机机器人执行已中断，请检查结果后从渠道重新发起",
                cancelled=True,
            )
    effect_decisions = await recover_incomplete_tool_effects(
        journal=ToolEffectJournal(engine.SessionLocal)
    )
    effect_attention_runs = {
        item.run_id for item in effect_decisions if item.action == "needs_attention"
    }
    for run_id in effect_attention_runs:
        run = runtime.get_run(run_id)
        if run is not None:
            await writer._write_terminal_to_stream(
                run_id,
                chat_id=run.chat_id or "",
                error_text="工具调用结果无法安全确定，任务已暂停等待处理",
            )
    decisions = ownership.journal().recover()
    for decision in decisions:
        if decision.action == "resume":
            if not _register_recovered_chat(decision):
                reason = "run kind or recovery snapshot cannot be resumed automatically"
                if ownership.journal().needs_attention(decision.run_id, reason=reason):
                    await writer._write_terminal_to_stream(
                        decision.run_id,
                        chat_id=decision.chat_id or "",
                        error_text="任务缺少可恢复快照，已暂停等待处理",
                    )
        elif decision.action == "resume_from_snapshot":
            snapshot_result = await recovery_snapshot._commit_recovered_chat_snapshot(decision)
            if snapshot_result is False:
                await writer._write_terminal_to_stream(
                    decision.run_id,
                    chat_id=decision.chat_id or "",
                    error_text="已保存的模型结果无法安全提交，任务已暂停等待处理",
                )
        elif decision.action == "needs_attention":
            if decision.phase == "model_inflight":
                recovery_error = "模型调用结果不确定，任务已暂停等待恢复决策"
            elif decision.phase == "legacy_unrecoverable":
                recovery_error = "旧版本任务缺少恢复快照，已暂停等待处理"
            else:
                recovery_error = "任务在工具结果不确定的安全边界暂停，等待恢复决策"
            await writer._write_terminal_to_stream(
                decision.run_id,
                chat_id=decision.chat_id or "",
                error_text=recovery_error,
            )
        elif decision.action == "completed":
            await recovery_snapshot._write_recovered_terminal_marker(
                decision.run_id,
                chat_id=decision.chat_id or "",
            )
        elif decision.action == "failed":
            await writer._write_terminal_to_stream(
                decision.run_id,
                chat_id=decision.chat_id or "",
                error_text="服务重启发生在任务准备完成前，请重新发起",
            )
    recovered_count = len(
        {item.run_id for item in decisions}
        | effect_attention_runs
        | {run_id for run_id, _ in local_channel_runs}
    )
    if recovered_count:
        logger.info("chat_run_orphan_recovered", count=recovered_count)
    return recovered_count


def _register_recovered_chat(decision: RecoveryDecision) -> bool:
    snapshot = dict(decision.snapshot or {})
    if snapshot.get("kind") != "chat":
        return False
    args = snapshot.get("worker_args")
    if not isinstance(args, dict):
        return False
    required = {
        "session_messages",
        "effective_user_message",
        "raw_user_message",
        "context",
    }
    if not required.issubset(args):
        return False
    owner = ownership.new_worker_owner(decision.run_id)
    recovered_run = runtime.get_run(decision.run_id)
    if recovered_run and (recovered_run.request_payload or {}).get("source") == "desktop_channel":
        return False
    from core.runs.api_recovery import validate_recovery_context
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with engine.SessionLocal() as db:
            restored_context = validate_recovery_context(
                db,
                recovered_run,
                args.get("context"),
                user_id=decision.user_id,
                chat_id=decision.chat_id,
            )
            if "context" in snapshot:
                validate_recovery_context(
                    db,
                    recovered_run,
                    snapshot["context"],
                    user_id=decision.user_id,
                    chat_id=decision.chat_id,
                )
    except (ValueError, SQLAlchemyError):
        logger.warning("chat_run_recovery_scope_rejected", run_id=decision.run_id)
        return False
    runtime.register_run_task(
        decision.run_id,
        worker._run_workflow(
            run_id=decision.run_id,
            chat_id=decision.chat_id,
            user_id=decision.user_id,
            message_id=decision.message_id,
            assistant_chat_seq=(
                int(recovered_run.assistant_chat_seq)
                if recovered_run is not None and recovered_run.assistant_chat_seq is not None
                else None
            ),
            session_messages=list(args.get("session_messages") or []),
            effective_user_message=str(args.get("effective_user_message") or ""),
            raw_user_message=str(args.get("raw_user_message") or ""),
            context=restored_context,
            model_name=(str(args["model_name"]) if args.get("model_name") else None),
            journal_owner=owner,
            recovering=True,
        ),
        name=f"chat_run_recovery:{decision.run_id}",
    )
    return True
