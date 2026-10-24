"""Bind Plan generation to run leases and the existing event protocol."""

from __future__ import annotations
from core.runs.tool_effects import errors as errors_module

import asyncio
import contextlib
import time
import typing as _typing
from typing import Any, Dict, List, Optional

from core.db.threads import run_db
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.runs.settings as settings
import core.chat.service as services_ChatService_module
import core.runs.journal as run_journal
import core.runs.tool_effects.journal as tool_effect_ledger
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def _run_plan_generate_workflow(
    *,
    run_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    task_description: str,
    model_name: str,
    model_provider_id: Optional[str],
    enabled_mcp_ids: Optional[List[str]],
    enabled_skill_ids: Optional[List[str]],
    enabled_kb_ids: Optional[List[str]],
    enabled_agent_ids: Optional[List[str]],
    session_messages: List[Dict[str, Any]],
    uploaded_files: Optional[List[Dict[str, Any]]],
    journal_owner: Optional[str] = None,
) -> None:
    from orchestration.plans.mode import astream_generate_plan

    owner = journal_owner or ownership.new_worker_owner(run_id)
    journal = ownership.journal()
    if not await run_db(journal.claim, run_id, owner=owner, lease_seconds=settings.RUN_LEASE_SECONDS):
        logger.info("plan_generate_claim_skipped", run_id=run_id, owner=owner)
        return
    try:
        await run_db(
            journal.append_operation,
            run_id,
            owner=owner,
            operation_type="worker_started",
            phase="pre_model",
            safety="replayable",
        )
    except run_journal.RunLeaseLost:
        logger.warning("plan_generate_fenced_before_start", run_id=run_id, owner=owner)
        return
    worker_task = asyncio.current_task()
    if worker_task is None:  # pragma: no cover - asyncio always owns this coroutine
        raise RuntimeError("plan generate worker has no asyncio task")
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        ownership.lease_heartbeat(run_id, owner, worker_task, lease_lost),
        name=f"plan_generate_lease:{run_id}",
    )
    offsets = writer.EventOffsets(journal, run_id, owner, lease_lost=lease_lost)

    _emit = offsets.emit

    plan_id_out: _typing.Optional[str] = None
    plan_title = ""
    plan_desc = ""
    plan_snapshot: _typing.Optional[_typing.Dict[str, _typing.Any]] = None
    plan_record: _typing.Optional[_typing.Dict[str, _typing.Any]] = None
    assistant_content = ""
    gen_usage: _typing.Optional[_typing.Dict[str, _typing.Any]] = None

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
                "kind": "plan_generate",
            }
        )

        from core.harness.tool_call_context import CURRENT_RUN_BINDING

        binding_token = CURRENT_RUN_BINDING.set((run_id, owner))
        try:
            async for event in astream_generate_plan(
                task_description=task_description,
                user_id=user_id,
                model_name=model_name,
                model_provider_id=model_provider_id,
                enabled_mcp_ids=enabled_mcp_ids,
                enabled_skill_ids=enabled_skill_ids,
                enabled_kb_ids=enabled_kb_ids,
                enabled_agent_ids=enabled_agent_ids,
                session_messages=session_messages,
                uploaded_files=uploaded_files,
                chat_id=chat_id,
            ):
                if event.get("type") == "plan_generated":
                    plan_record = dict(event)
                    plan_id_out = event.get("plan_id")
                    plan_title = event.get("title", "")
                    plan_desc = event.get("description", "")
                    steps = event.get("steps", [])
                    step_summary = "\n".join(
                        f"{i + 1}. {s.get('title', '')}" for i, s in enumerate(steps)
                    )
                    assistant_content = (
                        f"已生成执行计划：**{plan_title}**\n\n"
                        f"{plan_desc}\n\n"
                        f"**执行步骤：**\n{step_summary}"
                    )
                    plan_snapshot = {
                        "mode": "preview",
                        "title": plan_title,
                        "description": plan_desc,
                        "steps": [
                            {
                                "step_order": s.get("step_order", i + 1),
                                "title": s.get("title", ""),
                                "description": s.get("description"),
                                "expected_tools": s.get("expected_tools", []),
                                "expected_skills": s.get("expected_skills", []),
                            }
                            for i, s in enumerate(steps)
                        ],
                        "total_steps": len(steps),
                        "completed_steps": 0,
                    }
                    gen_usage = event.get("usage") or None
                await _emit(event)
        finally:
            CURRENT_RUN_BINDING.reset(binding_token)

        def _commit_plan_preview(persist_db) -> None:  # noqa: ANN001
            if not assistant_content or not plan_id_out or plan_record is None:
                return
            from core.chat.plans import PlanService

            extra_data: _typing.Dict[str, _typing.Any] = {}
            if uploaded_files:
                extra_data["uploaded_files"] = list(uploaded_files)
            agent_name_map = plan_record.get("agent_name_map")
            if isinstance(agent_name_map, dict) and agent_name_map:
                extra_data["agent_name_map"] = dict(agent_name_map)
            PlanService(persist_db).create_plan(
                plan_id=str(plan_id_out),
                user_id=user_id,
                title=str(plan_record.get("title") or "未命名计划"),
                description=str(plan_record.get("description") or ""),
                task_input=task_description,
                steps=list(plan_record.get("steps") or []),
                extra_data=extra_data,
                commit=False,
            )
            services_ChatService_module.ChatService(persist_db).add_message(
                chat_id=chat_id,
                role="assistant",
                content=assistant_content,
                model=model_name,
                message_id=message_id,
                extra_data={
                    "is_markdown": True,
                    "plan_id": plan_id_out,
                    "plan_snapshot": plan_snapshot,
                    "message_id": message_id,
                },
                usage=gen_usage,
                commit=False,
            )

        completed = await run_db(
            journal.complete,
            run_id,
            owner=owner,
            status="completed",
            usage=gen_usage,
            last_event_offset=offsets.last,
            commit_effect=_commit_plan_preview,
            committed_operation={
                "operation_type": "message_committed",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": message_id, "plan_id": plan_id_out},
            },
        )
        if completed:
            await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        else:
            logger.warning("plan_generate_terminal_cas_lost", run_id=run_id, owner=owner)

    except asyncio.CancelledError:
        if lease_lost.is_set():
            logger.warning("plan_generate_worker_fenced", run_id=run_id, owner=owner)
        else:
            cancelled = await run_db(
                journal.complete,
                run_id,
                owner=owner,
                status="cancelled",
                failure_reason="plan generation worker cancelled",
                last_event_offset=offsets.last,
            )
            current = await run_db(runtime.get_run, run_id)
            if cancelled or (current is not None and current.status == "cancelled"):
                logger.info("plan_generate_run_cancelled", run_id=run_id)
            else:
                logger.warning(
                    "plan_generate_cancel_cas_lost",
                    run_id=run_id,
                    owner=owner,
                )
                return
            await _emit(
                {
                    "type": "plan_error",
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
            if await run_db(
                journal.needs_attention,
                run_id,
                owner=owner,
                reason=f"tool outcome pending policy recovery: {nested_unknown}",
            ):
                await _emit(
                    {
                        "type": "plan_error",
                        "error": "工具调用结果无法安全确定，任务已暂停等待处理",
                    },
                    terminal=True,
                )
                await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
            return
        logger.error("plan_generate_run_failed", run_id=run_id, error=str(exc), exc_info=True)
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
                {"type": "plan_error", "error": str(exc)[:200]},
                terminal=True,
            )
            await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await writer.expire_stream(run_id)
