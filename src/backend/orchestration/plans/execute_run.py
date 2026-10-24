"""Bind Plan execution to run leases, event replay and terminal persistence."""

from __future__ import annotations
from core.runs.tool_effects import errors as errors_module

import asyncio
import time
import typing as _typing
from typing import Any, Dict, List, Optional

import core.db.engine as engine
from core.db.threads import run_db, run_in_session
import core.db.models as models
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.settings as settings
import core.chat.service as services_ChatService_module
import core.runs.tool_effects.journal as tool_effect_ledger
from core.infra.logging import get_logger

logger = get_logger(__name__)


def _is_automation_chat(db, chat_id: str) -> bool:
    session = db.get(models.ChatSession, chat_id)
    return bool(session and (session.extra_data or {}).get("automation_run"))


def _execution_snapshot(db, plan_id, user_id, completed_steps, total_steps, result_text):
    from core.chat.plans import PlanService

    plan = PlanService(db).get_plan(plan_id, user_id)
    if not plan:
        return None
    return PlanService.build_execution_snapshot(
        plan, completed_steps=completed_steps, total_steps=total_steps, result_text=result_text
    )


async def _run_plan_execute_workflow(
    *,
    run_id: str,
    plan_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    enabled_mcp_ids: Optional[List[str]],
    enabled_skill_ids: Optional[List[str]],
    enabled_kb_ids: Optional[List[str]],
    enabled_agent_ids: Optional[List[str]],
    session_messages: List[Dict[str, Any]],
    model_name: str,
    journal_owner: Optional[str] = None,
) -> None:
    """Background plan-execute worker. Streams via XADD, persists at end.

    Uses short-lived sessions (one per logical operation) to avoid holding a
    DB connection across the entire long-running stream.
    """
    from core.chat.tool_log import attach_tool_result as _attach_tool_result
    from core.chat.tool_log import upsert_tool_call as _upsert_tool_call
    from core.llm.runtime import workspace as _workspace_mod
    from core.files.artifacts.service import persist_artifacts as _persist_artifacts
    from orchestration.plans.mode import astream_execute_plan

    owner = journal_owner or ownership.new_worker_owner(run_id)
    journal = ownership.journal()
    if not await run_db(
        journal.claim, run_id, owner=owner, lease_seconds=settings.RUN_LEASE_SECONDS, pending_only=True
    ):
        logger.info("plan_run_claim_skipped", run_id=run_id, owner=owner)
        return
    current = asyncio.current_task()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        ownership.lease_heartbeat(run_id, owner, current, lease_lost),
        name=f"plan_run_lease:{run_id}",
    )

    offsets = writer.EventOffsets(journal, run_id, owner, lease_lost=lease_lost)

    _emit = offsets.emit

    async def _pause_plan_tool_outcome(exc: BaseException) -> None:
        if not await run_db(
            journal.needs_attention,
            run_id,
            owner=owner,
            reason=f"tool outcome pending policy recovery: {exc}",
        ):
            return
        await _emit(
            {
                "type": "plan_error",
                "plan_id": plan_id,
                "error": "工具调用结果无法安全确定，任务已暂停等待处理",
            },
            terminal=True,
        )
        await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    result_text = ""
    completed_steps = 0
    total_steps = 0
    exec_usage: _typing.Optional[_typing.Dict[str, _typing.Any]] = None
    tool_calls_log: _typing.List[_typing.Dict[str, _typing.Any]] = []
    # Strict workspace gate also applies to plan-execute runs. The plan
    # subagent has access to pin_to_workspace and is expected to pin its
    # final deliverables.
    _workspace_mod.init_state()

    plan_snapshot = None
    _ws_pinned = []
    artifacts_meta = []
    _ws_files = []

    def _commit_plan_result(persist_db) -> None:  # noqa: ANN001
        chat_service = services_ChatService_module.ChatService(persist_db)
        # 被用户中断的那一轮同样会走到这里（协作式取消让生成器正常收尾），
        # 照着"执行完成"落库会让用户回到会话时看到一句"计划执行完成"，
        # 与他刚按下的停止完全对不上。按快照里的中断位分开措辞。
        _cancelled = bool((plan_snapshot or {}).get("cancelled"))
        content = result_text or (
            f"计划执行已中断：共 {total_steps} 步，完成 {completed_steps} 步。"
            if _cancelled
            else f"计划执行完成：共 {total_steps} 步，完成 {completed_steps} 步。"
        )
        chat_service.add_message(
            chat_id=chat_id,
            role="assistant",
            content=content,
            model=model_name,
            message_id=message_id,
            extra_data={
                "is_markdown": bool(result_text),
                "plan_id": plan_id,
                "completed_steps": completed_steps,
                "total_steps": total_steps,
                "plan_snapshot": plan_snapshot,
                "artifacts": artifacts_meta,
                "workspace_files": _ws_files,
                "message_id": message_id,
            },
            tool_calls=tool_calls_log or None,
            usage=exec_usage,
            commit=False,
        )
        # The plan-execute background worker doesn't hold a workflow context
        # dict, so it builds the ProjectScope via the reverse-lookup path
        # chat_id → ChatSession → Project.
        from core.projects.edition import project_scope_from_chat_id

        _persist_artifacts(
            persist_db,
            user_id,
            chat_id,
            _ws_pinned,
            scope=project_scope_from_chat_id(persist_db, chat_id),
            commit=False,
        )

    def _persist_plan_partial() -> None:
        nonlocal plan_snapshot, artifacts_meta, _ws_pinned, _ws_files
        plan_snapshot = dict(plan_snapshot or {})
        plan_snapshot["cancelled"] = True
        _ws_pinned = _workspace_mod.get_pinned()
        artifacts_meta = _ws_pinned
        _ws_files = _workspace_mod.get_pinned_file_ids()
        # _commit_plan_result 走 add_message（非幂等），重复写会撞主键
        try:
            with engine.SessionLocal() as _db:
                if _db.get(models.ChatMessage, message_id) is not None:
                    return
                _commit_plan_result(_db)
                _db.commit()
        except Exception:  # noqa: BLE001 - 落库失败不能吞掉取消收尾
            logger.warning(
                "plan_run_cancelled_partial_persist_failed",
                run_id=run_id,
                exc_info=True,
            )

    try:
        await _emit(
            {
                "type": "run_started",
                "run_id": run_id,
                "message_id": message_id,
                "chat_id": chat_id,
                # 这几条路径不会被恢复接管，worker 起来的那一刻就是本轮起点，
                # 不必为了拿它再读一次库——首帧前的每一次往返用户都在干等。
                "started_at": int(time.time() * 1000),
                "kind": "plan_execute",
                "plan_id": plan_id,
            }
        )

        from core.harness.tool_call_context import CURRENT_RUN_BINDING

        binding_token = CURRENT_RUN_BINDING.set((run_id, owner))
        try:
            automation_run = await run_in_session(_is_automation_chat, chat_id)
            async for event in astream_execute_plan(
                plan_id=plan_id,
                user_id=user_id,
                model_name=model_name,
                enabled_mcp_ids=enabled_mcp_ids,
                enabled_skill_ids=enabled_skill_ids,
                enabled_kb_ids=enabled_kb_ids,
                enabled_agent_ids=enabled_agent_ids,
                session_messages=session_messages,
                chat_id=chat_id,
                run_id=run_id,
                automation_run=automation_run,
            ):
                evt_type = event.get("type")
                if evt_type == "plan_complete":
                    result_text = event.get("result_text", "")
                    completed_steps = event.get("completed_steps", 0)
                    total_steps = event.get("total_steps", 0)
                    exec_usage = event.get("usage") or None
                elif evt_type == "tool_call":
                    _upsert_tool_call(
                        tool_calls_log,
                        {
                            "tool_name": event.get("tool_name"),
                            "tool_id": event.get("tool_id"),
                            "tool_args": event.get("tool_args", {}),
                            "step_id": event.get("step_id"),
                        },
                    )
                elif evt_type == "tool_result":
                    res = event.get("result")
                    tid = event.get("tool_id")
                    tn = event.get("tool_name")
                    _attach_tool_result(tool_calls_log, tid, tn, res)
                await _emit(event)
        finally:
            CURRENT_RUN_BINDING.reset(binding_token)

        # Build plan snapshot in its own short session
        plan_snapshot = None
        try:
            plan_snapshot = await run_in_session(
                _execution_snapshot, plan_id, user_id, completed_steps, total_steps, result_text
            )
        except Exception as snap_exc:
            logger.warning("plan_run_snapshot_failed", run_id=run_id, error=str(snap_exc))

        # Strict workspace gate: only files pinned via pin_to_workspace
        # surface anywhere user-visible — both the chat message and the
        # user's file library ("My Space") are sourced from this list.
        _ws_pinned = _workspace_mod.get_pinned()
        artifacts_meta = _ws_pinned
        _ws_files = _workspace_mod.get_pinned_file_ids()

        completed = await run_db(
            journal.complete,
            run_id,
            owner=owner,
            status="completed",
            usage=exec_usage,
            last_event_offset=offsets.last,
            commit_effect=_commit_plan_result,
            committed_operation={
                "operation_type": "message_committed",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": message_id},
            },
        )
        if completed:
            await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        else:
            # 用户取消时 cancel_run 已先写终态、清租约，上面的 CAS 注定输掉。
            # 协作式取消让生成器正常收尾，本轮已完成的步骤和正文就在手里 ——
            # 不落库的话，用户回到会话只剩自己那句话。
            if await run_db(ownership.cancelled_by_user, run_id):
                await run_db(_persist_plan_partial)
            else:
                logger.warning("plan_run_terminal_cas_lost", run_id=run_id, owner=owner)

    except asyncio.CancelledError:
        if lease_lost.is_set():
            logger.warning("plan_run_worker_fenced", run_id=run_id, owner=owner)
            if await run_db(ownership.cancelled_by_user, run_id):
                await run_db(_persist_plan_partial)
        elif await run_db(
            journal.complete,
            run_id,
            owner=owner,
            status="cancelled",
            failure_reason="plan worker cancelled",
            last_event_offset=offsets.last,
        ):
            logger.info("plan_run_cancelled", run_id=run_id)
            await _emit(
                {
                    "type": "plan_error",
                    "plan_id": plan_id,
                    "error": "任务已被用户取消",
                    "_cancelled": True,
                },
                terminal=True,
            )
            await _emit(
                {"type": settings.TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True},
                terminal=True,
            )

    except Exception as exc:
        nested_unknown = errors_module.find_tool_outcome_unknown(exc)
        if nested_unknown is not None:
            await _pause_plan_tool_outcome(nested_unknown)
            return
        logger.error("plan_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        failed = await run_db(
            journal.complete,
            run_id,
            owner=owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=offsets.last,
        )
        if failed:
            await _emit(
                {"type": "plan_error", "plan_id": plan_id, "error": str(exc)[:200]},
                terminal=True,
            )
            await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await writer.expire_stream(run_id)
